"""Actual read-only continuation proof over formal-shaped synthetic records."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from portable.core import Blocked
from portable.taskline_handoff import canonical, sha256
from portable.taskline_host import ReadOnlyNextStep, TasklineHost
from taskline_host_checks import HostTests, SyntheticHostBridge


def install_operation(root, binding, proof, *, transfer=True):
    root = Path(root)
    next_run = binding['run_id'] + '-next'
    previous_owner = 'synthetic-original-caller' if transfer else proof['native_caller']
    task = {'task_id': binding['task_id'], 'caller': proof['native_caller'],
            'runs': [binding['run_id'], next_run], 'current_run': next_run, 'status': 'running'}
    if transfer:
        task['transfers'] = [{'old_caller': previous_owner, 'new_caller': proof['native_caller'],
                              'runs': [binding['run_id']], 'at': '2026-11-06T00:00:00Z',
                              'reason': 'Synthetic formally recorded transfer', 'duty_path': '/fixture/duty.md',
                              'duty_sha256': 'a' * 64}]
    rows = {'tasks/' + binding['task_id'] + '.json': task,
            'dispatches/' + binding['run_id'] + '/meta.json': {
                'id': binding['run_id'], 'task_id': binding['task_id'], 'session': previous_owner,
                'session_id': 'synthetic-worker-session', 'status': 'done', 'pid': None},
            'dispatches/' + next_run + '/meta.json': {
                'id': next_run, 'task_id': binding['task_id'], 'session': proof['native_caller'],
                'session_id': 'synthetic-worker-session', 'status': 'running', 'pid': None,
                'started': '2026-11-06T00:00:01Z'},
            'task-claims/' + binding['task_id'] + '.json': {
                'task_id': binding['task_id'], 'run_id': next_run, 'caller': proof['native_caller'],
                'session_id': 'synthetic-worker-session', 'phase': 'running', 'manual_continuation': True,
                'child_pid': 43210 + int(sha256(binding['task_id'])[:4], 16),
                'child_pid_started': 'synthetic-kernel-start'}}
    for relative, value in rows.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical(value))
    return next_run


def kernel_reader_for(root):
    def read(pid):
        claims = [json.loads(p.read_text()) for p in (root / 'task-claims').glob('*.json')]
        claim = next(row for row in claims if row['child_pid'] == pid)
        return {'pid': pid, 'pid_started': 'synthetic-kernel-start',
                'command': '/bin/bash /fixture/codex-task --continue ' + claim['run_id']}
    return read


class NextStepTests(unittest.TestCase):
    def setUp(self):
        self.case = HostTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root / 'operation-fixture'
        self.binding = self.case.fixture['request']['manifest']['bindings'][0]
        self.proof = self.case.bridge.proof
        self.next_run = install_operation(self.root, self.binding, self.proof)
        self.reader = ReadOnlyNextStep(self.root, process_reader=kernel_reader_for(self.root))

    def test_formal_transfer_then_continue_same_task_original_binding_and_new_run(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*.json')}
        first = self.reader.query(self.binding, self.proof)
        second = self.reader.query(self.binding, self.proof)
        self.assertEqual(first, second)
        self.assertEqual(first['branch'], 'transfer_then_continue')
        self.assertEqual(first['previous_run_id'], self.binding['run_id'])
        self.assertEqual(first['next_run_id'], self.next_run)
        self.assertNotIn('command', first['process_identity'])
        self.assertNotIn('codex-task', canonical(first))
        self.assertEqual({str(p): p.read_bytes() for p in self.root.rglob('*.json')}, before)

    def test_same_owner_continue_requires_no_fake_transfer(self):
        install_operation(self.root, self.binding, self.proof, transfer=False)
        self.assertEqual(self.reader.query(self.binding, self.proof)['branch'], 'same_owner_continue')

    def test_missing_audit_wrong_task_unstarted_or_pid_reuse_rejected(self):
        cases = [('tasks/' + self.binding['task_id'] + '.json', lambda v: v.pop('transfers')),
                 ('dispatches/' + self.next_run + '/meta.json', lambda v: v.update(task_id='other-task')),
                 ('dispatches/' + self.next_run + '/meta.json', lambda v: v.update(status='queued')),
                 ('task-claims/' + self.binding['task_id'] + '.json', lambda v: v.update(child_pid_started='reused-pid'))]
        for relative, change in cases:
            with self.subTest(relative=relative):
                install_operation(self.root, self.binding, self.proof)
                path = self.root / relative
                value = json.loads(path.read_text())
                change(value)
                path.write_text(canonical(value))
                with self.assertRaises(Blocked):
                    self.reader.query(self.binding, self.proof)
        install_operation(self.root, self.binding, self.proof)
        trusted_reader = self.reader.process_reader
        for command in ('echo codex-task --continue ' + self.next_run,
                        '/fixture/codex-task --continue ' + self.next_run + '-wrong'):
            with self.subTest(command=command):
                self.reader.process_reader = lambda pid: {**trusted_reader(pid), 'command': command}
                with self.assertRaisesRegex(Blocked, 'PROCESS_UNAVAILABLE'):
                    self.reader.query(self.binding, self.proof)
        self.reader.process_reader = trusted_reader

    def test_inactive_host_queries_existing_operation_and_only_archive_is_blocked(self):
        fixture = deepcopy(self.case.fixture)
        fixture['request']['manifest']['version'] = 2
        for metadata in fixture['request']['member_metadata'].values():
            metadata['worker_active'] = False
        self.case.store.prepare_manifest(**fixture['request'], upgrade=True)
        bridge = SyntheticHostBridge(fixture, self.case.contexts)
        for binding in fixture['request']['manifest']['bindings']:
            install_operation(self.root, binding, bridge.proof)
        bridge.next_step = lambda binding, proof: self.reader.query(binding, proof)
        host = TasklineHost(self.case.root / 'state', transport=bridge.transport, bridge=bridge, clock=lambda: 1000)
        bridge.host = host
        host.loop(self.case.mid, iterations=2)
        state = self.case.store.status(self.case.mid)
        self.assertEqual(state['blocker'], 'BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE')
        self.assertFalse(state['archive_attempted'])
        steps = [r for r in state['receipts'] if r['kind'] == 'next_step_started']
        self.assertEqual(len(steps), 2)
        self.assertTrue(all(r['previous_run_id'] == r['run_id'] and r['next_run_id'] != r['run_id'] for r in steps))
        self.assertEqual(sum(n == 't3_thread_organize' for n, _ in bridge.service.calls), 0)
