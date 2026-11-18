"""Actual dispatch product API + portable host over unchanged shared input."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

from portable.core import Blocked
from portable.taskline_handoff import canonical
from portable.taskline_host import TasklineHost, DispatchRouteBridge, strict_json, identity
from taskline_host_checks import HostTests, SyntheticHostBridge


def load_dispatch(directory=None):
    folder = Path(directory) if directory else Path(__file__).parent / 'fixtures/dispatch-host-product'
    if directory is None:
        provenance = json.loads((folder / 'SOURCE.json').read_text())
        for row in provenance['files']:
            assert hashlib.sha256((folder / row['path']).read_bytes()).hexdigest() == row['sha256']
    saved = sys.path[:]
    previous = sys.modules.pop('taskline_inventory', None)
    try:
        sys.path.insert(0, str(folder))
        spec = importlib.util.spec_from_file_location('dispatch_host_product', folder / 'taskline_handoff.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = saved
        sys.modules.pop('taskline_inventory', None)
        if previous:
            sys.modules['taskline_inventory'] = previous


class CrossService:
    def __init__(self, services):
        self.services = services
        self.host = None
        self.consume = True
        self.sent = []

    def __call__(self, tool, args):
        if tool == 't3_thread_read':
            thread = self.services.service.threads[args['threadId']]
            return {'thread': deepcopy(thread), 'items': deepcopy(self.services.items)
                    if args['threadId'] == self.services.service.target else [],
                    'hasMore': False, 'nextPosition': None}
        if tool == 't3_thread_send':
            packet = strict_json(args['message'])
            self.sent.append(deepcopy(packet))
            if self.consume:
                output = self.host.consume(self.services.fixture['request']['manifest']['manifest_id'],
                                            packet, self.services.contexts)
                # Actual agent publications use the dispatch single-receipt
                # wrapper, not a fake fixture transformation inside T3Relay.
                for publication in output['agent_outputs']:
                    from portable.taskline_handoff import sha256
                    item_id = sha256(publication)
                    self.services.audit[item_id] = identity(self.services.proof)
                    self.services.items.append({'itemId': item_id, 'sourceThreadId': args['threadId'],
                        'type': 'assistant_message', 'createdBy': 'agent', 'status': 'completed',
                        'visibility': 'local', 'textTruncated': False, 'text': canonical(publication)})
            return {'messageId': args['clientRequestId'], 'runId': 'synthetic-agent-turn', 'delivery': 'accepted'}
        return self.services.service(tool, args)


class CrossHostTests(unittest.TestCase):
    def setUp(self):
        # Reuse isolated context/setup only, without inheriting/rerunning tests.
        self.case = HostTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root, self.fixture, self.mid = self.case.root, self.case.fixture, self.case.mid
        self.services = self.case.bridge
        self.transport = CrossService(self.services)
        self.api = load_dispatch()
        self.dispatch_root = self.root / 'dispatch-fixture'
        for directory in ('tasks', 'dispatches', 'task-claims'):
            (self.dispatch_root / directory).mkdir(parents=True)
        for binding in self.fixture['request']['manifest']['bindings']:
            task = {'task_id': binding['task_id'], 'caller': 'synthetic-original-caller',
                    'status': 'awaiting_acceptance', 'current_run': None, 'runs': [binding['run_id']]}
            (self.dispatch_root / 'tasks' / (binding['task_id'] + '.json')).write_text(canonical(task))
            run = self.dispatch_root / 'dispatches' / binding['run_id']
            run.mkdir()
            (run / 'meta.json').write_text(canonical({'id': binding['run_id'], 'task_id': binding['task_id'],
                'session': 'synthetic-original-caller', 'status': 'done', 'pid': None, 'exit_code': 0}))
        self.bridge = DispatchRouteBridge(self.api, self.services, self.transport,
                    route_root=self.root / 'routes', dispatch_root=self.dispatch_root, clock=lambda: 1000)
        self.host = TasklineHost(self.root / 'state', transport=self.transport,
                                 bridge=self.bridge, clock=lambda: 1000)
        self.transport.host = self.host
        self.before = {str(p.relative_to(self.dispatch_root)): p.read_bytes()
                       for p in self.dispatch_root.rglob('*.json')}

    def test_real_route_store_relay_runtime_inventory_and_host_consumer(self):
        result = self.host.loop(self.mid, iterations=2)
        self.assertEqual(result['handoff_phase'], 'ARCHIVE_PENDING')
        state = self.host.store.status(self.mid)
        self.assertEqual(state['blocker'], 'BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE')
        self.assertFalse(state['archive_attempted'])
        self.assertEqual(len(self.transport.sent), 4)  # result + probe for each original task
        for binding in self.fixture['request']['manifest']['bindings']:
            status = self.bridge.routes.status(binding['handoff_id'])
            self.assertEqual(status['pending_count'], 0)
            self.assertGreaterEqual(status['consumed_count'], 2)
            self.assertFalse(status['persistent_route'])  # finite host loop released its real lease
            self.assertIsNone(self.api.RouteRuntime.query(self.bridge.routes, binding))
        self.assertEqual({str(p.relative_to(self.dispatch_root)): p.read_bytes()
                          for p in self.dispatch_root.rglob('*.json')}, self.before)
        self.assertEqual(sum(n == 't3_thread_organize' for n, _ in self.services.service.calls), 0)

    def test_real_product_inactive_transfer_continue_queries_new_run_without_creation(self):
        from taskline_next_step_checks import install_operation, kernel_reader_for
        from portable.taskline_host import ReadOnlyNextStep
        request = deepcopy(self.fixture['request'])
        request['manifest']['version'] = 2
        for metadata in request['member_metadata'].values():
            metadata['worker_active'] = False
        self.host.store.prepare_manifest(**request, upgrade=True)
        from taskline_shared_fixture import FixtureQueries
        updated_fixture = deepcopy(self.fixture)
        updated_fixture['request'] = request
        self.services.fixture = updated_fixture
        self.services.queries = FixtureQueries(updated_fixture)
        for binding in request['manifest']['bindings']:
            install_operation(self.dispatch_root, binding, self.services.proof)
        # The dispatch product independently validates the real current test
        # process start identity; claim-wrapper evidence remains a separate
        # synthetic kernel fixture validated by portable's additional layer.
        import os
        import time
        kernel = self.api.process_identity(os.getpid())
        if kernel['process_start'].startswith('darwin:'):
            epoch = int(kernel['process_start'].split(':')[1])
        else:
            boot = next(line.split()[1] for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
            epoch = int(boot) + int(kernel['process_start'].rsplit(':', 1)[1]) / os.sysconf('SC_CLK_TCK')
        tm = time.localtime(epoch)
        started = (('Mon','Tue','Wed','Thu','Fri','Sat','Sun')[tm.tm_wday] + ' ' +
                   ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')[tm.tm_mon-1] +
                   f' {tm.tm_mday} {tm.tm_hour:02}:{tm.tm_min:02}:{tm.tm_sec:02} {tm.tm_year}')
        process = {'pid_started': started}
        for binding in request['manifest']['bindings']:
            path = self.dispatch_root / 'dispatches' / (binding['run_id'] + '-next') / 'meta.json'
            value = json.loads(path.read_text())
            value.update(pid=os.getpid(), pid_started=process['pid_started'], work='/fixture/project')
            path.write_text(canonical(value))
            old_path = self.dispatch_root / 'dispatches' / binding['run_id'] / 'meta.json'
            old_value = json.loads(old_path.read_text()); old_value['work'] = '/fixture/project'
            old_path.write_text(canonical(old_value))
            task_path = self.dispatch_root / 'tasks' / (binding['task_id'] + '.json')
            task = json.loads(task_path.read_text()); task['workspace'] = '/fixture/project'
            task_path.write_text(canonical(task))
        self.bridge.next_step_reader = ReadOnlyNextStep(self.dispatch_root,
                                process_reader=kernel_reader_for(self.dispatch_root))
        before = {str(p): p.read_bytes() for p in self.dispatch_root.rglob('*.json')}
        self.host.loop(self.mid, iterations=2)
        state = self.host.store.status(self.mid)
        self.assertEqual(state['blocker'], 'BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE')
        steps = [r for r in state['receipts'] if r['kind'] == 'next_step_started']
        self.assertEqual(len(steps), 2)
        self.assertTrue(all(r['next_run_id'] != r['run_id'] for r in steps))
        self.assertEqual({str(p): p.read_bytes() for p in self.dispatch_root.rglob('*.json')}, before)
        self.assertEqual(sum(n == 't3_thread_organize' for n, _ in self.services.service.calls), 0)

    def test_late_result_routes_without_cas_and_without_archive(self):
        self.host.loop(self.mid)
        binding = self.fixture['request']['manifest']['bindings'][0]
        self.bridge.routes.publish(binding['handoff_id'], 'late-synthetic-result', {'kind': 'result', 'value': 'late'})
        self.host.loop(self.mid, resume=True)
        self.assertEqual(self.bridge.routes.status(binding['handoff_id'])['pending_count'], 0)
        self.assertTrue(any(p['event_id'] == 'late-synthetic-result' for p in self.transport.sent))
        self.assertEqual(sum(n == 't3_thread_organize' for n, _ in self.services.service.calls), 0)

    def test_actual_dispatch_acceptance_not_consumption_remains_pending(self):
        self.transport.consume = False
        self.host.loop(self.mid)
        for binding in self.fixture['request']['manifest']['bindings']:
            self.assertGreater(self.bridge.routes.status(binding['handoff_id'])['pending_count'], 0)
        self.assertEqual(self.host.store.status(self.mid)['blocker'], 'BLOCKED_MISSING_EVIDENCE')

    def test_real_observe_late_answer_preserves_original_payload_hash_and_is_idempotent(self):
        from portable.taskline_handoff import sha256
        self.host.loop(self.mid)
        binding = self.fixture['request']['manifest']['bindings'][0]
        answer = self.dispatch_root / 'dispatches' / binding['run_id'] / 'final-answer.txt'
        answer.write_text('SYNTHETIC LATE RESULT, ORIGINAL TASK/RUN')
        before = len(self.transport.sent)
        self.host.loop(self.mid, resume=True)
        late = [packet for packet in self.transport.sent[before:]
                if packet['payload'].get('result', {}).get('text') == answer.read_text()]
        self.assertEqual(len(late), 1)
        packet = late[0]
        self.assertNotIn('context_file', packet['payload'])
        self.assertEqual(packet['payload_sha256'], sha256(packet['payload']))
        self.assertEqual(packet['binding'], binding)
        self.assertEqual(packet['payload']['task_id'], binding['task_id'])
        self.assertEqual(packet['payload']['run_id'], binding['run_id'])
        acknowledged = self.bridge.routes.status(binding['handoff_id'])
        self.assertEqual(acknowledged['pending_count'], 0)
        sent = len(self.transport.sent)
        self.host.loop(self.mid, resume=True)
        self.assertEqual(len(self.transport.sent), sent)
        self.assertEqual(sum(n == 't3_thread_organize' for n, _ in self.services.service.calls), 0)
        self.assertFalse(self.services.service.threads[self.services.service.source]['archived'])

    def test_wrong_wire_digest_and_payload_rejected_by_product_bridge(self):
        self.transport.consume = False
        self.host.loop(self.mid)
        packet = deepcopy(self.transport.sent[0])
        packet['binding_digest'] = 'c' * 64
        with self.assertRaisesRegex(Blocked, 'EVENT_BINDING'):
            self.bridge.translate_event(packet, self.services.proof)

    def test_complete_inventory_detects_omitted_second_task(self):
        inventory = self.bridge.inventory(self.fixture['request']['manifest']['source_thread']['thread_id'],
                                          self.services.proof)
        self.assertEqual(len(inventory['task_runs']), 2)
        request = deepcopy(self.fixture['request'])
        request['manifest']['version'] = 2
        request['manifest']['bindings'] = request['manifest']['bindings'][:1]
        request['member_metadata'].pop('synthetic-handoff-2')
        self.host.store.prepare_manifest(**request, upgrade=True)
        with self.assertRaisesRegex(Blocked, 'INVENTORY_MISMATCH'):
            self.host.loop(self.mid)
