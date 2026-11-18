"""Synthetic query/transport services derived from the unchanged shared input.

No real T3, model, task creation or authority service is called. The production
adapter receives the same tool names and raw read envelope as a real transport.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from portable.taskline_handoff import (T3Adapter, canonical, manifest_scope, sha256,
                                       utf16_units)

FIXTURE = Path(__file__).resolve().parent / 'fixtures/taskline-cross-repo.json'
FIXTURE_SHA256 = '8605898e2c1fe78e3f8bb7459a37cc732d388484554ffc590e8899618b8a9b45'


def load_fixture():
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256
    return json.loads(raw)


class FixtureQueries:
    """Independent context reader, event publisher, consumer and query records."""
    def __init__(self, fixture):
        self.fixture = deepcopy(fixture)
        self.manifest = self.fixture['request']['manifest']
        self.metadata = self.fixture['request']['member_metadata']
        self.bindings = {b['handoff_id']: b for b in self.manifest['bindings']}
        self.inventory = [{'task_id': b['task_id'], 'run_id': b['run_id']}
                          for b in self.manifest['bindings']]
        self.context_reads, self.consumers, self.receipts, self.events = {}, {}, {}, {}
        self.calls = []
        for hid, binding in self.bindings.items():
            assert sha256(self.fixture['contexts'][hid]) == binding['context_sha256']
            payload = {'task_id': binding['task_id'], 'run_id': binding['run_id'],
                       'context': self.fixture['contexts'][hid]}
            self.events[hid] = {'event_id': 'probe-' + sha256(binding), 'payload': payload,
                                'payload_sha256': sha256(payload), 'delivery': 'accepted'}

    def receipt(self, hid, kind):
        b = self.bindings[hid]
        value = {'receipt_id': hid + ':' + kind, 'kind': kind, 'binding_digest': sha256(b),
                 'handoff_id': hid, 'version': b['version'], 'task_id': b['task_id'], 'run_id': b['run_id'],
                 'source_thread_id': b['source_thread']['thread_id'],
                 'target_thread_id': b['target_thread']['thread_id'],
                 'provenance': 'fixture:query:' + hid, **manifest_scope(self.manifest)}
        if kind == 'context_read':
            value['context_sha256'] = self.context_reads[hid]
        else:
            event = self.events[hid]
            value.update(event_id=event['event_id'], payload_sha256=event['payload_sha256'],
                         persistent_route=True)
        self.receipts[value['receipt_id']] = value

    def read_context(self, hid):
        # Hash the actual fixture context read by the receiver, not the receipt.
        self.context_reads[hid] = sha256(self.fixture['contexts'][hid])
        self.receipt(hid, 'context_read')

    def consume(self, hid):
        event = self.events[hid]
        self.consumers[event['event_id']] = {
            'target': self.bindings[hid]['target_thread']['thread_id'],
            'payload_sha256': sha256(event['payload'])}
        self.receipt(hid, 'route_consumed')

    def ready_member(self, hid):
        self.read_context(hid)
        self.consume(hid)

    def ready_all(self):
        for hid in self.bindings:
            self.ready_member(hid)

    def authorization_query(self, binding, ref):
        hid = binding['handoff_id']
        self.calls.append('authority:' + hid)
        metadata = self.metadata[hid]
        if binding != self.bindings[hid] or ref != metadata['authorization_ref']:
            return None
        return {'authorized': True, 'binding_digest': sha256(binding),
                'current_caller': metadata['current_caller'], 'authorization_ref': deepcopy(ref),
                'scope': metadata['scope'], 'worker_active': metadata['worker_active'],
                'provenance': 'fixture:existing-authority-query'}

    def inventory_query(self, manifest):
        self.calls.append('inventory')
        return {'complete': manifest == self.manifest, **manifest_scope(self.manifest),
                'source_thread_id': self.manifest['source_thread']['thread_id'],
                'task_runs': deepcopy(self.inventory), 'provenance': 'fixture:window-query'}

    def receipt_query(self, binding, receipt_id):
        hid = binding['handoff_id']
        self.calls.append('receipt:' + str(receipt_id))
        if binding != self.bindings[hid]:
            return None
        if receipt_id is None:
            return [deepcopy(r) for r in self.receipts.values() if r['handoff_id'] == hid]
        receipt = self.receipts.get(receipt_id)
        if receipt is None:
            return None
        if receipt['kind'] == 'context_read':
            if self.context_reads.get(hid) != sha256(self.fixture['contexts'][hid]):
                return None
        else:
            event = self.events[hid]
            consumed = self.consumers.get(event['event_id'])
            if (receipt['event_id'] != event['event_id'] or
                    receipt['payload_sha256'] != sha256(event['payload']) or
                    consumed != {'target': binding['target_thread']['thread_id'],
                                 'payload_sha256': sha256(event['payload'])}):
                return None
        return deepcopy(receipt)

    def adapter(self, transport):
        return T3Adapter(transport, authority_query=self.authorization_query,
                         receipt_query=self.receipt_query, inventory_query=self.inventory_query)


class SimulatedT3:
    def __init__(self, fixture, title=None):
        manifest = fixture['request']['manifest']
        self.source = manifest['source_thread']['thread_id']
        self.target = manifest['target_thread']['thread_id']
        self.threads = {t['thread_id']: {'id': t['thread_id'], 'link': t['link'], 'archived': False,
                                       'title': '合成后继', 'status': 'idle', 'activeRunId': None,
                                       'pendingRequestCount': 0}
                        for t in (manifest['source_thread'], manifest['target_thread'])}
        self.threads[self.source]['title'] = title if title is not None else fixture['title_vectors'][0]['title']
        self.calls, self.idempotency, self.deferred = [], {}, []
        self.rename_timeout = None
        self.archive_mode = 'normal'
        self.after_rename = None

    def __call__(self, name, args):
        self.calls.append((name, deepcopy(args)))
        if name == 't3_thread_read':
            # Actual stored title only; no invented item or synthetic link field.
            return {'content': [{'type': 'text', 'text': canonical({'thread': self.threads[args['threadId']]})}]}
        if name == 't3_thread_update':
            assert set(args) == {'action', 'threadId', 'title', 'clientRequestId'}
            assert args['action'] == 'rename'
            assert 0 < utf16_units(args['title']) <= 512
            assert 0 < utf16_units(args['clientRequestId']) <= 256
            if self.rename_timeout == 'before':
                self.rename_timeout = None
                raise TimeoutError()
            request = (args['threadId'], args['title'])
            previous = self.idempotency.setdefault(args['clientRequestId'], request)
            assert previous == request
            self.threads[args['threadId']]['title'] = args['title']
            if self.after_rename:
                self.after_rename()
            if self.rename_timeout == 'after':
                self.rename_timeout = None
                raise TimeoutError()
            return {'structuredContent': {'accepted': True}}
        if name == 't3_thread_organize':
            assert args == {'action': 'archive', 'threadId': self.source}
            if self.archive_mode == 'deferred':
                self.deferred.append(args['threadId'])
                raise TimeoutError()
            self.threads[args['threadId']]['archived'] = True
            if self.archive_mode == 'after_timeout':
                raise TimeoutError()
            return {'accepted': True}
        raise AssertionError('Unexpected external capability: ' + name)

    def apply_deferred(self):
        for thread_id in self.deferred:
            self.threads[thread_id]['archived'] = True
        self.deferred.clear()
