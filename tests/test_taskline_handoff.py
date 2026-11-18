"""Synthetic consumer/authority queries only. Never starts a model or real worker."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
import zipfile

from portable.core import Blocked
from portable.taskline_handoff import (CAPABILITIES, HandoffStore, T3Adapter,
                                       canonical, sha256, singleton_manifest, manifest_scope,
                                       handoff_title, parse_handoff_title, utf16_units, TITLE_PREFIX, TITLE_FAMILY)

SOURCE = Path(__file__).resolve().parents[1]
# Support both discovery and the explicit tests.test_taskline_handoff VERIFY entry.
if str(SOURCE / 'tests') not in sys.path:sys.path.insert(0, str(SOURCE / 'tests'))


def request(active=False, scope='engineering'):
    context = hashlib.sha256(b'isolated original context').hexdigest()
    return {
        'binding': {'handoff_id': 'demo-handoff', 'version': 1,
                    'source_thread': {'thread_id': 'old', 'link': 't3://old'},
                    'target_thread': {'thread_id': 'new', 'link': 't3://new'},
                    'task_id': 'original-task', 'run_id': 'original-run',
                    'context_sha256': context},
        'metadata': {'context_ref': {'ref': 'fixture:original-context', 'sha256': context},
                     'authorization_ref': {'ref': 'fixture:existing-authority', 'sha256': 'b' * 64},
                     'current_caller': 'new-real-caller', 'worker_active': active,
                     'scope': scope, 'todos': ['Continue original task verification']}}


class QueriedAdapter:
    """A separate synthetic authority/event consumer, not a ready boolean mock.

    Available receipts and durable query results are deliberately separate so
    tests can expose forged IDs, revocation, ACK-only delivery and stale reads.
    """
    def __init__(self, req, manifest=None):
        self.manifest = deepcopy(manifest or singleton_manifest(req['binding']))
        self.inventory_tasks = [{'task_id': req['binding']['task_id'],
                                 'run_id': req['binding']['run_id']}]
        self.req = deepcopy(req)
        b, m = req['binding'], req['metadata']
        self.calls = []
        self.threads = {key: {'thread_id': key, 'link': 't3://' + key, 'archived': False,
                              'status': 'idle', 'activeRunId': None, 'pendingRequestCount': 0}
                        for key in ('old', 'new')}
        self.authority = {'authorized': True, 'binding_digest': sha256(b),
                          'current_caller': m['current_caller'], 'scope': m['scope'],
                          'worker_active': m['worker_active'],
                          'authorization_ref': deepcopy(m['authorization_ref']),
                          'provenance': 'fixture:authority-query/1'}
        self.offered = []
        self.consumer = {}
        self.archive_error = None
        self.link_error = False
        self.fail_target_after_link = False
        self.revoked_after_link = False
        self.archive_ack_only = False
        self.caps = CAPABILITIES.copy()
        self.original_tasks = ('original-task',)
        self.original_runs = ('original-run',)
        for kind in ('context_read', 'route_consumed', 'next_step_started'):
            receipt = {'receipt_id': kind + '-1', 'kind': kind,
                       'binding_digest': sha256(b), 'handoff_id': b['handoff_id'],
                       'version': b['version'], 'task_id': b['task_id'], 'run_id': b['run_id'],
                       'source_thread_id': 'old', 'target_thread_id': 'new',
                       'provenance': 'fixture:consumer/query/1', **manifest_scope(self.manifest)}
            if kind == 'context_read':
                receipt['context_sha256'] = b['context_sha256']
            if kind == 'route_consumed':
                receipt.update(event_id='probe-1', payload_sha256=sha256({'probe': 1}), persistent_route=True)
            if kind == 'next_step_started':
                receipt['operation_id'] = 'same-task-operation-1'
            self.offered.append(receipt)
            self.consumer[receipt['receipt_id']] = deepcopy(receipt)

    def capabilities(self):
        return self.caps

    def verify_inventory(self, manifest):
        self.calls.append('inventory_query')
        return {'complete': True, **manifest_scope(self.manifest), 'source_thread_id': 'old',
                'task_runs': deepcopy(self.inventory_tasks), 'provenance': 'fixture:inventory-query/1'}

    def verify_authorization(self, binding, ref):
        self.calls.append('authority_query')
        return deepcopy(self.authority)

    def fetch_receipts(self, binding):
        self.calls.append('fetch_receipts')
        return deepcopy(self.offered)

    def verify_receipt(self, binding, receipt):
        self.calls.append('query:' + receipt['receipt_id'])
        return deepcopy(self.consumer.get(receipt['receipt_id']))

    def read_thread(self, thread_id):
        self.calls.append('read:' + thread_id)
        return deepcopy(self.threads.get(thread_id))

    def ensure_successor_link(self, binding, idempotency_key):
        assert set(binding) == set(self.req['binding'])
        binding = self.manifest
        self.calls.append('link')
        if self.link_error:
            raise TimeoutError('synthetic private transport text')
        self.threads['old'].update(successor_link=binding['target_thread']['link'],
                                   successor_manifest_id=binding['manifest_id'],
                                   successor_manifest_version=binding['version'],
                                   successor_manifest_digest=sha256(binding))
        if self.fail_target_after_link:
            self.threads['new']['archived'] = True
        if self.revoked_after_link:
            self.authority['authorized'] = False
        return {'delivery': 'accepted'}

    def archive_thread(self, thread_id, idempotency_key):
        self.calls.append('archive')
        if self.archive_error == 'before':
            raise OSError('synthetic private transport text')
        if not self.archive_ack_only:
            self.threads[thread_id]['archived'] = True
        if self.archive_error == 'after':
            raise TimeoutError('synthetic private transport text')
        return {'accepted': True}

    def mutate_receipt(self, index, key, value):
        self.offered[index][key] = value
        self.consumer[self.offered[index]['receipt_id']] = deepcopy(self.offered[index])


class MultiQueriedAdapter(QueriedAdapter):
    def __init__(self, requests, manifest):
        super().__init__(requests[0], manifest)
        self.members = {r['binding']['handoff_id']: QueriedAdapter(r, manifest) for r in requests}
        self.inventory_tasks = [{'task_id': r['binding']['task_id'], 'run_id': r['binding']['run_id']}
                                for r in requests]

    def verify_authorization(self, binding, ref):
        self.calls.append('authority:' + binding['handoff_id'])
        return self.members[binding['handoff_id']].verify_authorization(binding, ref)

    def fetch_receipts(self, binding):
        self.calls.append('fetch:' + binding['handoff_id'])
        return self.members[binding['handoff_id']].fetch_receipts(binding)

    def verify_receipt(self, binding, receipt):
        self.calls.append('query:' + binding['handoff_id'] + ':' + receipt['receipt_id'])
        return self.members[binding['handoff_id']].verify_receipt(binding, receipt)


def two_members():
    first, second = request(), request(active=True)
    second['binding'].update(handoff_id='second-handoff', task_id='second-task', run_id='second-run')
    manifest = {'manifest_id': 'window-handoff', 'version': 1,
                'source_thread': first['binding']['source_thread'],
                'target_thread': first['binding']['target_thread'],
                'bindings': [first['binding'], second['binding']]}
    metadata = {r['binding']['handoff_id']: r['metadata'] for r in (first, second)}
    return first, second, manifest, metadata


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.req = request()
        self.store = HandoffStore(self.root / 'state')
        self.store.prepare(**self.req)
        self.adapter = QueriedAdapter(self.req)

    def advance(self):
        return self.store.advance('demo-handoff', self.adapter)

    def assert_blocked(self, reason=None):
        result = self.advance()
        self.assertNotEqual(result['phase'], 'ARCHIVED')
        if reason:
            self.assertEqual(result['blocker'], reason)
        self.assertNotIn('archive', self.adapter.calls)
        self.assertFalse(self.adapter.threads['old']['archived'])
        return result

    def test_success_chain_queries_then_link_readback_recheck_archive_readback(self):
        result = self.advance()
        self.assertEqual(result['phase'], 'ARCHIVED')
        calls = self.adapter.calls
        self.assertEqual(calls[:3], ['inventory_query', 'authority_query', 'fetch_receipts'])
        self.assertLess(calls.index('query:context_read-1'), calls.index('link'))
        link = calls.index('link')
        self.assertEqual(calls[link + 1:link + 3], ['read:old', 'inventory_query'])
        self.assertEqual(calls[-3:], ['read:old', 'archive', 'read:old'])
        self.assertEqual(calls.count('authority_query'), 2)
        self.assertIsNotNone(result['archive_intent'])
        self.assertEqual(self.adapter.original_tasks, ('original-task',))
        self.assertEqual(self.adapter.original_runs, ('original-run',))
        self.assertFalse(any('launch' in c or 'restart' in c or 'create' in c for c in calls))

    def test_intent_saved_before_link_or_archive(self):
        original = self.adapter.ensure_successor_link
        def checked(binding, key):
            state = HandoffStore(self.root / 'state').load('demo-handoff')
            self.assertEqual(state['phase'], 'ARCHIVE_PENDING')
            self.assertEqual(state['archive_intent']['manifest_digest'], sha256(self.adapter.manifest))
            return original(binding, key)
        self.adapter.ensure_successor_link = checked
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')

    def test_missing_adapter_explicit_block_and_recovery(self):
        result = self.store.advance('demo-handoff')
        self.assertEqual(result['blocker'], 'BLOCKED_CAPABILITY')
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')

    def test_missing_each_capability_prevents_archive(self):
        for cap in CAPABILITIES:
            with self.subTest(cap=cap):
                self.adapter.caps = CAPABILITIES - {cap}
                self.assert_blocked('BLOCKED_CAPABILITY')

    def test_no_boolean_authority_or_wrong_identity(self):
        for field, value in [('authorized', False), ('binding_digest', 'c' * 64),
                             ('current_caller', 'old-caller'), ('authorization_ref', {}),
                             ('worker_active', True), ('scope', 'read_only'), ('provenance', '')]:
            with self.subTest(field=field):
                self.adapter = QueriedAdapter(self.req)
                self.adapter.authority[field] = value
                self.assert_blocked('BLOCKED_AUTHORITY')
        self.adapter.authority = True
        self.assert_blocked('BLOCKED_AUTHORITY')

    def test_missing_evidence_never_archives(self):
        for kind in ('context_read', 'route_consumed', 'next_step_started'):
            with self.subTest(kind=kind):
                self.adapter = QueriedAdapter(self.req)
                self.adapter.offered = [r for r in self.adapter.offered if r['kind'] != kind]
                self.assert_blocked('BLOCKED_MISSING_EVIDENCE')

    def test_receipts_need_live_query_not_id_or_hash_or_ack(self):
        for query in (None, True, {'delivery': 'accepted'}):
            self.adapter.consumer['context_read-1'] = query
            self.assert_blocked('UNVERIFIED_RECEIPT')

    def test_wrong_binding_fields_rejected_even_if_query_confirms_them(self):
        changes = [('version', 2), ('version', True), ('handoff_id', 'another'),
                   ('binding_digest', 'd' * 64), ('task_id', 'new-task'), ('run_id', 'new-run'),
                   ('source_thread_id', 'other'), ('target_thread_id', 'other'),
                   ('context_sha256', 'd' * 64)]
        for key, value in changes:
            with self.subTest(field=key, value=value):
                self.adapter = QueriedAdapter(self.req)
                self.adapter.mutate_receipt(0, key, value)
                self.assert_blocked()

    def test_bad_receipt_provenance_kind_route_payload_and_operation(self):
        for index, key, value in [(0, 'receipt_id', ''), (0, 'provenance', ''),
                                  (0, 'kind', 'ready'), (1, 'event_id', ''),
                                  (1, 'payload_sha256', 'bad'), (2, 'operation_id', '')]:
            with self.subTest(field=key):
                self.adapter = QueriedAdapter(self.req)
                self.adapter.mutate_receipt(index, key, value)
                self.assert_blocked()

    def test_wrong_event_or_payload_does_not_match_consumed_event_query(self):
        for key, value in [('event_id', 'unconsumed'), ('payload_sha256', 'f' * 64)]:
            self.adapter = QueriedAdapter(self.req)
            self.adapter.offered[1][key] = value
            self.assert_blocked('UNVERIFIED_RECEIPT')

    def test_new_window_failure_and_failure_after_link(self):
        self.adapter.threads['new'] = None
        self.assert_blocked('BLOCKED_SUCCESSOR_UNAVAILABLE')
        self.adapter = QueriedAdapter(self.req)
        self.adapter.fail_target_after_link = True
        self.assert_blocked('BLOCKED_SUCCESSOR_UNAVAILABLE')

    def test_authority_revoked_between_ready_and_archive(self):
        self.adapter.revoked_after_link = True
        self.assert_blocked('BLOCKED_AUTHORITY')

    def test_link_send_acceptance_is_not_readback(self):
        self.adapter.ensure_successor_link = lambda *args: {'accepted': True}
        self.assert_blocked('BLOCKED_LINK_READBACK')

    def test_archive_ack_is_not_archived(self):
        self.adapter.archive_ack_only = True
        result = self.advance()
        self.assertEqual(result['phase'], 'ARCHIVE_PENDING')
        self.assertEqual(result['blocker'], 'BLOCKED_ARCHIVE_READBACK')
        self.adapter.archive_ack_only = False
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')

    def test_archive_failure_retry_first_reads_old_no_relink_or_worker_restart(self):
        self.adapter.archive_error = 'before'
        failed = self.advance()
        self.assertEqual(failed['phase'], 'ARCHIVE_PENDING')
        self.assertEqual(failed['blocker'], 'BLOCKED_ADAPTER_ERROR')
        self.assertNotIn('synthetic private', canonical(failed))
        self.store = HandoffStore(self.root / 'state')
        self.adapter.calls.clear()
        self.adapter.archive_error = None
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')
        self.assertEqual(self.adapter.calls[0], 'read:old')
        self.assertNotIn('link', self.adapter.calls)
        self.assertEqual(self.adapter.calls.count('archive'), 1)

    def test_unknown_archive_result_readback_no_duplicate_archive(self):
        self.adapter.archive_error = 'after'
        self.assertEqual(self.advance()['phase'], 'ARCHIVE_PENDING')
        self.adapter.calls.clear()
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')
        self.assertEqual(self.adapter.calls[0], 'read:old')
        self.assertNotIn('archive', self.adapter.calls)
        self.assertNotIn('link', self.adapter.calls)

    def test_unknown_link_result_reads_back_before_repeating(self):
        original = self.adapter.ensure_successor_link
        def uncertain(*args):
            original(*args)
            raise TimeoutError()
        self.adapter.ensure_successor_link = uncertain
        self.assertEqual(self.advance()['phase'], 'ARCHIVE_PENDING')
        self.adapter.calls.clear()
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')
        self.assertEqual(self.adapter.calls[0], 'read:old')
        self.assertNotIn('link', self.adapter.calls)

    def test_link_failure_recoverable_without_new_window(self):
        self.adapter.link_error = True
        self.assert_blocked('BLOCKED_ADAPTER_ERROR')
        self.adapter.link_error = False
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')
        self.assertEqual(set(self.adapter.threads), {'old', 'new'})

    def test_duplicate_receipts_idempotent_terminal_and_late_dispatch_result(self):
        self.adapter.offered.append(deepcopy(self.adapter.offered[0]))
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')
        self.assertEqual(len(self.store.status('demo-handoff')['receipts']), 3)
        before = deepcopy(self.adapter.calls)
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')
        self.assertEqual(before, self.adapter.calls)
        # Dispatch retains task/run ownership and its durable consumer after
        # archive. Portable never closes/deletes/routes its late-event outbox.
        late = {'event_id': 'late-result', 'task_id': 'original-task', 'run_id': 'original-run'}
        self.adapter.consumer[late['event_id']] = late
        self.assertEqual(self.store.status('demo-handoff')['phase'], 'ARCHIVED')
        self.assertEqual(self.adapter.consumer['late-result'], late)
        self.assertEqual(self.adapter.original_tasks, ('original-task',))

    def test_live_route_query_requires_actual_consumption_and_survives_archive(self):
        # Independent durable synthetic dispatch service. A delivery ACK does
        # not create a consumer record. Its journal remains outside portable.
        journal = self.root / 'dispatch-route.json'
        payload = {'probe': 1}
        route = {'binding': self.req['binding'], 'target': 'new',
                 'events': {'probe-1': payload}, 'consumed': {}, 'deliveries': []}
        journal.write_text(canonical(route))
        original_query = self.adapter.verify_receipt
        def query(binding, receipt):
            if receipt['kind'] != 'route_consumed':
                return original_query(binding, receipt)
            observed = json.loads(journal.read_text())
            event_id = receipt['event_id']
            if (observed['binding'] != binding or
                    observed['consumed'].get(event_id) != binding['target_thread']['thread_id'] or
                    event_id not in observed['events'] or
                    sha256(observed['events'][event_id]) != receipt['payload_sha256']):
                return None
            return deepcopy(receipt)
        self.adapter.verify_receipt = query
        self.assert_blocked('UNVERIFIED_RECEIPT')
        route['consumed']['probe-1'] = 'new'
        journal.write_text(canonical(route))
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')
        archived_snapshot = (self.root / 'state/demo-handoff.json').read_bytes()
        # A late result is produced after portable archived. Restart the
        # independent router from its disk journal and deliver to the same
        # binding; no archived flag is consulted or written in dispatch.
        late = json.loads(journal.read_text())
        late['events']['late-1'] = {'task_id': 'original-task', 'run_id': 'original-run',
                                    'result': 'synthetic completion'}
        journal.write_text(canonical(late))
        for _ in range(2):
            reopened = json.loads(journal.read_text())
            for event_id, result in reopened['events'].items():
                if event_id in reopened['consumed']:
                    continue
                self.assertEqual(result['task_id'], reopened['binding']['task_id'])
                self.assertEqual(result['run_id'], reopened['binding']['run_id'])
                reopened['deliveries'].append({'event_id': event_id, 'target': reopened['target'],
                                                'payload_sha256': sha256(result)})
                reopened['consumed'][event_id] = reopened['target']
            journal.write_text(canonical(reopened))
        delivered = json.loads(journal.read_text())
        self.assertEqual(delivered['deliveries'], [{'event_id': 'late-1', 'target': 'new',
                           'payload_sha256': sha256(late['events']['late-1'])}])
        self.assertEqual(HandoffStore(self.root / 'state').status('demo-handoff')['phase'], 'ARCHIVED')
        self.assertEqual((self.root / 'state/demo-handoff.json').read_bytes(), archived_snapshot)

    def test_active_worker_only_context_and_persistent_route_zero_launches(self):
        req = request(active=True)
        store = HandoffStore(self.root / 'active')
        store.prepare(**req)
        adapter = QueriedAdapter(req)
        adapter.offered = adapter.offered[:2]
        self.assertEqual(store.advance('demo-handoff', adapter)['phase'], 'ARCHIVED')
        self.assertFalse(any('launch' in c or 'restart' in c for c in adapter.calls))
        store = HandoffStore(self.root / 'active-bad')
        store.prepare(**req)
        adapter.mutate_receipt(1, 'persistent_route', False)
        self.assertEqual(store.advance('demo-handoff', adapter)['blocker'], 'PERSISTENT_ROUTE_UNVERIFIED')

    def test_paused_and_readonly_never_query_or_launch_engineering(self):
        for scope in ('paused', 'read_only'):
            req = request(scope=scope)
            store = HandoffStore(self.root / scope)
            store.prepare(**req)
            adapter = QueriedAdapter(req)
            state = store.advance('demo-handoff', adapter)
            self.assertEqual(state['blocker'], 'BLOCKED_SCOPE_' + scope.upper())
            self.assertEqual(adapter.calls, [])
            self.assertIsNone(state['archive_intent'])

    def test_prepare_idempotence_conflict_upgrade_invalidates_old_evidence(self):
        first = self.store.status('demo-handoff')
        self.assertEqual(self.store.prepare(**self.req), first)
        req = deepcopy(self.req)
        req['metadata']['todos'].append('different')
        with self.assertRaises(Blocked):
            self.store.prepare(**req)
        req['binding']['version'] = 2
        with self.assertRaises(Blocked):
            self.store.prepare(**req)
        upgraded = self.store.prepare(**req, upgrade=True)
        self.assertEqual(upgraded['phase'], 'PREPARED')
        self.assertEqual(upgraded['receipts'], [])
        self.assertEqual(len(upgraded['history']), 1)
        self.adapter.authority['binding_digest'] = sha256(req['binding'])
        self.adapter.manifest = singleton_manifest(req['binding'])
        self.assert_blocked('RECEIPT_BINDING_MISMATCH')

    def test_busy_source_without_archive_rpc_can_upgrade_and_recover(self):
        self.adapter.threads['old']['activeRunId'] = 'still-writing'
        blocked = self.assert_blocked('BLOCKED_SOURCE_BUSY_OR_UNKNOWN')
        self.assertIsNotNone(blocked['archive_intent'])
        self.assertFalse(blocked['archive_attempted'])
        self.assertEqual(blocked['archive_outcome'], 'NOT_ATTEMPTED')
        self.store = HandoffStore(self.root / 'state')
        manifest = deepcopy(blocked['manifest'])
        manifest['version'] = 2
        upgraded = self.store.prepare_manifest(manifest, blocked['member_metadata'], upgrade=True)
        self.assertEqual(upgraded['receipts'], [])
        self.assertIsNone(upgraded['archive_intent'])
        self.assertFalse(upgraded['archive_attempted'])
        self.assertEqual(upgraded['history'][-1]['blocker'], 'BLOCKED_SOURCE_BUSY_OR_UNKNOWN')
        fresh = QueriedAdapter(self.req, manifest)
        result = self.store.advance('demo-handoff', fresh)
        self.assertEqual(result['phase'], 'ARCHIVED')
        self.assertEqual(result['archive_outcome'], 'ARCHIVED')
        self.assertEqual(fresh.calls.count('archive'), 1)
        self.assertNotIn('archive', self.adapter.calls)

    def test_inventory_changes_after_link_upgrade_fresh_full_evidence_one_archive(self):
        first, second, manifest, metadata = two_members()
        original = self.adapter.ensure_successor_link
        def changed_inventory(binding, key):
            result = original(binding, key)
            self.adapter.inventory_tasks.append({'task_id': second['binding']['task_id'],
                                                  'run_id': second['binding']['run_id']})
            return result
        self.adapter.ensure_successor_link = changed_inventory
        blocked = self.assert_blocked('BLOCKED_INVENTORY_MISMATCH')
        self.assertIn('link', self.adapter.calls)
        self.assertFalse(blocked['archive_attempted'])
        self.store = HandoffStore(self.root / 'state')
        manifest.update(manifest_id='demo-handoff', version=2)
        upgraded = self.store.prepare_manifest(manifest, metadata, upgrade=True)
        self.assertEqual(upgraded['receipts'], [])
        self.assertIsNone(upgraded['archive_intent'])
        fresh = MultiQueriedAdapter([first, second], manifest)
        fresh.threads['old'] = deepcopy(self.adapter.threads['old'])
        # Old member's receipts remain queryable, but cannot be relabelled to
        # the new manifest. Demonstrate rejection before obtaining fresh ones.
        fresh.members['demo-handoff'].offered = deepcopy(self.adapter.offered)
        fresh.members['demo-handoff'].consumer = deepcopy(self.adapter.consumer)
        rejected = self.store.advance('demo-handoff', fresh)
        self.assertEqual(rejected['blocker'], 'RECEIPT_BINDING_MISMATCH')
        self.assertNotIn('archive', fresh.calls)
        fresh.members['demo-handoff'] = QueriedAdapter(first, manifest)
        recovered = self.store.advance('demo-handoff', fresh)
        self.assertEqual(recovered['phase'], 'ARCHIVED')
        self.assertEqual(fresh.calls.count('archive'), 1)
        self.assertNotIn('archive', self.adapter.calls)
        self.assertEqual(fresh.threads['old']['successor_manifest_digest'], sha256(manifest))
        self.assertEqual(len(recovered['receipts']), 6)

    def test_archive_attempt_unknown_is_persisted_before_rpc(self):
        archive = self.adapter.archive_thread
        def checked(thread_id, key):
            snapshot = HandoffStore(self.root / 'state').load('demo-handoff')
            self.assertTrue(snapshot['archive_attempted'])
            self.assertEqual(snapshot['archive_outcome'], 'UNKNOWN')
            self.assertEqual(snapshot['archive_intent']['idempotency_key'] + ':archive', key)
            return archive(thread_id, key)
        self.adapter.archive_thread = checked
        self.assertEqual(self.advance()['archive_outcome'], 'ARCHIVED')

    def test_unknown_archive_read_failure_stays_frozen_until_confirmed_readback(self):
        self.adapter.archive_error = 'after'
        unknown = self.advance()
        self.assertTrue(unknown['archive_attempted'])
        self.assertEqual(unknown['archive_outcome'], 'UNKNOWN')
        self.store = HandoffStore(self.root / 'state')
        manifest = deepcopy(unknown['manifest'])
        manifest['version'] = 2
        with self.assertRaisesRegex(Blocked, 'VERSION_FROZEN'):
            self.store.prepare_manifest(manifest, unknown['member_metadata'], upgrade=True)
        read = self.adapter.read_thread
        def unavailable(thread_id):
            self.adapter.calls.append('read-failed:' + thread_id)
            raise TimeoutError()
        self.adapter.read_thread = unavailable
        self.adapter.calls.clear()
        failed = self.advance()
        self.assertEqual(failed['archive_outcome'], 'UNKNOWN')
        self.assertEqual(self.adapter.calls, ['read-failed:old'])
        with self.assertRaisesRegex(Blocked, 'VERSION_FROZEN'):
            self.store.prepare_manifest(manifest, unknown['member_metadata'], upgrade=True)
        self.adapter.read_thread = read
        self.adapter.calls.clear()
        confirmed = self.advance()
        self.assertEqual(confirmed['phase'], 'ARCHIVED')
        self.assertEqual(confirmed['archive_outcome'], 'ARCHIVED')
        self.assertEqual(self.adapter.calls, ['read:old'])

    def test_false_readback_keeps_unknown_frozen_even_when_source_now_busy(self):
        self.adapter.archive_error = 'before'
        unknown = self.advance()
        self.assertEqual(unknown['archive_outcome'], 'UNKNOWN')
        manifest = deepcopy(unknown['manifest'])
        manifest['version'] = 2
        self.adapter.threads['old']['pendingRequestCount'] = 1
        self.adapter.calls.clear()
        self.store = HandoffStore(self.root / 'state')
        blocked = self.advance()
        self.assertEqual(self.adapter.calls, ['read:old'])
        self.assertEqual(blocked['blocker'], 'BLOCKED_SOURCE_BUSY_OR_UNKNOWN')
        self.assertTrue(blocked['archive_attempted'])
        self.assertEqual(blocked['archive_outcome'], 'UNKNOWN')
        self.assertEqual(self.store.status('demo-handoff')['archive_outcome'], 'UNKNOWN')
        with self.assertRaisesRegex(Blocked, 'VERSION_FROZEN'):
            self.store.prepare_manifest(manifest, blocked['member_metadata'], upgrade=True)
        # Retrying the SAME immutable manifest remains allowed.
        self.adapter.threads['old']['pendingRequestCount'] = 0
        self.adapter.archive_error = None
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')

    def test_old_candidate_intent_without_boundary_is_conservatively_reconciled(self):
        self.adapter.archive_error = 'after'
        self.advance()
        path = self.root / 'state/demo-handoff.json'
        value = json.loads(path.read_text())
        value.pop('archive_attempted')
        value.pop('archive_outcome')
        path.write_text(canonical(value))
        self.store = HandoffStore(self.root / 'state')
        manifest = deepcopy(value['manifest'])
        manifest['version'] = 2
        with self.assertRaisesRegex(Blocked, 'VERSION_FROZEN'):
            self.store.prepare_manifest(manifest, value['member_metadata'], upgrade=True)
        self.adapter.calls.clear()
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')
        self.assertEqual(self.adapter.calls, ['read:old'])

    def test_no_version_change_during_unknown_external_archive(self):
        self.adapter.archive_error = 'after'
        self.advance()
        req = deepcopy(self.req)
        req['binding']['version'] = 2
        with self.assertRaisesRegex(Blocked, 'VERSION_FROZEN'):
            self.store.prepare(**req, upgrade=True)

    def test_prepare_rejects_raw_ready_paths_bad_hashes_extra_binding(self):
        for mutate in (lambda r: r['binding'].update(ready=True),
                       lambda r: r['binding'].update(handoff_id='../escape'),
                       lambda r: r['binding'].update(version=True),
                       lambda r: r['metadata'].update(ready=True),
                       lambda r: r['metadata']['context_ref'].update(sha256='e' * 64)):
            req = request()
            mutate(req)
            with self.assertRaises(Blocked):
                self.store.prepare(**req)
        self.assertFalse((self.root / 'escape.json').exists())

    def test_state_restart_persists_package_and_original_binding(self):
        reopened = HandoffStore(self.root / 'state')
        state = reopened.status('demo-handoff')
        self.assertEqual(state['binding'], self.req['binding'])
        self.assertEqual(state['metadata'], self.req['metadata'])
        self.assertEqual(reopened.advance('demo-handoff', self.adapter)['phase'], 'ARCHIVED')

    def test_symlink_root_state_lock_and_permissive_directory_fail_closed(self):
        (self.root / 'alias').symlink_to(self.root / 'state', target_is_directory=True)
        with self.assertRaises(Blocked):
            HandoffStore(self.root / 'alias')
        target = self.root / 'untouched'
        target.write_text('original')
        path = self.root / 'state/demo-handoff.json'
        path.rename(self.root / 'original-state.json')
        path.symlink_to(target)
        with self.assertRaises(OSError):
            self.store.status('demo-handoff')
        self.assertEqual(target.read_text(), 'original')
        (self.root / 'state/.lock').rename(self.root / 'original-lock')
        (self.root / 'state/.lock').symlink_to(target)
        with self.assertRaises(OSError):
            self.store.status('demo-handoff')
        unsafe = self.root / 'unsafe'
        unsafe.mkdir()
        unsafe.chmod(0o777)
        with self.assertRaises(Blocked):
            HandoffStore(unsafe)

    def test_parallel_duplicate_prepares_and_advances_one_archive(self):
        def prepare(_):
            return HandoffStore(self.root / 'state').prepare(**self.req)
        with ThreadPoolExecutor(max_workers=4) as pool:
            prepared = list(pool.map(prepare, range(8)))
        self.assertTrue(all(v == prepared[0] for v in prepared))
        def advance(_):
            return HandoffStore(self.root / 'state').advance('demo-handoff', self.adapter)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(advance, range(8)))
        self.assertTrue(all(v['phase'] == 'ARCHIVED' for v in results))
        self.assertEqual(self.adapter.calls.count('archive'), 1)
        self.assertEqual(self.adapter.calls.count('link'), 1)

    def test_corrupt_snapshot_and_wrong_thread_readback_fail_closed(self):
        self.adapter.threads['old']['link'] = 't3://wrong'
        self.assert_blocked('BLOCKED_SOURCE_READBACK')
        path = self.root / 'state/demo-handoff.json'
        value = json.loads(path.read_text())
        value['binding']['run_id'] = 'tampered'
        path.write_text(canonical(value))
        with self.assertRaisesRegex(Blocked, 'CORRUPT_STATE'):
            self.store.status('demo-handoff')

    def test_t3_tools_without_query_capabilities_block(self):
        calls = []
        adapter = T3Adapter(lambda *args: calls.append(args))
        state = self.store.advance('demo-handoff', adapter)
        self.assertEqual(state['blocker'], 'BLOCKED_CAPABILITY')
        self.assertEqual(calls, [])

    def t3_fixture(self, envelope='raw'):
        calls, updates = [], []
        for thread in self.adapter.threads.values():
            thread.setdefault('title', '合成标题 🧪')
        def tool(name, args):
            calls.append((name, args))
            if name == 't3_thread_read':
                thread = self.adapter.read_thread(args['threadId'])
                thread = {k: v for k, v in thread.items() if not k.startswith('successor_')}
                payload = {'thread': thread}  # no invented typed item
                if envelope == 'text':
                    return {'content': [{'type': 'text', 'text': canonical(payload)}]}
                if envelope == 'structured':
                    return {'structuredContent': {'result': payload}}
                return payload
            if name == 't3_thread_update':
                self.assertEqual(args['action'], 'rename')
                updates.append(deepcopy(args))
                self.adapter.calls.append('link')
                self.adapter.threads[args['threadId']]['title'] = args['title']
                return {'result': {'accepted': True}}
            if name == 't3_thread_organize':
                self.adapter.threads[args['threadId']]['archived'] = True
                return {'result': {'accepted': True}}
            self.fail('unexpected tool (including thread_send/worker restart)')
        def receipt_query(binding, rid):
            return self.adapter.fetch_receipts(binding) if rid is None else deepcopy(self.adapter.consumer[rid])
        adapter = T3Adapter(tool, authority_query=self.adapter.verify_authorization,
                            receipt_query=receipt_query, inventory_query=self.adapter.verify_inventory)
        return adapter, calls, updates

    def test_t3_injected_transport_raw_envelopes_and_passive_link_no_send(self):
        for envelope in ('raw', 'text', 'structured'):
            with self.subTest(envelope=envelope):
                store = HandoffStore(self.root / ('t3-' + envelope))
                store.prepare(**self.req)
                self.adapter = QueriedAdapter(self.req)
                adapter, calls, _ = self.t3_fixture(envelope)
                self.assertEqual(store.advance('demo-handoff', adapter)['phase'], 'ARCHIVED')
                self.assertFalse(any(name == 't3_thread_send' for name, _ in calls))
                self.assertEqual(calls[-2], ('t3_thread_organize', {'action': 'archive', 'threadId': 'old'}))
                self.assertEqual(calls[-1], ('t3_thread_read', {'threadId': 'old'}))

    def test_t3_rename_unavailable_blocks_without_send_or_archive(self):
        adapter, calls, _ = self.t3_fixture()
        tool = adapter.tool_call
        def unavailable(name, args):
            if name == 't3_thread_update':
                raise Blocked('BLOCKED_CAPABILITY_TITLE_RENAME')
            return tool(name, args)
        adapter.tool_call = unavailable
        state = self.store.advance('demo-handoff', adapter)
        self.assertEqual(state['blocker'], 'BLOCKED_CAPABILITY_TITLE_RENAME')
        self.assertFalse(any(name in ('t3_thread_send', 't3_thread_organize') for name, _ in calls))

    def test_t3_rename_acceptance_cannot_synthesize_title_readback(self):
        adapter, calls, _ = self.t3_fixture()
        tool = adapter.tool_call
        def ack_only(name, args):
            if name == 't3_thread_update':
                return {'accepted': True, 'title': args['title']}
            return tool(name, args)
        adapter.tool_call = ack_only
        state = self.store.advance('demo-handoff', adapter)
        self.assertEqual(state['blocker'], 'BLOCKED_LINK_READBACK')
        self.assertFalse(any(name == 't3_thread_organize' for name, _ in calls))

    def test_t3_error_envelopes_and_unknown_fields_fail_closed(self):
        for raw in ({'isError': True, 'structuredContent': {'thread': {}}},
                    {'result': {'error': 'private synthetic error'}},
                    {'content': [{'type': 'text', 'text': 'not json'}]},
                    {'structuredContent': {'thread': {'id': 'new', 'archived': False}, 'items': []}}):
            with self.subTest(raw=raw):
                adapter, calls, _ = self.t3_fixture()
                adapter.tool_call = lambda *args: deepcopy(raw)
                state = self.store.advance('demo-handoff', adapter)
                self.assertNotEqual(state['phase'], 'ARCHIVED')
                self.assertEqual(calls, [])
                self.assertNotIn('private synthetic error', canonical(state))

    def test_already_archived_recovery_records_fact_without_new_ready_or_write(self):
        self.adapter.archive_error = 'after'
        self.assertEqual(self.advance()['phase'], 'ARCHIVE_PENDING')
        self.adapter.caps = {'thread_read'}
        self.adapter.threads['new']['status'] = 'failed'
        self.adapter.consumer.clear()
        self.adapter.calls.clear()
        self.store = HandoffStore(self.root / 'state')
        result = self.advance()
        self.assertEqual(result['phase'], 'ARCHIVED')
        self.assertEqual(result['receipts'], [])
        self.assertEqual(self.adapter.calls, ['read:old'])

    def test_t3_raw_busy_source_and_failed_successor_do_not_write(self):
        for thread, field, value in [('old', 'activeRunId', 'still-writing'),
                                     ('old', 'pendingRequestCount', 1),
                                     ('new', 'status', 'failed')]:
            with self.subTest(thread=thread, field=field):
                self.adapter = QueriedAdapter(self.req)
                self.adapter.threads[thread][field] = value
                adapter, calls, items = self.t3_fixture(envelope='text')
                state = self.store.advance('demo-handoff', adapter)
                self.assertNotEqual(state['phase'], 'ARCHIVED')
                self.assertEqual(items, [])
                self.assertNotIn('link', self.adapter.calls)
                self.assertFalse(any(name in ('t3_thread_send', 't3_thread_organize') for name, _ in calls))

    def test_t3_archive_error_envelope_retries_by_actual_readback(self):
        adapter, calls, items = self.t3_fixture()
        tool = adapter.tool_call
        def uncertain(name, args):
            result = tool(name, args)
            if name == 't3_thread_organize':
                return {'isError': True, 'content': [{'type': 'text', 'text': 'synthetic private error'}]}
            return result
        adapter.tool_call = uncertain
        result = self.store.advance('demo-handoff', adapter)
        self.assertEqual(result['phase'], 'ARCHIVE_PENDING')
        self.assertEqual(result['blocker'], 'BLOCKED_T3_TOOL_ERROR')
        self.assertNotIn('synthetic private error', canonical(result))
        calls.clear()
        self.assertEqual(self.store.advance('demo-handoff', adapter)['phase'], 'ARCHIVED')
        self.assertEqual(calls[0], ('t3_thread_read', {'threadId': 'old'}))
        self.assertFalse(any(name == 't3_thread_organize' for name, _ in calls))

    def test_two_tasks_only_one_ready_must_not_archive_window(self):
        first, second, manifest, metadata = two_members()
        self.store.prepare_manifest(manifest, metadata)
        adapter = MultiQueriedAdapter([first, second], manifest)
        adapter.members['second-handoff'].offered = []
        result = self.store.advance('window-handoff', adapter)
        self.assertEqual(result['blocker'], 'BLOCKED_MISSING_EVIDENCE')
        self.assertIsNone(result['archive_intent'])
        self.assertNotIn('link', adapter.calls)
        self.assertNotIn('archive', adapter.calls)
        self.assertIn('query:demo-handoff:route_consumed-1', adapter.calls)
        self.assertIn('fetch:second-handoff', adapter.calls)

    def test_two_tasks_all_ready_one_window_archive_with_each_query(self):
        first, second, manifest, metadata = two_members()
        store = HandoffStore(self.root / 'multi-state')
        prepared = store.prepare_manifest(manifest, metadata)
        self.assertEqual(store.prepare_manifest(manifest, metadata), prepared)
        adapter = MultiQueriedAdapter([first, second], manifest)
        adapter.members['second-handoff'].offered = adapter.members['second-handoff'].offered[:2]
        result = store.advance('window-handoff', adapter)
        self.assertEqual(result['phase'], 'ARCHIVED')
        self.assertEqual(len(result['receipts']), 5)
        for hid in ('demo-handoff', 'second-handoff'):
            self.assertIn('query:' + hid + ':context_read-1', adapter.calls)
            self.assertIn('query:' + hid + ':route_consumed-1', adapter.calls)
        self.assertEqual(result['archive_intent']['manifest_digest'], sha256(manifest))
        self.assertEqual(adapter.calls.count('inventory_query'), 2)
        reopened = HandoffStore(self.root / 'multi-state')
        self.assertEqual(reopened.advance('window-handoff', adapter)['phase'], 'ARCHIVED')
        self.assertEqual(adapter.calls.count('archive'), 1)
        self.assertEqual(adapter.calls.count('link'), 1)

    def test_manifest_membership_change_invalidates_original_task_receipts(self):
        first, second, manifest, metadata = two_members()
        old_manifest = deepcopy(manifest)
        old_manifest['bindings'] = old_manifest['bindings'][:1]
        self.store.prepare_manifest(old_manifest, {'demo-handoff': metadata['demo-handoff']})
        adapter = MultiQueriedAdapter([first, second], old_manifest)
        result = self.store.advance('window-handoff', adapter)
        self.assertEqual(result['blocker'], 'BLOCKED_INVENTORY_MISMATCH')
        manifest['version'] = 2
        with self.assertRaises(Blocked):
            self.store.prepare_manifest(manifest, metadata)
        upgraded = self.store.prepare_manifest(manifest, metadata, upgrade=True)
        self.assertEqual(upgraded['receipts'], [])
        self.assertEqual(upgraded['history'][0]['manifest'], old_manifest)
        adapter.manifest = deepcopy(manifest)  # current inventory query, old consumer receipts
        result = self.store.advance('window-handoff', adapter)
        self.assertEqual(result['blocker'], 'RECEIPT_BINDING_MISMATCH')
        self.assertNotIn('archive', adapter.calls)

    def test_manifest_version_only_or_context_change_rejects_old_receipts(self):
        for change in ('version', 'context'):
            with self.subTest(change=change):
                first, second, manifest, metadata = two_members()
                store = HandoffStore(self.root / change)
                store.prepare_manifest(manifest, metadata)
                adapter = MultiQueriedAdapter([first, second], manifest)
                manifest = deepcopy(manifest)
                manifest['version'] = 2
                if change == 'context':
                    manifest['bindings'][0]['context_sha256'] = 'c' * 64
                    metadata = deepcopy(metadata)
                    metadata['demo-handoff']['context_ref']['sha256'] = 'c' * 64
                    adapter.members['demo-handoff'].authority['binding_digest'] = sha256(manifest['bindings'][0])
                store.prepare_manifest(manifest, metadata, upgrade=True)
                adapter.manifest = deepcopy(manifest)
                result = store.advance('window-handoff', adapter)
                self.assertEqual(result['blocker'], 'RECEIPT_BINDING_MISMATCH')
                self.assertNotIn('archive', adapter.calls)

    def test_unknown_omitted_or_stale_inventory_never_assumed_empty(self):
        original = self.adapter.verify_inventory(singleton_manifest(self.req['binding']))
        self.adapter.calls.clear()
        cases = [None, True, {'complete': False}, {**original, 'task_runs': []},
                 {**original, 'manifest_digest': 'c' * 64},
                 {**original, 'task_runs': original['task_runs'] + [{'task_id': 'omitted-task', 'run_id': 'another-run'}]}]
        for inventory in cases:
            with self.subTest(inventory=inventory):
                self.adapter.verify_inventory = lambda manifest: deepcopy(inventory)
                self.assert_blocked()
                self.assertNotIn('link', self.adapter.calls)
        self.adapter.verify_inventory = None
        self.assert_blocked('BLOCKED_INVENTORY_UNKNOWN')

    def test_inventory_change_after_link_blocks_archive(self):
        original = self.adapter.ensure_successor_link
        def writer(binding, key):
            result = original(binding, key)
            self.adapter.inventory_tasks.append({'task_id': 'late-new-work', 'run_id': 'new-run'})
            return result
        self.adapter.ensure_successor_link = writer
        self.assert_blocked('BLOCKED_INVENTORY_MISMATCH')

    def test_failed_cancelled_interrupted_or_unknown_successor_blocks(self):
        for status in ('failed', 'cancelled', 'interrupted', 'unknown', None):
            self.adapter = QueriedAdapter(self.req)
            self.adapter.threads['new']['status'] = status
            self.assert_blocked('BLOCKED_SUCCESSOR_UNAVAILABLE')
        self.adapter = QueriedAdapter(self.req)
        original = self.adapter.ensure_successor_link
        def writer(binding, key):
            result = original(binding, key)
            self.adapter.threads['new']['status'] = 'failed'
            return result
        self.adapter.ensure_successor_link = writer
        self.assert_blocked('BLOCKED_SUCCESSOR_UNAVAILABLE')

    def test_source_active_or_pending_work_conflict_blocks_before_link(self):
        for field, value in [('activeRunId', 'still-writing'), ('pendingRequestCount', 1),
                             ('pendingRequestCount', None), ('pendingRequestCount', False)]:
            with self.subTest(field=field, value=value):
                self.adapter = QueriedAdapter(self.req)
                self.adapter.threads['old'][field] = value
                self.assert_blocked('BLOCKED_SOURCE_BUSY_OR_UNKNOWN')
                self.assertNotIn('link', self.adapter.calls)
        self.adapter = QueriedAdapter(self.req)
        del self.adapter.threads['old']['activeRunId']
        self.assert_blocked('BLOCKED_SOURCE_BUSY_OR_UNKNOWN')

    def test_source_new_work_during_link_blocks_archive_and_recovers(self):
        original = self.adapter.ensure_successor_link
        def writer(binding, key):
            result = original(binding, key)
            self.adapter.threads['old']['pendingRequestCount'] = 1
            return result
        self.adapter.ensure_successor_link = writer
        self.assert_blocked('BLOCKED_SOURCE_BUSY_OR_UNKNOWN')
        self.adapter.threads['old']['pendingRequestCount'] = 0
        self.adapter.calls.clear()
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')
        self.assertEqual(self.adapter.calls[0], 'read:old')
        self.assertNotIn('link', self.adapter.calls)

    def test_receipt_revocation_after_link_is_queried_again(self):
        original = self.adapter.ensure_successor_link
        def writer(binding, key):
            result = original(binding, key)
            self.adapter.consumer.pop('route_consumed-1')
            return result
        self.adapter.ensure_successor_link = writer
        self.assert_blocked('UNVERIFIED_RECEIPT')

    def test_protocol_whitelist_only_change_and_strict_runtime_drift_rejected(self):
        from portable.distribution import build
        # Exact source snapshot: works in depth-1 CI and source archives.
        # Pin provenance as well as bytes; never read Git history at runtime.
        fixtures = Path(__file__).resolve().parent / 'fixtures'
        provenance = json.loads((fixtures / 'protocol-cc9ad6d.source.json').read_text())
        self.assertEqual(provenance, {
            'source_commit': 'cc9ad6d4992675d9f4048f469b97f83fdcaa0570',
            'source_path': 'portable/protocol.json',
            'fixture': 'protocol-cc9ad6d.json',
            'sha256': 'a8cf77cd9b8559fdf9631e8e6b3abe9f2fe1a2e783d63b85b66dd8dc85a3b8af',
        })
        baseline_bytes = (fixtures / 'protocol-cc9ad6d.json').read_bytes()
        self.assertEqual(hashlib.sha256(baseline_bytes).hexdigest(), provenance['sha256'])
        baseline = json.loads(baseline_bytes)
        current = json.loads((SOURCE / 'portable/protocol.json').read_text())
        expected = deepcopy(baseline)
        expected['distribution']['runtime_files'].extend(['taskline_handoff.py', 'taskline_host.py'])
        expected['distribution']['runtime_files'].append('ci.py')  # 0.2.42：统一查 CI、等 CI、再合并
        expected['distribution']['runtime_files'].append('review.py')
        expected['distribution']['runtime_files'].append('ci_docs_only.py')  # 第 77 题：文档改动轻量 CI 的判定规则  # 复盘工具（见 DECISIONS 2026-11-14）
        expected['distribution']['runtime_files'].insert(expected['distribution']['runtime_files'].index('protocol.json'), 'project_binding.py')
        expected['distribution']['runtime_files'].insert(expected['distribution']['runtime_files'].index('sealed_bundle.py'), 'remote_publish.py')
        expected['distribution']['runtime_files'].insert(expected['distribution']['runtime_files'].index('team_host.py')+1, 'team_retire.py')  # 11-17 停用自带战队后台
        for name in ('team_host.py', 'backend_overlay.py'):expected['distribution']['runtime_files'].remove(name)  # 自带战队代码已删（见 DECISIONS 2026-11-18）
        expected['distribution']['runtime_files'].remove('projects.json')  # 11-14 清理：源码里空的项目注册表，只有测试在读
        expected['distribution']['runtime_files'].append('nudge.py')  # 后台自动接着做（见 DECISIONS 2026-11-18）
        # Public rule wording is reviewed per PR and guarded by test_workflow_handover, test_settings
        # and test_board (key phrases, rule count, no local words); here only the rest of the
        # protocol must match the pinned baseline, so rules and version come from protocol.json itself.
        expected['public_rules'] = current['public_rules']
        expected['version'] = current['version']
        self.assertEqual(current, expected)
        for change in ('missing', 'unlisted', 'no-whitelist-entry'):
            with self.subTest(change=change):
                source = self.root / ('source-' + change)
                (source / 'portable').mkdir(parents=True)
                for path in (SOURCE / 'portable').iterdir():
                    if path.suffix in ('.py', '.json'):
                        shutil.copy2(path, source / 'portable' / path.name)
                if change == 'missing':
                    (source / 'portable/taskline_handoff.py').rename(source / 'omitted-module.txt')
                elif change == 'unlisted':
                    (source / 'portable/unlisted.py').write_text('# synthetic unknown module')
                else:
                    protocol = json.loads((source / 'portable/protocol.json').read_text())
                    protocol['distribution']['runtime_files'].remove('taskline_handoff.py')
                    (source / 'portable/protocol.json').write_text(canonical(protocol))
                with self.assertRaisesRegex(Blocked, '白名单漂移'):
                    build(source, self.root / ('release-' + change), edition='friend')

    def test_cli_help_prepare_status_default_advance_and_zipapp_inclusion(self):
        reqfile = self.root / 'request.json'
        reqfile.write_text(canonical(self.req))
        prefix = [sys.executable, '-B', '-m', 'portable', 'taskline']
        help_result = subprocess.run(prefix + ['--help'], cwd=SOURCE, capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('retry', help_result.stdout)
        prefix += ['--state-root', str(self.root / 'cli-state')]
        for suffix in (['prepare', str(reqfile)], ['status', 'demo-handoff']):
            result = subprocess.run(prefix + suffix, cwd=SOURCE, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(prefix + ['advance', 'demo-handoff'], cwd=SOURCE, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['blocker'], 'BLOCKED_CAPABILITY')
        from portable.distribution import build, release_verify
        release = self.root / 'friend-release'
        build(SOURCE, release, edition='friend')
        release_verify(release)
        with zipfile.ZipFile(release / '工具/portable.pyz') as archive:
            self.assertIn('portable/taskline_handoff.py', archive.namelist())
            self.assertFalse(any(name.startswith('tests/') or 'taskline-cross-repo.json' in name
                                 for name in archive.namelist()))
            runtime = {Path(name).name for name in archive.namelist()
                       if name.startswith('portable/') and Path(name).suffix in ('.py', '.json')}
            protocol = json.loads((SOURCE / 'portable/protocol.json').read_text())
            self.assertEqual(runtime, set(protocol['distribution']['runtime_files']))
            self.assertEqual(json.loads(archive.read('portable/protocol.json')), protocol)
        result = subprocess.run([sys.executable, '-B', str(release / '工具/portable.pyz'),
                                 'taskline', '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('prepare', result.stdout)


class TitleAdapterTests(unittest.TestCase):
    def setUp(self):
        from taskline_shared_fixture import load_fixture, FixtureQueries, SimulatedT3
        self.fixture = load_fixture()
        self.queries = FixtureQueries(self.fixture)
        self.service = SimulatedT3(self.fixture)
        self.adapter = self.queries.adapter(self.service)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = HandoffStore(self.root / 'state')
        self.store.prepare_manifest(**self.fixture['request'])
        self.manifest = self.fixture['request']['manifest']
        self.mid = self.manifest['manifest_id']

    def advance(self):
        return self.store.advance(self.mid, self.adapter)

    def writes(self, name):
        return [args for tool, args in self.service.calls if tool == name]

    def test_user_fixture_bytes_context_hashes_and_title_vectors(self):
        from taskline_shared_fixture import FIXTURE, FIXTURE_SHA256
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_SHA256)
        for binding in self.manifest['bindings']:
            self.assertEqual(sha256(self.fixture['contexts'][binding['handoff_id']]), binding['context_sha256'])
        for vector in self.fixture['title_vectors'][1:]:
            self.assertEqual(utf16_units(vector['title']), vector['utf16_units'])
            with self.assertRaisesRegex(Blocked, 'TITLE_CAPACITY'):
                handoff_title(vector['title'], self.manifest)
        self.assertTrue(self.service.source.startswith('mcp:launch:'))
        self.assertTrue(self.manifest['source_thread']['link'].startswith('<t3-thread://v2/'))

    def test_exact_shared_two_task_request_one_ready_blocks_then_all_archive_once(self):
        hids = list(self.queries.bindings)
        self.queries.ready_member(hids[0])
        result = self.advance()
        self.assertEqual(result['blocker'], 'BLOCKED_MISSING_EVIDENCE')
        self.assertEqual(self.writes('t3_thread_update'), [])
        self.assertEqual(self.writes('t3_thread_organize'), [])
        self.queries.ready_member(hids[1])
        result = self.advance()
        self.assertEqual(result['phase'], 'ARCHIVED')
        self.assertEqual(result['manifest'], self.fixture['request']['manifest'])
        self.assertEqual(len(result['receipts']), 4)
        self.assertEqual(len(self.writes('t3_thread_update')), 1)
        self.assertEqual(len(self.writes('t3_thread_organize')), 1)
        title = self.service.threads[self.service.source]['title']
        original, marker = parse_handoff_title(title)
        self.assertEqual(original, self.fixture['title_vectors'][0]['title'])
        self.assertEqual(marker['successor_link'], self.manifest['target_thread']['link'])
        self.assertEqual(marker['successor_manifest_digest'], sha256(self.manifest))
        self.assertLessEqual(utf16_units(self.writes('t3_thread_update')[0]['clientRequestId']), 256)
        self.assertFalse(any(name == 't3_thread_send' for name, _ in self.service.calls))
        before = deepcopy(self.service.calls)
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')
        self.assertEqual(before, self.service.calls)

    def test_shared_accepted_route_delivery_without_consumption_is_blocked(self):
        for hid in self.queries.bindings:
            self.queries.read_context(hid)
            self.assertEqual(self.queries.events[hid]['delivery'], 'accepted')
        self.assertEqual(self.advance()['blocker'], 'BLOCKED_MISSING_EVIDENCE')
        self.assertEqual(self.writes('t3_thread_update'), [])

    def test_title_exact_512_utf16_with_emoji_and_513_blocks_before_write(self):
        from taskline_shared_fixture import SimulatedT3
        overhead = utf16_units(handoff_title('x', self.manifest)) - 1
        capacity = 512 - overhead
        original = '中' * (capacity - 2) + '🧪'
        self.assertEqual(utf16_units(handoff_title(original, self.manifest)), 512)
        self.queries.ready_all()
        self.service = SimulatedT3(self.fixture, title=original)
        self.adapter = self.queries.adapter(self.service)
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')
        self.assertEqual(utf16_units(self.writes('t3_thread_update')[0]['title']), 512)
        self.assertEqual(parse_handoff_title(self.service.threads[self.service.source]['title'])[0], original)
        service = SimulatedT3(self.fixture, title=original + '中')
        store = HandoffStore(self.root / 'too-long')
        store.prepare_manifest(**self.fixture['request'])
        result = store.advance(self.mid, self.queries.adapter(service))
        self.assertEqual(result['blocker'], 'BLOCKED_TITLE_CAPACITY')
        self.assertFalse(any(name in ('t3_thread_update', 't3_thread_organize') for name, _ in service.calls))

    def test_bare_shared_512_and_513_titles_never_write(self):
        from taskline_shared_fixture import SimulatedT3
        self.queries.ready_all()
        for index, vector in enumerate(self.fixture['title_vectors'][1:]):
            service = SimulatedT3(self.fixture, title=vector['title'])
            store = HandoffStore(self.root / str(index))
            store.prepare_manifest(**self.fixture['request'])
            self.assertEqual(store.advance(self.mid, self.queries.adapter(service))['blocker'],
                             'BLOCKED_TITLE_CAPACITY')
            self.assertTrue(all(name == 't3_thread_read' for name, _ in service.calls))

    def test_quotes_ordinary_delimiters_and_versions_preserve_initial_title_once(self):
        original = '原题 | [分隔符] ⟦portable-handoff text⟧ "引号" 🧪'
        title = handoff_title(original, self.manifest)
        self.assertEqual(parse_handoff_title(title)[0], original)
        self.assertEqual(handoff_title(title, self.manifest), title)
        manifest2 = deepcopy(self.manifest)
        manifest2['version'] = 2
        title2 = handoff_title(title, manifest2)
        self.assertEqual(parse_handoff_title(title2)[0], original)
        self.assertEqual(title2.count(TITLE_FAMILY), 1)
        changed = deepcopy(self.manifest)
        changed['target_thread']['link'] += '"?quote=🧪'
        self.assertEqual(parse_handoff_title(handoff_title(original, changed))[1]['successor_link'],
                         changed['target_thread']['link'])
        for conflict in (self.manifest, {**manifest2, 'manifest_id': 'conflict'}):
            with self.assertRaisesRegex(Blocked, 'MARKER_CONFLICT'):
                handoff_title(title2, conflict)
        changed_same_version = deepcopy(self.manifest)
        changed_same_version['bindings'][0]['context_sha256'] = 'a' * 64
        with self.assertRaisesRegex(Blocked, 'MARKER_CONFLICT'):
            handoff_title(title, changed_same_version)

    def test_unknown_or_corrupt_marker_fails_closed_and_lone_surrogates_rejected(self):
        title = handoff_title('原标题', self.manifest)
        for malformed in (title.replace('handoff:v1', 'handoff:v2'), title + 'extra',
                          '原标题' + TITLE_FAMILY + 'unknown', title + TITLE_FAMILY + 'v1 []⟧'):
            with self.assertRaisesRegex(Blocked, 'MARKER_CONFLICT'):
                parse_handoff_title(malformed)
        for text in ('\ud83e', '\uddea', 'x\ud83ex'):
            with self.assertRaisesRegex(Blocked, 'UNPAIRED_UNICODE'):
                utf16_units(text)
        self.assertEqual(utf16_units('🧪'), 2)
        self.assertEqual(utf16_units('\ud83e\uddea'), 2)

    def test_rename_timeouts_read_before_retry_and_client_request_id_stable(self):
        from taskline_shared_fixture import SimulatedT3
        self.queries.ready_all()
        for mode in ('before', 'after'):
            service = SimulatedT3(self.fixture)
            service.rename_timeout = mode
            store = HandoffStore(self.root / mode)
            store.prepare_manifest(**self.fixture['request'])
            adapter = self.queries.adapter(service)
            blocked = store.advance(self.mid, adapter)
            self.assertEqual(blocked['blocker'], 'BLOCKED_ADAPTER_ERROR')
            self.assertFalse(blocked['archive_attempted'])
            start = len(service.calls)
            reopened = HandoffStore(self.root / mode)
            # A fresh adapter cannot rely on a locally cached title.
            self.assertEqual(reopened.advance(self.mid, self.queries.adapter(service))['phase'], 'ARCHIVED')
            self.assertEqual(service.calls[start][0], 't3_thread_read')
            updates = [args for name, args in service.calls if name == 't3_thread_update']
            self.assertEqual(len(updates), 2 if mode == 'before' else 1)
            self.assertEqual(len({u['clientRequestId'] for u in updates}), 1)
            self.assertEqual(sum(n == 't3_thread_organize' for n, _ in service.calls), 1)

    def test_real_title_replaces_previous_version_after_no_archive_attempt(self):
        from taskline_shared_fixture import FixtureQueries
        self.queries.ready_all()
        self.service.after_rename = lambda: self.service.threads[self.service.target].update(status='failed')
        blocked = self.advance()
        self.assertEqual(blocked['blocker'], 'BLOCKED_SUCCESSOR_UNAVAILABLE')
        self.assertFalse(blocked['archive_attempted'])
        self.assertEqual(self.writes('t3_thread_organize'), [])
        updated = deepcopy(self.fixture)
        updated['request']['manifest']['version'] = 2
        self.store.prepare_manifest(**updated['request'], upgrade=True)
        queries2 = FixtureQueries(updated)
        queries2.ready_all()
        self.service.after_rename = None
        self.service.threads[self.service.target]['status'] = 'idle'
        self.assertEqual(self.store.advance(self.mid, queries2.adapter(self.service))['phase'], 'ARCHIVED')
        original, marker = parse_handoff_title(self.service.threads[self.service.source]['title'])
        self.assertEqual(original, self.fixture['title_vectors'][0]['title'])
        self.assertEqual(marker['successor_manifest_version'], 2)
        self.assertEqual(len(self.writes('t3_thread_update')), 2)

    def test_already_correct_actual_title_is_not_renamed(self):
        self.queries.ready_all()
        self.service.threads[self.service.source]['title'] = handoff_title(
            self.fixture['title_vectors'][0]['title'], self.manifest)
        self.assertEqual(self.advance()['phase'], 'ARCHIVED')
        self.assertEqual(self.writes('t3_thread_update'), [])
        self.assertEqual(len(self.writes('t3_thread_organize')), 1)

    def test_unknown_previous_manifest_marker_is_not_overwritten(self):
        from taskline_shared_fixture import FixtureQueries
        unknown = deepcopy(self.manifest)
        unknown['bindings'][0]['context_sha256'] = 'c' * 64
        self.service.threads[self.service.source]['title'] = handoff_title('保留原题', unknown)
        updated = deepcopy(self.fixture)
        updated['request']['manifest']['version'] = 2
        self.store.prepare_manifest(**updated['request'], upgrade=True)
        queries2 = FixtureQueries(updated)
        queries2.ready_all()
        result = self.store.advance(self.mid, queries2.adapter(self.service))
        self.assertEqual(result['blocker'], 'BLOCKED_TITLE_MARKER_CONFLICT')
        self.assertEqual(self.writes('t3_thread_update'), [])
        self.assertEqual(self.writes('t3_thread_organize'), [])

    def test_unsafe_legacy_upgrade_history_with_old_rpc_fails_closed(self):
        manifest2 = deepcopy(self.manifest)
        manifest2['version'] = 2
        self.store.prepare_manifest(manifest2, self.fixture['request']['member_metadata'], upgrade=True)
        path = self.store.path(self.mid)
        value = json.loads(path.read_text())
        previous = value['history'][0]
        previous['archive_attempted'] = True
        previous['archive_outcome'] = 'NOT_ARCHIVED'  # old candidate's unsafe conclusion
        previous['archive_intent'] = {**manifest_scope(self.manifest),
                                      'idempotency_key': 'manifest:' + sha256(self.manifest)}
        path.write_text(canonical(value))
        with self.assertRaisesRegex(Blocked, 'HISTORICAL_ARCHIVE_ATTEMPT'):
            self.advance()
        self.assertEqual(self.service.calls, [])

    def test_late_old_archive_rpc_false_snapshot_never_unfreezes_new_manifest(self):
        self.queries.ready_all()
        self.service.archive_mode = 'deferred'
        unknown = self.advance()
        self.assertEqual(unknown['archive_outcome'], 'UNKNOWN')
        self.assertFalse(self.service.threads[self.service.source]['archived'])
        self.store = HandoffStore(self.root / 'state')
        # Force a read-only retry: the false read cannot prove cancellation.
        adapter = self.queries.adapter(self.service)
        adapter.capabilities = lambda: {'thread_read'}
        start = len(self.service.calls)
        again = self.store.advance(self.mid, adapter)
        self.assertEqual(again['archive_outcome'], 'UNKNOWN')
        self.assertEqual(self.service.calls[start][0], 't3_thread_read')
        manifest2 = deepcopy(self.manifest)
        manifest2['version'] = 2
        with self.assertRaisesRegex(Blocked, 'VERSION_FROZEN'):
            self.store.prepare_manifest(manifest2, self.fixture['request']['member_metadata'], upgrade=True)
        # The earlier RPC really applies late. Only the original immutable
        # manifest can be completed; no newly unready task list was introduced.
        self.service.apply_deferred()
        start = len(self.service.calls)
        confirmed = self.store.advance(self.mid, self.queries.adapter(self.service))
        self.assertEqual(confirmed['phase'], 'ARCHIVED')
        self.assertEqual(self.service.calls[start:], [('t3_thread_read', {'threadId': self.service.source})])
        self.assertEqual(len(self.writes('t3_thread_organize')), 1)

    def test_source_conflict_and_failed_target_shared_input_block_without_rename(self):
        from taskline_shared_fixture import SimulatedT3
        self.queries.ready_all()
        for role, field, value in (('source', 'activeRunId', 'still-writing'),
                                   ('source', 'pendingRequestCount', 1), ('target', 'status', 'failed')):
            service = SimulatedT3(self.fixture)
            service.threads[getattr(service, role)][field] = value
            store = HandoffStore(self.root / (role + field))
            store.prepare_manifest(**self.fixture['request'])
            self.assertNotEqual(store.advance(self.mid, self.queries.adapter(service))['phase'], 'ARCHIVED')
            self.assertTrue(all(name == 't3_thread_read' for name, _ in service.calls))


from taskline_host_checks import HostTests
from taskline_cross_host_checks import CrossHostTests
from taskline_next_step_checks import NextStepTests


if __name__ == '__main__':
    unittest.main()
