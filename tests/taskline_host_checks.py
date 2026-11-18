"""Host tests loaded by test_taskline_handoff; all CLI/session data synthetic."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from portable.core import Blocked
from portable.taskline_handoff import HandoffStore, canonical, manifest_scope, sha256
from portable.taskline_host import (ACPCLITransport, TasklineHost, EVENT_SCHEMA,
                                    SESSION_SCHEMA, identity, session_digest)
from taskline_shared_fixture import load_fixture, FixtureQueries, SimulatedT3


class SyntheticHostBridge:
    def __init__(self, fixture, contexts):
        self.fixture, self.contexts = deepcopy(fixture), contexts
        self.queries = FixtureQueries(fixture)
        manifest = fixture['request']['manifest']
        self.proof = {'schema': SESSION_SCHEMA, 'environment_id': '11111111-2222-4333-8444-555555555555',
                      'app_thread_id': manifest['target_thread']['thread_id'],
                      'native_caller': next(iter(fixture['request']['member_metadata'].values()))['current_caller'],
                      'provider_session_id': 'synthetic-provider-session-1',
                      'provider_instance_id': 'synthetic-provider', 'generation': 1, 'scope': 'engineering',
                      'issued_at': 900, 'expires_at': 1100,
                      'provenance': {'issuer': 'synthetic-formal-bridge', 'proof_id': 'proof-1',
                                     'mapping_source': 'synthetic-registry-and-dispatch-identity'}}
        self.service = SimulatedT3(fixture)
        self.items, self.audit, self.events = [], {}, {}
        self.history_complete, self.verified, self.publish = True, True, True
        self.active, self.enters, self.exits, self.pumps, self.heartbeats = 0, 0, 0, 0, 0
        self.host, self.on_pump = None, None
        self.sequence = 0

    def resolve_session(self):
        return deepcopy(self.proof)

    def verify_session(self, proof):
        return deepcopy(self.proof) if self.verified and proof == self.proof else None

    def caller_thread_history(self, proof):
        rows = [{k: proof[k] for k in ('native_caller', 'app_thread_id', 'provider_session_id',
                                       'provider_instance_id', 'generation', 'provenance')}]
        rows.append({'native_caller': 'synthetic-original-caller',
                     'app_thread_id': self.fixture['request']['manifest']['source_thread']['thread_id'],
                     'provider_session_id': 'source-session', 'provider_instance_id': 'source-instance',
                     'generation': 1, 'provenance': deepcopy(proof['provenance'])})
        return {'schema': 'portable.caller-thread-history/v1',
                **{k: proof[k] for k in ('environment_id', 'provider_session_id', 'provider_instance_id', 'generation')},
                'caller_history_complete': self.history_complete, 'caller_history': rows,
                'provenance': deepcopy(proof['provenance'])}

    def authorization(self, binding, ref, proof):
        return self.queries.authorization_query(binding, ref)

    def inventory(self, thread, proof):
        return {'complete': True, 'caller_history_complete': self.history_complete, 'checked_at': 1000,
                'environment_id': proof['environment_id'], 'source_thread_id': thread,
                'caller_history': [{'native_caller': 'synthetic-original-caller',
                                    'provider_session_id': 'synthetic-source-session', 'generation': 1}],
                'task_runs': deepcopy(self.queries.inventory), 'provenance': 'synthetic-full-inventory'}

    def event(self, binding):
        manifest = self.fixture['request']['manifest']
        payload = {'context_file': binding['handoff_id'] + '.json', 'sequence': self.sequence}
        event = {'schema': EVENT_SCHEMA, **manifest_scope(manifest), 'binding': deepcopy(binding),
                 'event_id': sha256([binding, manifest_scope(manifest), self.sequence]),
                 'payload': payload, 'payload_sha256': sha256(payload)}
        self.events[event['event_id']] = deepcopy(event)
        return event

    def verify_route_event(self, binding, event, proof):
        actual = self.events.get(event['event_id'])
        if not actual or actual['binding'] != binding or actual['payload_sha256'] != event['payload_sha256']:
            return None
        return {'binding_digest': sha256(binding), 'event_id': event['event_id'],
                'payload_sha256': event['payload_sha256'], 'target_thread_id': proof['app_thread_id'],
                'persistent_route': self.active > 0, 'provenance': 'synthetic-runtime-and-event-query'}

    def verify_item_actor(self, item, proof):
        if self.audit.get(item['itemId']) != identity(proof):
            return None
        return {'item_id': item['itemId'], 'session_digest': session_digest(proof),
                'provenance': 'synthetic-provider-publication-audit'}

    @contextmanager
    def route_runtime(self, binding, proof):
        bridge = self
        class Runtime:
            def heartbeat(self):
                bridge.heartbeats += 1
        self.active += 1
        self.enters += 1
        try:
            yield Runtime()
        finally:
            self.active -= 1
            self.exits += 1

    def pump_once(self, binding, runtime, proof):
        self.pumps += 1
        if self.on_pump:
            self.on_pump(self)
        if self.host.status(self.fixture['request']['manifest']['manifest_id'])['stop_requested']:
            return None
        candidate = self.host.consume(self.fixture['request']['manifest']['manifest_id'],
                                      self.event(binding), self.contexts)
        if self.publish:
            # Simulate actual successor agent output in a real-shaped local
            # assistant message. Independent query reads this service state.
            item_id = sha256(candidate['agent_output'])
            self.audit[item_id] = identity(proof)
            self.items.append({'itemId': item_id, 'sourceThreadId': proof['app_thread_id'],
                               'visibility': 'local', 'createdBy': 'agent', 'status': 'completed',
                               'type': 'assistant_message', 'textTruncated': False,
                               'text': canonical(candidate['agent_output'])})
        return candidate

    def transport(self, tool, args):
        if tool == 't3_thread_read' and args.get('view') == 'messages':
            return {'structuredContent': {'thread': {'threadId': args['threadId']},
                                          'items': deepcopy(self.items), 'hasMore': False, 'nextPosition': None}}
        return self.service(tool, args)


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = load_fixture()
        self.mid = self.fixture['request']['manifest']['manifest_id']
        self.store = HandoffStore(self.root / 'state')
        self.store.prepare_manifest(**self.fixture['request'])
        self.contexts = self.root / 'contexts'
        self.contexts.mkdir()
        for hid, context in self.fixture['contexts'].items():
            (self.contexts / (hid + '.json')).write_text(canonical(context))
        self.bridge = SyntheticHostBridge(self.fixture, self.contexts)
        self.host = TasklineHost(self.root / 'state', transport=self.bridge.transport,
                                 bridge=self.bridge, clock=lambda: 1000)
        self.bridge.host = self.host

    def fake_cli(self):
        path = self.root / 'fake-t3'
        path.write_text('#!' + sys.executable + '\n' + '''import json,os,sys,time
mode=os.environ.get('HOST_FAKE_MODE','normal')
if mode=='timeout': time.sleep(2)
if mode=='error':
    sys.stderr.write(os.environ['T3_ACP_MCP_AUTHORIZATION'])
    sys.exit(1)
if mode=='flood':
    sys.stdout.write('x'*100000)
    sys.exit(0)
if mode=='bad-json': print('{"a":1,"a":2}'); sys.exit(0)
if mode=='nan': print('{"a":NaN}'); sys.exit(0)
if mode=='secret': print(json.dumps({'echo':os.environ['T3_ACP_MCP_AUTHORIZATION']})); sys.exit(0)
if mode=='escaped-secret':
    print('{"echo":"'+''.join(chr(92)+'u'+format(ord(c),'04x') for c in os.environ['T3_ACP_MCP_AUTHORIZATION'])+'"}')
    sys.exit(0)
print(json.dumps({'argv':sys.argv[1:],'session_present':bool(os.environ.get('T3_ACP_MCP_AUTHORIZATION'))}))
''')
        path.chmod(0o700)
        return str(path)

    def env(self, mode='normal'):
        return patch.dict(os.environ, {'T3_ACP_MCP_ENDPOINT': 'http://synthetic-endpoint.invalid/mcp',
                                       'T3_ACP_MCP_AUTHORIZATION': 'SYNTHETIC-SESSION-CREDENTIAL',
                                       'HOST_FAKE_MODE': mode})

    def activate(self):
        state = self.store.status(self.mid)
        proof = self.host._session(state)
        self.host._activate(self.mid, state, proof, resume=False)
        return state, proof

    def test_official_cli_argv_is_json_no_shell_or_credential_argv(self):
        executable = self.fake_cli()
        with self.env():
            result = ACPCLITransport(executable)('t3_thread_read', {'threadId': 'x; touch never'})
        self.assertEqual(result['argv'], ['acp-mcp-call', 't3_thread_read', '{"threadId":"x; touch never"}'])
        self.assertTrue(result['session_present'])
        self.assertNotIn('SYNTHETIC-SESSION-CREDENTIAL', canonical(result))
        self.assertFalse((self.root / 'never').exists())

    def test_cli_error_timeout_finite_output_strict_json_and_secret_suppression(self):
        executable = self.fake_cli()
        for mode, reason in [('error', 'BLOCKED_RPC_TOOL_ERROR'), ('timeout', 'BLOCKED_RPC_TIMEOUT'),
                             ('flood', 'BLOCKED_RPC_OUTPUT_LIMIT'), ('bad-json', 'BLOCKED_RPC_JSON'),
                             ('nan', 'BLOCKED_RPC_JSON'), ('secret', 'BLOCKED_RPC_SECRET_ECHO'),
                             ('escaped-secret', 'BLOCKED_RPC_SECRET_ECHO')]:
            with self.subTest(mode=mode), self.env(mode):
                transport = ACPCLITransport(executable, timeout=0.05 if mode == 'timeout' else 2, max_output=2048)
                with self.assertRaises(Blocked) as error:
                    transport('t3_thread_read', {'threadId': 'synthetic'})
                self.assertEqual(str(error.exception), reason)
                self.assertNotIn('SYNTHETIC-SESSION-CREDENTIAL', str(error.exception))

    def test_missing_environment_and_dangerous_tools_block_without_child(self):
        with patch.dict(os.environ):
            os.environ.pop('T3_ACP_MCP_ENDPOINT', None)
            os.environ.pop('T3_ACP_MCP_AUTHORIZATION', None)
            with self.assertRaisesRegex(Blocked, 'RPC_SESSION_UNAVAILABLE'):
                ACPCLITransport('/does-not-exist')('t3_thread_read', {'threadId': 'x'})
        with self.env():
            for tool, args in [('t3_thread_send', {}), ('t3_thread_update', {'action': 'regenerate_title'}),
                               ('t3_thread_organize', {'action': 'delete', 'threadId': 'x'})]:
                with self.assertRaisesRegex(Blocked, 'ALLOWLIST'):
                    ACPCLITransport('/does-not-exist')(tool, args)

    def test_session_credential_cannot_enter_argv_proof_or_journal(self):
        with self.env():
            with self.assertRaisesRegex(Blocked, 'SECRET_ARGUMENT'):
                ACPCLITransport('/does-not-exist')('t3_thread_read', {'threadId': 'SYNTHETIC-SESSION-CREDENTIAL'})
            self.bridge.proof['provenance']['proof_id'] = 'SYNTHETIC-SESSION-CREDENTIAL'
            with self.assertRaisesRegex(Blocked, 'PROOF_CREDENTIAL_ECHO'):
                self.host.loop(self.mid)
        self.assertEqual(self.bridge.enters, 0)
        self.assertFalse(self.host.journal.files.path(self.mid).exists())

    def test_default_cli_host_help_and_precise_mapping_block(self):
        command = [sys.executable, '-B', '-m', 'portable', 'taskline', '--state-root', str(self.root / 'state'), 'host']
        source = Path(__file__).resolve().parents[1]
        help_result = subprocess.run(command + ['--help'], cwd=source, capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('resume', help_result.stdout)
        with self.env():
            result = subprocess.run(command + ['doctor'], cwd=source, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['blockers'], ['BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE', 'BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE'])
        self.assertNotIn('SYNTHETIC-SESSION-CREDENTIAL', result.stdout + result.stderr)

    def test_untrusted_missing_wrong_thread_or_wrong_caller_proof_rejected(self):
        self.bridge.verified = False
        with self.assertRaisesRegex(Blocked, 'SESSION_UNVERIFIED'):
            self.host.loop(self.mid)
        self.bridge.verified = True
        for key, value in [('app_thread_id', 'other'), ('native_caller', 'borrowed-old-caller')]:
            original = self.bridge.proof[key]
            self.bridge.proof[key] = value
            with self.assertRaisesRegex(Blocked, 'CALLER_THREAD_MISMATCH'):
                self.host.loop(self.mid)
            self.bridge.proof[key] = original
        self.assertEqual(self.bridge.enters, 0)
        self.assertEqual(self.bridge.service.calls, [])

    def test_full_inventory_not_manifest_echo_and_missing_member_blocks_runtime(self):
        self.bridge.history_complete = False
        with self.assertRaisesRegex(Blocked, 'CALLER_HISTORY_UNAVAILABLE'):
            self.host.loop(self.mid)
        self.bridge.history_complete = True
        self.bridge.queries.inventory.pop()
        with self.assertRaisesRegex(Blocked, 'INVENTORY_MISMATCH'):
            self.host.loop(self.mid)
        self.assertEqual(self.bridge.enters, 0)

    def test_loop_consumes_publishes_queries_and_strict_gate_keeps_window(self):
        result = self.host.loop(self.mid, iterations=2)
        self.assertEqual(result['handoff_phase'], 'ARCHIVE_PENDING')
        self.assertEqual(self.store.status(self.mid)['blocker'], 'BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE')
        self.assertEqual(self.bridge.enters, 2)
        self.assertEqual(self.bridge.exits, 2)
        self.assertEqual(self.bridge.pumps, 4)  # continues routing after archive
        self.assertEqual(self.bridge.active, 0)
        self.assertEqual(sum(name == 't3_thread_organize' for name, _ in self.bridge.service.calls), 0)
        self.assertEqual(self.host.status(self.mid)['status'], 'STOPPED')

    def test_local_output_and_delivery_acceptance_never_remote_published_proof(self):
        self.bridge.publish = False
        result = self.host.loop(self.mid)
        self.assertEqual(result['handoff_phase'], 'BLOCKED')
        self.assertEqual(self.store.status(self.mid)['blocker'], 'BLOCKED_MISSING_EVIDENCE')
        self.assertEqual(len(self.bridge.items), 0)
        record = self.host.journal.load(self.mid)
        self.assertEqual(len(record['events']), 2)
        self.assertTrue(all(event['publication'] == 'LOCAL_ONLY' for event in record['events'].values()))
        self.assertEqual(sum(n == 't3_thread_organize' for n, _ in self.bridge.service.calls), 0)

    def test_context_mismatch_wrong_binding_version_and_id_equivocation_rejected(self):
        self.activate()
        binding = self.fixture['request']['manifest']['bindings'][0]
        event = self.bridge.event(binding)
        path = self.contexts / event['payload']['context_file']
        original = path.read_bytes()
        path.write_text('{}')
        with self.assertRaisesRegex(Blocked, 'CONTEXT_HASH'):
            self.host.consume(self.mid, event, self.contexts)
        path.write_bytes(original)
        wrong = deepcopy(event)
        wrong['manifest_version'] = 2
        with self.assertRaisesRegex(Blocked, 'EVENT_BINDING'):
            self.host.consume(self.mid, wrong, self.contexts)
        first = self.host.consume(self.mid, event, self.contexts)
        second = self.host.consume(self.mid, event, self.contexts)
        self.assertEqual(first, second)
        self.assertEqual(len(self.host.journal.load(self.mid)['events']), 1)
        changed = deepcopy(event)
        changed['payload']['sequence'] = 2
        changed['payload_sha256'] = sha256(changed['payload'])
        self.bridge.events[changed['event_id']] = changed
        with self.assertRaisesRegex(Blocked, 'EVENT_CONFLICT'):
            self.host.consume(self.mid, changed, self.contexts)
        self.assertTrue(all(r['kind'] != 'next_step_started' for r in first['agent_output']['receipts']))

    def test_context_path_symlink_and_wrong_caller_rejected(self):
        self.activate()
        binding = self.fixture['request']['manifest']['bindings'][0]
        event = self.bridge.event(binding)
        path = self.contexts / event['payload']['context_file']
        path.rename(self.root / 'original-context')
        path.symlink_to(self.root / 'original-context')
        with self.assertRaisesRegex(Blocked, 'CONTEXT_PATH'):
            self.host.consume(self.mid, event, self.contexts)
        self.bridge.proof['native_caller'] = 'wrong-caller'
        with self.assertRaisesRegex(Blocked, 'CALLER_THREAD_MISMATCH'):
            self.host.consume(self.mid, event, self.contexts)

    def test_generation_change_invalidates_loop_and_releases_all_runtime_leases(self):
        self.bridge.on_pump = lambda bridge: bridge.proof.update(generation=2, provider_session_id='session-2')
        with self.assertRaisesRegex(Blocked, 'SESSION_CHANGED'):
            self.host.loop(self.mid)
        self.assertEqual(self.host.status(self.mid)['status'], 'INVALIDATED')
        self.assertEqual(self.bridge.active, 0)
        self.assertEqual(self.bridge.exits, 2)
        self.assertEqual(sum(n == 't3_thread_organize' for n, _ in self.bridge.service.calls), 0)

    def test_pause_readonly_and_expiry_never_start_pump(self):
        for scope in ('paused', 'read_only'):
            self.bridge.proof['scope'] = scope
            with self.assertRaisesRegex(Blocked, 'HOST_SCOPE'):
                self.host.loop(self.mid)
        self.bridge.proof['scope'] = 'engineering'
        self.bridge.proof['expires_at'] = 999
        with self.assertRaisesRegex(Blocked, 'EXPIRED'):
            self.host.loop(self.mid)
        self.assertEqual(self.bridge.enters, 0)

    def test_stop_resume_same_valid_session_and_independent_loop_lease(self):
        self.bridge.publish = False
        self.bridge.on_pump = lambda bridge: self.host.stop(self.mid)
        # stop during a pump prevents any metadata RPC; the loop exits locally.
        self.host.loop(self.mid, iterations=2)
        self.assertEqual(self.host.status(self.mid)['status'], 'STOPPED')
        self.bridge.on_pump = None
        self.host.loop(self.mid, resume=True)
        self.assertEqual(self.host.status(self.mid)['status'], 'STOPPED')
        with self.host.journal.lease(self.mid):
            with self.assertRaisesRegex(Blocked, 'ALREADY_RUNNING'):
                self.host.loop(self.mid)

    def test_new_successor_session_requires_new_caller_and_old_session_cannot_resume(self):
        self.bridge.publish = False
        self.host.loop(self.mid)
        self.bridge.proof.update(generation=2, provider_session_id='session-2')
        with self.assertRaisesRegex(Blocked, 'NEW_CALLER_REQUIRED'):
            self.host.loop(self.mid)
        self.assertEqual(self.bridge.active, 0)

    def test_forged_local_publication_actor_or_payload_receipt_does_not_archive(self):
        self.bridge.publish = False
        self.host.loop(self.mid)
        local = self.host.journal.load(self.mid)
        output = {'schema': 'portable.agent-consumption/v1', 'session': identity(self.bridge.proof),
                  'receipts': next(event['receipts'] for event in local['events'].values()
                                   if event['receipts'][0]['handoff_id'] ==
                                   self.fixture['request']['manifest']['bindings'][0]['handoff_id'])}
        item = {'itemId': 'forged-item', 'sourceThreadId': self.bridge.proof['app_thread_id'],
                'visibility': 'local', 'createdBy': 'agent', 'status': 'completed',
                               'type': 'assistant_message', 'textTruncated': False,
                'text': canonical(output)}
        self.bridge.items = [item]
        self.host.loop(self.mid, resume=True)
        self.assertEqual(self.store.status(self.mid)['blocker'], 'BLOCKED_RECEIPT_ACTOR_UNVERIFIED')
        self.bridge.audit['forged-item'] = identity(self.bridge.proof)
        output['receipts'][0]['receipt_id'] = 'not-produced-by-context-reader'
        item['text'] = canonical(output)
        self.host.loop(self.mid, resume=True)
        self.assertEqual(self.store.status(self.mid)['blocker'], 'BLOCKED_RECEIPT_LOCAL_READ_UNVERIFIED')
        self.assertEqual(sum(n == 't3_thread_organize' for n, _ in self.bridge.service.calls), 0)

    def test_stale_old_session_publication_is_ignored_not_migrated(self):
        self.bridge.items = [{'itemId': 'old', 'sourceThreadId': self.bridge.proof['app_thread_id'],
                'visibility': 'local', 'createdBy': 'agent', 'status': 'completed',
                               'type': 'assistant_message', 'textTruncated': False,
                'text': canonical({'schema': 'portable.agent-consumption/v1',
                                   'session': {'native_caller': 'old'}, 'receipts': []})}]
        self.assertEqual(self.host.loop(self.mid)['handoff_phase'], 'ARCHIVE_PENDING')
        self.assertEqual(self.store.status(self.mid)['blocker'], 'BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE')

    def test_inactive_worker_cannot_fabricate_next_step_operation(self):
        fixture = deepcopy(self.fixture)
        for metadata in fixture['request']['member_metadata'].values():
            metadata['worker_active'] = False
        fixture['request']['manifest']['version'] = 2
        self.store.prepare_manifest(**fixture['request'], upgrade=True)
        bridge = SyntheticHostBridge(fixture, self.contexts)
        host = TasklineHost(self.root / 'state', transport=bridge.transport, bridge=bridge, clock=lambda: 1000)
        bridge.host = host
        host.loop(self.mid)
        self.assertEqual(self.store.status(self.mid)['blocker'], 'BLOCKED_NEXT_STEP_QUERY_UNAVAILABLE')
        self.assertEqual(sum(n == 't3_thread_organize' for n, _ in bridge.service.calls), 0)

    def test_revoked_session_mid_loop_stops_and_cannot_resume_same_identity(self):
        self.bridge.on_pump = lambda bridge: setattr(bridge, 'verified', False)
        with self.assertRaisesRegex(Blocked, 'UNVERIFIED'):
            self.host.loop(self.mid)
        self.assertEqual(self.host.status(self.mid)['status'], 'INVALIDATED')
        self.assertEqual(self.bridge.active, 0)
        self.bridge.verified = True
        self.bridge.on_pump = None
        with self.assertRaisesRegex(Blocked, 'RETIRED'):
            self.host.loop(self.mid, resume=True)

    def test_successor_replacement_requires_start_new_session_and_retires_old(self):
        self.bridge.publish = False
        self.host.loop(self.mid)
        old_proof = deepcopy(self.bridge.proof)
        updated = deepcopy(self.fixture)
        updated['request']['manifest']['version'] = 2
        for metadata in updated['request']['member_metadata'].values():
            metadata['current_caller'] = 'codex:synthetic-new-session'
        self.store.prepare_manifest(**updated['request'], upgrade=True)
        bridge = SyntheticHostBridge(updated, self.contexts)
        bridge.publish = False
        bridge.proof.update(provider_session_id='session-2', generation=2)
        host = TasklineHost(self.root / 'state', transport=bridge.transport, bridge=bridge, clock=lambda: 1000)
        bridge.host = host
        with self.assertRaisesRegex(Blocked, 'SESSION_CHANGED'):
            host.loop(self.mid, resume=True)
        host.loop(self.mid)
        journal = host.journal.load(self.mid)
        self.assertIn(session_digest(old_proof), journal['retired'])
        # Even a defective bridge echoing a previously revoked proof cannot
        # restore the journal's retired host owner or routing loop.
        rollback = deepcopy(updated)
        rollback['request']['manifest']['version'] = 3
        for metadata in rollback['request']['member_metadata'].values():
            metadata['current_caller'] = old_proof['native_caller']
        self.store.prepare_manifest(**rollback['request'], upgrade=True)
        bridge.fixture = rollback
        bridge.proof = old_proof
        with self.assertRaisesRegex(Blocked, 'RETIRED'):
            host.loop(self.mid, resume=True)
        self.assertEqual(bridge.active, 0)

    def test_ready_manifest_is_not_a_signed_host_session_or_complete_inventory(self):
        state = self.store.status(self.mid)
        proof = deepcopy(self.bridge.proof)
        proof['self_signed_ready'] = True
        self.bridge.resolve_session = lambda: proof
        self.bridge.verify_session = lambda value: deepcopy(value)
        with self.assertRaisesRegex(Blocked, 'SESSION_PROOF'):
            self.host.loop(self.mid)
        self.assertEqual(self.bridge.enters, 0)
        self.assertEqual(self.host.doctor()['proof_source'], 'BRIDGE_INJECTED_REQUIRES_VALIDATION')
        self.assertEqual(self.host.doctor()['status'], 'BLOCKED')

    def test_transient_bridge_timeout_releases_lease_and_same_session_can_resume(self):
        original = self.bridge.pump_once
        def timeout(*args):
            raise Blocked('BLOCKED_RPC_TIMEOUT')
        self.bridge.pump_once = timeout
        with self.assertRaisesRegex(Blocked, 'RPC_TIMEOUT'):
            self.host.loop(self.mid)
        record = self.host.journal.load(self.mid)
        self.assertEqual(record['phase'], 'STOPPED')
        self.assertNotIn(session_digest(self.bridge.proof), record['retired'])
        self.assertEqual(self.bridge.active, 0)
        self.bridge.pump_once = original
        self.host.loop(self.mid, resume=True)
        self.assertEqual(self.host.status(self.mid)['status'], 'STOPPED')

    def test_observed_env_append_only_never_becomes_verified_session(self):
        host = TasklineHost(self.root / 'observation-only', transport=lambda *a: None, clock=lambda: 1000)
        with patch.dict(os.environ, {'T3CODE_THREAD_ID': 'app-one', 'CODEX_THREAD_ID': 'native-one'}):
            first = host.observe_current_env()
        with patch.dict(os.environ, {'T3CODE_THREAD_ID': 'app-two', 'CODEX_THREAD_ID': 'native-two'}):
            second = host.observe_current_env()
        self.assertEqual(first['trust'], 'observed_current_env')
        self.assertFalse(first['verified_scope'])
        records = json.loads((host.journal.files.root / '.observed-current-env.json').read_text())['observations']
        self.assertEqual(records, [first, second])
        self.assertEqual(host.doctor()['blockers'], ['BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE', 'BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE'])

    def test_snapshot_archive_race_visible_in_component_but_strict_host_blocks(self):
        queries = FixtureQueries(self.fixture)
        queries.ready_all()
        service = SimulatedT3(self.fixture)
        def racing_tool(tool, args):
            if tool == 't3_thread_organize':
                service.threads[service.source]['pendingRequestCount'] = 1
            return service(tool, args)
        # This explicitly documents the current official API's limitation; it
        # does NOT label the best-effort primitive an atomic safe host.
        component = queries.adapter(racing_tool)
        observed = self.store.advance(self.mid, component)
        self.assertEqual(observed['phase'], 'ARCHIVED')
        self.assertEqual(service.threads[service.source]['pendingRequestCount'], 1)
        self.assertFalse(component.safety()['atomic_conditional_archive'])
        strict_store = HandoffStore(self.root / 'strict-state')
        strict_store.prepare_manifest(**self.fixture['request'])
        bridge = SyntheticHostBridge(self.fixture, self.contexts)
        host = TasklineHost(self.root / 'strict-state', transport=bridge.transport,
                            bridge=bridge, clock=lambda: 1000)
        bridge.host = host
        host.loop(self.mid)
        self.assertEqual(strict_store.status(self.mid)['blocker'], 'BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE')
        self.assertEqual(sum(n == 't3_thread_organize' for n, _ in bridge.service.calls), 0)

    def test_strict_host_archive_gate_keeps_window_and_does_not_cross_rpc_boundary(self):
        self.host.loop(self.mid)
        state = self.store.status(self.mid)
        self.assertEqual(state['blocker'], 'BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE')
        self.assertFalse(state['archive_attempted'])
        self.assertFalse(self.bridge.service.threads[self.bridge.service.source]['archived'])
        self.assertFalse(self.host.doctor()['atomic_conditional_archive'])

    def test_late_result_after_archive_still_consumes_original_task_without_new_run(self):
        self.host.loop(self.mid)
        self.assertEqual(self.store.status(self.mid)['blocker'], 'BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE')
        # Simulate an independently completed external archive, not a write by
        # this strict host. Readback may reconcile it while routing continues.
        self.bridge.service.threads[self.bridge.service.source]['archived'] = True
        self.bridge.sequence = 1
        self.host.loop(self.mid, resume=True)
        self.assertEqual(len(self.host.journal.load(self.mid)['events']), 4)
        self.assertEqual(sum(n == 't3_thread_organize' for n, _ in self.bridge.service.calls), 0)
        pairs = [(b['task_id'], b['run_id']) for b in self.fixture['request']['manifest']['bindings']]
        self.assertEqual(pairs, [(b['task_id'], b['run_id']) for b in self.store.status(self.mid)['manifest']['bindings']])
