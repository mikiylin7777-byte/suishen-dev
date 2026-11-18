"""Durable taskline orchestration. All external authority comes from a trusted adapter.

No adapter is imported from state, environment, or CLI input. See
 docs/taskline-handoff.md for the adapter query/attestation contract.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Optional, Protocol

from .core import Blocked

BINDING_KEYS = {'handoff_id', 'version', 'source_thread', 'target_thread',
                'task_id', 'run_id', 'context_sha256'}
CAPABILITIES = {'authorization_query', 'receipt_query', 'thread_read',
                'successor_link', 'archive', 'inventory_query'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sha256(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def hash_value(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def validate_binding(binding):
    require(isinstance(binding, dict) and set(binding) == BINDING_KEYS, 'INVALID_BINDING')
    require(isinstance(binding['handoff_id'], str) and
            re.fullmatch('[A-Za-z0-9_-]{1,120}', binding['handoff_id']), 'INVALID_HANDOFF_ID')
    require(type(binding['version']) is int and binding['version'] >= 1, 'INVALID_VERSION')
    for key in ('task_id', 'run_id'):
        require(nonempty(binding[key]), 'INVALID_' + key.upper())
    require(hash_value(binding['context_sha256']), 'INVALID_CONTEXT_HASH')
    for key in ('source_thread', 'target_thread'):
        thread = binding[key]
        require(isinstance(thread, dict) and set(thread) == {'thread_id', 'link'} and
                all(nonempty(v) for v in thread.values()), 'INVALID_THREAD')
    require(binding['source_thread']['thread_id'] != binding['target_thread']['thread_id'],
            'SAME_THREAD')
    require(binding['source_thread']['link'] != binding['target_thread']['link'], 'SAME_LINK')


def validate_metadata(binding, metadata):
    require(isinstance(metadata, dict) and set(metadata) ==
            {'context_ref', 'authorization_ref', 'current_caller', 'scope', 'worker_active', 'todos'},
            'INVALID_METADATA')
    for key in ('context_ref', 'authorization_ref'):
        ref = metadata[key]
        require(isinstance(ref, dict) and set(ref) == {'ref', 'sha256'} and
                nonempty(ref['ref']) and hash_value(ref['sha256']), 'INVALID_REFERENCE')
    require(metadata['context_ref']['sha256'] == binding['context_sha256'], 'CONTEXT_MISMATCH')
    require(nonempty(metadata['current_caller']), 'INVALID_CALLER')
    require(metadata['scope'] in ('engineering', 'paused', 'read_only'), 'INVALID_SCOPE')
    require(type(metadata['worker_active']) is bool, 'INVALID_WORKER_STATE')
    require(isinstance(metadata['todos'], list) and all(nonempty(v) for v in metadata['todos']),
            'INVALID_TODOS')


def singleton_manifest(binding):
    return {'manifest_id': binding['handoff_id'], 'version': binding['version'],
            'source_thread': deepcopy(binding['source_thread']),
            'target_thread': deepcopy(binding['target_thread']), 'bindings': [deepcopy(binding)]}


def manifest_scope(manifest):
    return {'manifest_id': manifest['manifest_id'], 'manifest_version': manifest['version'],
            'manifest_digest': sha256(manifest)}


def validate_manifest(manifest, member_metadata):
    require(isinstance(manifest, dict) and set(manifest) ==
            {'manifest_id', 'version', 'source_thread', 'target_thread', 'bindings'}, 'INVALID_MANIFEST')
    require(isinstance(manifest['manifest_id'], str) and
            re.fullmatch('[A-Za-z0-9_-]{1,120}', manifest['manifest_id']), 'INVALID_MANIFEST_ID')
    require(type(manifest['version']) is int and manifest['version'] >= 1, 'INVALID_MANIFEST_VERSION')
    require(isinstance(manifest['bindings'], list) and bool(manifest['bindings']), 'EMPTY_MANIFEST')
    require(isinstance(member_metadata, dict), 'INVALID_MEMBER_METADATA')
    ids, tasks = set(), set()
    for binding in manifest['bindings']:
        validate_binding(binding)
        require(binding['source_thread'] == manifest['source_thread'] and
                binding['target_thread'] == manifest['target_thread'], 'MANIFEST_THREAD_MISMATCH')
        key = binding['handoff_id']
        pair = (binding['task_id'], binding['run_id'])
        require(key not in ids and pair not in tasks, 'DUPLICATE_MANIFEST_MEMBER')
        ids.add(key)
        tasks.add(pair)
        require(key in member_metadata, 'MEMBER_METADATA_MISSING')
        validate_metadata(binding, member_metadata[key])
    require(set(member_metadata) == ids, 'INVALID_MEMBER_METADATA')


class HandoffStore:
    """An explicitly selected private directory, atomic snapshots and process lock.

    The directory must be owned by this uid and not group/world writable. State is
    local orchestration data, never an authority source. Untrusted local writers
    must not be granted write access to it.
    """
    def __init__(self, state_root):
        path = Path(state_root).absolute()
        require('..' not in path.parts, 'UNSAFE_STATE_ROOT')
        for part in reversed((path, *path.parents)):
            require(not part.is_symlink(), 'UNSAFE_STATE_ROOT')
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and
                not info.st_mode & 0o022, 'UNSAFE_STATE_PERMISSIONS')
        self.root = path

    def path(self, handoff_id):
        require(isinstance(handoff_id, str) and
                re.fullmatch('[A-Za-z0-9_-]{1,120}', handoff_id), 'INVALID_HANDOFF_ID')
        return self.root / (handoff_id + '.json')

    @contextmanager
    def locked(self):
        require(not self.root.is_symlink(), 'UNSAFE_STATE_ROOT')
        info = self.root.stat()
        require(info.st_uid == os.getuid() and not info.st_mode & 0o022,
                'UNSAFE_STATE_PERMISSIONS')
        fd = os.open(self.root / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_nlink == 1,
                    'UNSAFE_LOCK')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def load(self, handoff_id):
        fd = os.open(self.path(handoff_id), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                    info.st_size <= 4 * 1024 * 1024, 'UNSAFE_STATE_FILE')
            value = json.load(stream)
        require(isinstance(value, dict) and type(value.get('schema')) is int and
                value['schema'] == 2, 'UNKNOWN_STATE_SCHEMA_MANIFEST_REQUIRED')
        manifest = value['manifest']
        validate_manifest(manifest, value['member_metadata'])
        first = manifest['bindings'][0]
        require(manifest['manifest_id'] == handoff_id and
                value['manifest_digest'] == sha256(manifest) and
                value['request_digest'] == sha256([manifest, value['member_metadata']]) and
                value['binding'] == first and value['binding_digest'] == sha256(first) and
                value['metadata'] == value['member_metadata'][first['handoff_id']], 'CORRUPT_STATE')
        require(value.get('phase') in ('PREPARED', 'BLOCKED', 'ARCHIVE_PENDING', 'ARCHIVED') and
                isinstance(value.get('receipts'), list) and isinstance(value.get('history'), list),
                'CORRUPT_STATE')
        for previous in value['history']:
            require(isinstance(previous, dict), 'CORRUPT_MANIFEST_HISTORY')
            # An unsafe older candidate may already have upgraded after a
            # false snapshot. Its pending RPC can still apply to this window.
            require(previous.get('archive_attempted') is not True and not
                    ('archive_attempted' not in previous and previous.get('archive_intent')),
                    'BLOCKED_HISTORICAL_ARCHIVE_ATTEMPT')
        intent = value.get('archive_intent')
        require(intent is None or intent == {
            **manifest_scope(manifest), 'idempotency_key': 'manifest:' + value['manifest_digest']},
            'CORRUPT_ARCHIVE_INTENT')
        require(value['phase'] not in ('ARCHIVE_PENDING', 'ARCHIVED') or intent is not None,
                'CORRUPT_ARCHIVE_INTENT')
        if 'archive_attempted' not in value and 'archive_outcome' not in value:
            # Older candidate snapshots did not distinguish intent from RPC.
            # An existing intent is conservatively unknown until real readback.
            value['archive_attempted'] = intent is not None
            value['archive_outcome'] = ('ARCHIVED' if value['phase'] == 'ARCHIVED' else
                                        'UNKNOWN' if intent is not None else 'NOT_ATTEMPTED')
        # A prior candidate treated one false snapshot as cancellation. That
        # does not rule out a late RPC; retain the original attempt boundary.
        if value.get('archive_attempted') is True and value.get('archive_outcome') == 'NOT_ARCHIVED':
            value['archive_outcome'] = 'UNKNOWN'
        attempted, outcome = value.get('archive_attempted'), value.get('archive_outcome')
        require(type(attempted) is bool and outcome in
                ('NOT_ATTEMPTED', 'UNKNOWN', 'NOT_ARCHIVED', 'ARCHIVED') and
                (intent is not None or (not attempted and outcome == 'NOT_ATTEMPTED')) and
                (outcome != 'NOT_ATTEMPTED' or not attempted) and
                (outcome not in ('UNKNOWN', 'NOT_ARCHIVED') or attempted) and
                (value['phase'] != 'ARCHIVED' or outcome == 'ARCHIVED'),
                'CORRUPT_ARCHIVE_ATTEMPT')
        return value

    def save(self, value):
        serialized = canonical(value) + '\n'
        require(len(serialized.encode()) <= 4 * 1024 * 1024, 'STATE_TOO_LARGE')
        target = self.path(value['manifest']['manifest_id'])
        require(not target.is_symlink(), 'UNSAFE_STATE_FILE')
        fd, name = tempfile.mkstemp(prefix='.handoff-', dir=self.root)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def prepare(self, binding, metadata, *, upgrade=False):
        # Convenience input only. Even a singleton must pass authoritative full
        # window inventory and manifest-scoped receipts before archival.
        validate_binding(binding)
        return self.prepare_manifest(singleton_manifest(binding),
                                     {binding['handoff_id']: metadata}, upgrade=upgrade)

    def prepare_manifest(self, manifest, member_metadata, *, upgrade=False):
        manifest, member_metadata = deepcopy(manifest), deepcopy(member_metadata)
        validate_manifest(manifest, member_metadata)
        with self.locked():
            previous = None
            try:
                previous = self.load(manifest['manifest_id'])
            except FileNotFoundError:
                pass
            request_digest = sha256([manifest, member_metadata])
            if previous:
                if previous['request_digest'] == request_digest:
                    return previous
                require(upgrade and manifest['version'] == previous['manifest']['version'] + 1,
                        'PREPARE_CONFLICT_EXPLICIT_VERSION_REQUIRED')
                require(not previous['archive_attempted'] and previous['archive_outcome'] != 'ARCHIVED',
                        'ARCHIVE_INTENT_VERSION_FROZEN')
            else:
                require(manifest['version'] == 1, 'INITIAL_VERSION_MUST_BE_ONE')
            first = manifest['bindings'][0]
            state = {'schema': 2, 'manifest': manifest, 'manifest_digest': sha256(manifest),
                     'member_metadata': member_metadata,
                     # Read-only compatibility aliases; never the window scope.
                     'binding': deepcopy(first), 'binding_digest': sha256(first),
                     'metadata': deepcopy(member_metadata[first['handoff_id']]),
                     'request_digest': request_digest, 'phase': 'PREPARED',
                     'blocker': None, 'archive_intent': None, 'archive_attempted': False,
                     'archive_outcome': 'NOT_ATTEMPTED', 'receipts': [],
                     'history': [] if not previous else previous['history'] +
                     [{k: v for k, v in previous.items() if k != 'history'}]}
            self.save(state)
            return state

    def status(self, handoff_id):
        with self.locked():
            # A snapshot is not a fresh ready decision. advance always re-queries.
            return self.load(handoff_id)

    def _member_evidence(self, state, adapter, manifest):
        b, m = state['binding'], state['metadata']
        require(m['scope'] == 'engineering', 'BLOCKED_SCOPE_' + m['scope'].upper())
        require(adapter is not None and CAPABILITIES <= set(adapter.capabilities()),
                'BLOCKED_CAPABILITY')
        authority = adapter.verify_authorization(deepcopy(b), deepcopy(m['authorization_ref']))
        require(isinstance(authority, dict) and authority.get('authorized') is True and
                authority.get('binding_digest') == state['binding_digest'] and
                authority.get('current_caller') == m['current_caller'] and
                authority.get('authorization_ref') == m['authorization_ref'] and
                authority.get('scope') == m['scope'] and
                type(authority.get('worker_active')) is bool and
                authority['worker_active'] == m['worker_active'] and
                nonempty(authority.get('provenance')), 'BLOCKED_AUTHORITY')
        require(callable(getattr(adapter, 'fetch_receipts', None)), 'BLOCKED_RECEIPT_QUERY')
        receipts = adapter.fetch_receipts(deepcopy(b))
        require(isinstance(receipts, list), 'BLOCKED_RECEIPT_QUERY')
        verified, identities = {}, {}
        for receipt in receipts:
            require(isinstance(receipt, dict), 'INVALID_RECEIPT')
            # verify_receipt must query the actual consumer/operation, returning
            # its current canonical receipt. A truthy ACK is never sufficient.
            queried = adapter.verify_receipt(deepcopy(b), deepcopy(receipt))
            require(isinstance(queried, dict) and queried == receipt, 'UNVERIFIED_RECEIPT')
            expected = {'binding_digest': state['binding_digest'], 'handoff_id': b['handoff_id'],
                        'version': b['version'], 'task_id': b['task_id'], 'run_id': b['run_id'],
                        'source_thread_id': b['source_thread']['thread_id'],
                        'target_thread_id': b['target_thread']['thread_id'],
                        **manifest_scope(manifest)}
            require(all(type(receipt.get(k)) is type(v) and receipt.get(k) == v
                        for k, v in expected.items()), 'RECEIPT_BINDING_MISMATCH')
            require(nonempty(receipt.get('receipt_id')) and nonempty(receipt.get('provenance')),
                    'RECEIPT_PROVENANCE_MISSING')
            rid = receipt['receipt_id']
            require(rid not in identities or identities[rid] == receipt, 'RECEIPT_ID_CONFLICT')
            identities[rid] = receipt
            kind = receipt.get('kind')
            require(kind in ('context_read', 'route_consumed', 'next_step_started'),
                    'INVALID_RECEIPT_KIND')
            if kind == 'context_read':
                require(receipt.get('context_sha256') == b['context_sha256'], 'RECEIPT_CONTEXT_MISMATCH')
            if kind == 'route_consumed':
                require(nonempty(receipt.get('event_id')) and hash_value(receipt.get('payload_sha256')),
                        'INVALID_ROUTE_RECEIPT')
                if m['worker_active']:
                    require(receipt.get('persistent_route') is True, 'PERSISTENT_ROUTE_UNVERIFIED')
            if kind == 'next_step_started':
                require(nonempty(receipt.get('operation_id')), 'OPERATION_READBACK_MISSING')
            verified[kind] = receipt
        needed = {'context_read', 'route_consumed'}
        if not m['worker_active']:
            needed.add('next_step_started')
        require(needed <= verified.keys(), 'BLOCKED_MISSING_EVIDENCE')
        return list(identities.values())

    def _evidence(self, state, adapter):
        manifest = state['manifest']
        # Scope is checked before touching external systems, for ALL members.
        for metadata in state['member_metadata'].values():
            require(metadata['scope'] == 'engineering', 'BLOCKED_SCOPE_' + metadata['scope'].upper())
        require(adapter is not None and CAPABILITIES <= set(adapter.capabilities()),
                'BLOCKED_CAPABILITY')
        require(callable(getattr(adapter, 'verify_inventory', None)), 'BLOCKED_INVENTORY_UNKNOWN')
        inventory = adapter.verify_inventory(deepcopy(manifest))
        require(isinstance(inventory, dict) and inventory.get('complete') is True and
                all(type(inventory.get(k)) is type(v) and inventory.get(k) == v
                    for k, v in manifest_scope(manifest).items()) and
                inventory.get('source_thread_id') == manifest['source_thread']['thread_id'] and
                nonempty(inventory.get('provenance')) and isinstance(inventory.get('task_runs'), list),
                'BLOCKED_INVENTORY_UNKNOWN')
        task_runs = inventory['task_runs']
        require(all(isinstance(row, dict) and set(row) == {'task_id', 'run_id'} and
                    all(nonempty(v) for v in row.values()) for row in task_runs),
                'BLOCKED_INVENTORY_UNKNOWN')
        actual = [canonical(row) for row in task_runs]
        expected = [canonical({'task_id': b['task_id'], 'run_id': b['run_id']})
                    for b in manifest['bindings']]
        require(len(actual) == len(set(actual)) and set(actual) == set(expected),
                'BLOCKED_INVENTORY_MISMATCH')
        receipts = []
        for binding in manifest['bindings']:
            member = {'binding': binding, 'binding_digest': sha256(binding),
                      'metadata': state['member_metadata'][binding['handoff_id']]}
            receipts.extend(self._member_evidence(member, adapter, manifest))
        target = adapter.read_thread(manifest['target_thread']['thread_id'])
        require(isinstance(target, dict) and
                target.get('thread_id') == manifest['target_thread']['thread_id'] and
                target.get('archived') is False and target.get('link') == manifest['target_thread']['link']
                and target.get('status') in ('idle', 'running', 'completed'),
                'BLOCKED_SUCCESSOR_UNAVAILABLE')
        return receipts

    def advance(self, handoff_id, adapter=None):
        with self.locked():
            state = self.load(handoff_id)
            if state['phase'] == 'ARCHIVED':
                return state
            try:
                if callable(getattr(adapter, 'set_manifest_history', None)):
                    adapter.set_manifest_history(deepcopy(state['history']))
                state['receipts'] = []  # never reuse an older ready determination
                b = state['manifest']
                old = None
                if state['archive_intent']:
                    require(adapter is not None and 'thread_read' in set(adapter.capabilities()),
                            'BLOCKED_CAPABILITY')
                    old = self._read_source(state, adapter)
                    if old['archived']:
                        # Reconcile an already completed external operation,
                        # without issuing any write or claiming a fresh ready.
                        # The durable intent and actual manifest link must match.
                        self._check_link(old, b)
                        state.update(phase='ARCHIVED', blocker=None)
                        self.save(state)
                        return state
                state['receipts'] = self._evidence(state, adapter)
                key = 'manifest:' + state['manifest_digest']
                if not state['archive_intent']:
                    state['archive_intent'] = {'idempotency_key': key, **manifest_scope(b)}
                state.update(phase='ARCHIVE_PENDING', blocker=None)
                self.save(state)  # intent is durable before any external mutation
                source_id = b['source_thread']['thread_id']
                if old is None:
                    old = self._read_source(state, adapter)
                self._check_source(old, b)
                # On timeout/restart, reconcile the external result before resending.
                if old['archived']:
                    self._check_link(old, b)
                else:
                    if (old.get('successor_link') != b['target_thread']['link'] or
                            old.get('successor_manifest_digest') != state['manifest_digest']):
                        adapter.ensure_successor_link(deepcopy(b['bindings'][0]), key + ':link')
                    old = self._read_source(state, adapter)
                    self._check_link(old, b)
                    # Authority, consumer receipts, worker state and successor
                    # liveness may have changed while publishing the link.
                    state['receipts'] = self._evidence(state, adapter)
                    old = self._read_source(state, adapter)
                    self._check_link(old, b)
                    if not old['archived']:
                        guard = getattr(adapter, 'before_archive', None)
                        if callable(guard):
                            guard(deepcopy(b))
                        # Write-ahead boundary: after this durable save, a
                        # crash may mean the RPC applied. Never infer otherwise
                        # from an exception, ACK, or a missing local return.
                        state.update(archive_attempted=True, archive_outcome='UNKNOWN')
                        self.save(state)
                        adapter.archive_thread(source_id, key + ':archive')
                        old = self._read_source(state, adapter)
                        self._check_link(old, b)
                require(old['archived'] is True, 'BLOCKED_ARCHIVE_READBACK')
                state.update(phase='ARCHIVED', blocker=None)
            except Exception as exc:
                # Transport exceptions can have unknown effects. Persist recovery
                # without logging potentially sensitive transport exception text.
                reason = str(exc) if isinstance(exc, Blocked) else 'BLOCKED_ADAPTER_ERROR'
                state.update(phase='ARCHIVE_PENDING' if state['archive_intent'] else 'BLOCKED',
                             blocker=reason,
                             receipts=state['receipts'] if reason == 'BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE' else [])
                # These are audited snapshots, never a future ready grant;
                # every advance clears/requeries them before any external write.
            self.save(state)
            return state

    def _read_source(self, state, adapter):
        manifest = state['manifest']
        thread = adapter.read_thread(manifest['source_thread']['thread_id'])
        self._check_source(thread, manifest, require_idle=False)
        if thread['archived'] and state['archive_outcome'] != 'ARCHIVED':
            state['archive_outcome'] = 'ARCHIVED'
            self.save(state)
        # A false snapshot is not proof that an earlier timed-out RPC was
        # cancelled. UNKNOWN remains frozen across false reads and restarts;
        # only this same manifest may retry. No adapter ACK clears the boundary.
        self._check_source(thread, manifest)
        return thread

    @staticmethod
    def _check_source(thread, binding, *, require_idle=True):
        require(isinstance(thread, dict) and
                thread.get('thread_id') == binding['source_thread']['thread_id'] and
                thread.get('link') == binding['source_thread']['link'] and
                type(thread.get('archived')) is bool, 'BLOCKED_SOURCE_READBACK')
        if require_idle and not thread['archived']:
            require('activeRunId' in thread and thread['activeRunId'] is None and
                    type(thread.get('pendingRequestCount')) is int and thread['pendingRequestCount'] == 0,
                    'BLOCKED_SOURCE_BUSY_OR_UNKNOWN')

    @staticmethod
    def _check_link(thread, binding):
        require(thread.get('successor_link') == binding['target_thread']['link'] and
                thread.get('successor_manifest_digest') == sha256(binding) and
                thread.get('successor_manifest_id') == binding['manifest_id'] and
                type(thread.get('successor_manifest_version')) is int and
                thread['successor_manifest_version'] == binding['version'], 'BLOCKED_LINK_READBACK')


def t3_payload(raw):
    """Unwrap explicit JSON/MCP envelopes, rejecting tool errors at every level."""
    for _ in range(5):
        require(isinstance(raw, dict) and raw.get('isError') is not True and
                raw.get('ok') is not False and not raw.get('error'), 'BLOCKED_T3_TOOL_ERROR')
        if 'structuredContent' in raw:
            raw = raw['structuredContent']
        elif 'content' in raw:
            content = raw['content']
            require(isinstance(content, list) and len(content) == 1 and
                    isinstance(content[0], dict) and content[0].get('type') == 'text',
                    'BLOCKED_T3_ENVELOPE')
            try:
                raw = json.loads(content[0]['text'])
            except (ValueError, TypeError, KeyError):
                raise Blocked('BLOCKED_T3_ENVELOPE') from None
        elif 'result' in raw and 'thread' not in raw:
            raw = raw['result']
        else:
            return raw
    raise Blocked('BLOCKED_T3_ENVELOPE')


TITLE_FAMILY = '\n⟦portable-handoff:'
TITLE_PREFIX = TITLE_FAMILY + 'v1 '
TITLE_END = '⟧'


def utf16_units(value):
    require(isinstance(value, str), 'BLOCKED_TITLE_SHAPE')
    count, index = 0, 0
    while index < len(value):
        code = ord(value[index])
        if 0xD800 <= code <= 0xDBFF:
            require(index + 1 < len(value) and 0xDC00 <= ord(value[index + 1]) <= 0xDFFF,
                    'BLOCKED_UNPAIRED_UNICODE')
            index += 2
            count += 2
        else:
            require(not 0xDC00 <= code <= 0xDFFF, 'BLOCKED_UNPAIRED_UNICODE')
            index += 1
            count += 2 if code > 0xFFFF else 1
    return count


def parse_handoff_title(title):
    """Parse only the actual title. Never consult a cache or write response.

    The full reserved family is fail-closed; ordinary brackets, quotes and
    separators in the original title remain literal, untouched text.
    """
    require(nonempty(title), 'BLOCKED_TITLE_SHAPE')
    require(utf16_units(title) <= 512, 'BLOCKED_TITLE_CAPACITY')
    if TITLE_FAMILY not in title:
        return title, None
    require(title.count(TITLE_FAMILY) == 1 and TITLE_PREFIX in title and title.endswith(TITLE_END),
            'BLOCKED_TITLE_MARKER_CONFLICT')
    original, encoded = title.split(TITLE_PREFIX, 1)
    require(nonempty(original), 'BLOCKED_TITLE_MARKER_CONFLICT')
    try:
        record = json.loads(encoded[:-len(TITLE_END)])
    except (ValueError, TypeError):
        raise Blocked('BLOCKED_TITLE_MARKER_CONFLICT') from None
    require(isinstance(record, list) and len(record) == 4 and
            isinstance(record[0], str) and re.fullmatch('[A-Za-z0-9_-]{1,120}', record[0]) and
            type(record[1]) is int and record[1] >= 1 and hash_value(record[2]) and nonempty(record[3]),
            'BLOCKED_TITLE_MARKER_CONFLICT')
    utf16_units(record[3])
    # ASCII JSON prevents delimiter-looking link text from creating a second
    # marker. Canonical spelling makes exact readback unambiguous.
    canonical_record = json.dumps(record, ensure_ascii=True, separators=(',', ':'))
    require(encoded == canonical_record + TITLE_END, 'BLOCKED_TITLE_MARKER_CONFLICT')
    return original, {'successor_manifest_id': record[0], 'successor_manifest_version': record[1],
                      'successor_manifest_digest': record[2], 'successor_link': record[3]}


def handoff_title(current_title, manifest):
    original, previous = parse_handoff_title(current_title)
    scope = manifest_scope(manifest)
    desired_marker = {'successor_manifest_id': manifest['manifest_id'],
                      'successor_manifest_version': manifest['version'],
                      'successor_manifest_digest': scope['manifest_digest'],
                      'successor_link': manifest['target_thread']['link']}
    if previous is not None:
        require(previous == desired_marker or
                (previous['successor_manifest_id'] == manifest['manifest_id'] and
                 previous['successor_manifest_version'] < manifest['version']),
                'BLOCKED_TITLE_MARKER_CONFLICT')
    record = [manifest['manifest_id'], manifest['version'], scope['manifest_digest'],
              manifest['target_thread']['link']]
    utf16_units(record[3])
    desired = original + TITLE_PREFIX + json.dumps(record, ensure_ascii=True, separators=(',', ':')) + TITLE_END
    require(utf16_units(desired) <= 512, 'BLOCKED_TITLE_CAPACITY')
    return desired


class AuthorizationQuery(Protocol):
    def __call__(self, binding: dict, authorization_ref: dict) -> dict: ...


class ReceiptQuery(Protocol):
    def __call__(self, binding: dict, receipt_id: Optional[str]) -> object: ...


class InventoryQuery(Protocol):
    def __call__(self, manifest: dict) -> dict: ...


class T3Adapter:
    """Injected formal T3 read/rename/archive transport; no thread_send.

    The installed title API supports 512 JS UTF-16 units. Original text is
    preserved, and only actual thread.title readback proves the passive link.
    Authority/receipt/inventory providers remain explicit trusted live queries.
    """
    def __init__(self, tool_call, *, authority_query: Optional[AuthorizationQuery] = None,
                 receipt_query: Optional[ReceiptQuery] = None,
                 inventory_query: Optional[InventoryQuery] = None):
        self.tool_call = tool_call
        self.authority_query = authority_query
        self.receipt_query = receipt_query
        self.inventory_query = inventory_query
        self._prior_markers = []

    def set_manifest_history(self, history):
        self._prior_markers = []
        for state in history:
            manifest = state['manifest']
            validate_manifest(manifest, state['member_metadata'])
            require(state['manifest_digest'] == sha256(manifest), 'CORRUPT_MANIFEST_HISTORY')
            self._prior_markers.append({
                'successor_manifest_id': manifest['manifest_id'],
                'successor_manifest_version': manifest['version'],
                'successor_manifest_digest': sha256(manifest),
                'successor_link': manifest['target_thread']['link']})

    def capabilities(self):
        caps = set()
        if callable(self.tool_call):
            caps |= {'thread_read', 'archive', 'successor_link'}
        if callable(self.authority_query):
            caps.add('authorization_query')
        if callable(self.receipt_query):
            caps.add('receipt_query')
        if callable(self.inventory_query):
            caps.add('inventory_query')
        return caps

    def verify_inventory(self, manifest):
        result = self.inventory_query(deepcopy(manifest))
        # Write scope only; never used to construct thread/link readback.
        self._manifest = deepcopy(manifest)
        return result

    def verify_authorization(self, binding, authorization_ref):
        return self.authority_query(binding, authorization_ref)

    def fetch_receipts(self, binding):
        return self.receipt_query(binding, None)

    def verify_receipt(self, binding, receipt):
        return self.receipt_query(binding, receipt['receipt_id'])

    def read_thread(self, thread_id):
        payload = t3_payload(self.tool_call('t3_thread_read', {'threadId': thread_id}))
        thread = payload.get('thread')
        require(isinstance(thread, dict), 'BLOCKED_T3_THREAD_SHAPE')
        # Deliberately no defaults for unknown run/pending/status fields.
        result = {'thread_id': thread.get('thread_id', thread.get('threadId', thread.get('id'))),
                  'link': thread.get('link'), 'archived': thread.get('archived'),
                  'status': thread.get('status'), 'activeRunId': thread.get('activeRunId'),
                  'pendingRequestCount': thread.get('pendingRequestCount')}
        if 'activeRunId' not in thread:
            result.pop('activeRunId')
        require(result['thread_id'] == thread_id and type(result['archived']) is bool,
                'BLOCKED_T3_THREAD_SHAPE')
        result['title'] = thread.get('title')
        _, marker = parse_handoff_title(result['title'])
        if marker is not None:
            result.update(marker)
        return result

    def ensure_successor_link(self, binding, idempotency_key):
        manifest = getattr(self, '_manifest', None)
        require(isinstance(manifest, dict) and binding in manifest['bindings'] and
                idempotency_key == 'manifest:' + sha256(manifest) + ':link',
                'BLOCKED_LINK_MANIFEST_SCOPE')
        old = self.read_thread(manifest['source_thread']['thread_id'])
        HandoffStore._check_source(old, manifest)
        _, existing = parse_handoff_title(old['title'])
        if existing is not None and existing['successor_manifest_digest'] != sha256(manifest):
            require(existing in self._prior_markers, 'BLOCKED_TITLE_MARKER_CONFLICT')
        desired = handoff_title(old['title'], manifest)
        if old['title'] == desired:
            return {'already_present': True}
        require(old['archived'] is False, 'BLOCKED_SOURCE_ALREADY_ARCHIVED')
        # Stable ASCII ID: <=256 UTF16 units, never an unpaired surrogate.
        client_id = 'taskline-title-' + hashlib.sha256(
            desired.encode('utf-16-le', errors='surrogatepass')).hexdigest()
        require(utf16_units(client_id) <= 256, 'BLOCKED_REQUEST_ID_CAPACITY')
        t3_payload(self.tool_call('t3_thread_update', {
            'action': 'rename', 'threadId': manifest['source_thread']['thread_id'],
            'title': desired, 'clientRequestId': client_id}))
        readback = self.read_thread(manifest['source_thread']['thread_id'])
        HandoffStore._check_source(readback, manifest)
        require(readback['title'] == desired, 'BLOCKED_LINK_READBACK')
        HandoffStore._check_link(readback, manifest)
        return {'readback_verified': True}

    def safety(self):
        return {'atomic_conditional_archive': False}

    def archive_thread(self, thread_id, idempotency_key):
        # organize has no documented idempotency parameter. The orchestrator
        # reads before writing and afterwards to reconcile uncertain results.
        return t3_payload(self.tool_call('t3_thread_organize',
                                       {'action': 'archive', 'threadId': thread_id}))


def register(subparsers):
    parser = subparsers.add_parser('taskline', help='独立任务线交接；默认外部动作阻塞')
    parser.add_argument('--state-root', required=True, help='独立、显式的状态目录')
    actions = parser.add_subparsers(dest='taskline_action', required=True)
    prepare = actions.add_parser('prepare', help='保存绑定和准备包；不操作外部系统')
    prepare.add_argument('request', help='含 manifest/member_metadata（或单成员 binding/metadata）的 JSON')
    prepare.add_argument('--upgrade', action='store_true')
    for name in ('status', 'state', 'advance', 'retry'):
        action = actions.add_parser(name)
        action.add_argument('handoff_id')
    from .taskline_host import register_host
    register_host(actions)
    parser.set_defaults(taskline_run=run)


def run(args, adapter=None):
    if args.taskline_action == 'host':
        from .taskline_host import run_host
        return run_host(args)
    store = HandoffStore(args.state_root)
    action = args.taskline_action
    if action == 'prepare':
        request = json.loads(Path(args.request).read_text())
        require(isinstance(request, dict), 'INVALID_REQUEST')
        if set(request) == {'manifest', 'member_metadata'}:
            return store.prepare_manifest(request['manifest'], request['member_metadata'], upgrade=args.upgrade)
        require(set(request) == {'binding', 'metadata'}, 'INVALID_REQUEST')
        return store.prepare(request['binding'], request['metadata'], upgrade=args.upgrade)
    if action in ('status', 'state'):
        return store.status(args.handoff_id)
    return store.advance(args.handoff_id, adapter)
