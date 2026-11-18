"""Durable dispatch routes only. Portable owns all task-line orchestration.

External calls are restricted to injected t3_thread_read/t3_thread_send.
No production defaults, task mutation, worker management or lifecycle actions.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import sys
import uuid
import importlib.util
# Product hosts load this fixed API by file path; resolve its sibling without
# changing sys.path or accepting a caller-supplied plugin/module location.
_inventory_path = Path(__file__).resolve().with_name('taskline_inventory.py')
_inventory_key = '_dispatch_taskline_inventory_' + hashlib.sha256(str(_inventory_path).encode()).hexdigest()[:16]
_inventory = sys.modules.get(_inventory_key)
_inventory_public = sys.modules.get('taskline_inventory')
if (_inventory is None and _inventory_public is not None and
        Path(getattr(_inventory_public,'__file__','')).resolve() == _inventory_path):
    _inventory = _inventory_public
    sys.modules[_inventory_key] = _inventory
if _inventory is None:
    _inventory_spec = importlib.util.spec_from_file_location(_inventory_key, _inventory_path)
    _inventory = importlib.util.module_from_spec(_inventory_spec)
    _inventory_spec.loader.exec_module(_inventory)
    sys.modules[_inventory_key] = _inventory
sys.modules.setdefault('taskline_inventory', _inventory)
HostSessionGate = _inventory.HostSessionGate
ReadOnlyTaskInventory = _inventory.ReadOnlyTaskInventory
ReadOnlyNextStepQuery = _inventory.ReadOnlyNextStepQuery
SessionBlocked = _inventory.SessionBlocked
process_identity = _inventory.process_identity


class HandoffError(ValueError):
    pass


class CapabilityMissing(HandoffError):
    pass


def require(condition, code):
    if not condition:
        raise HandoffError(code)


def canonical(value, *, max_bytes=1024 * 1024):
    try:
        data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                          allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HandoffError('INVALID_JSON') from exc
    if max_bytes is not None and len(data.encode()) > max_bytes:
        raise HandoffError('INPUT_TOO_LARGE')
    return data


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def nonempty(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise HandoffError('INVALID_TEXT')
    return value


def safe_path(value):
    """Require canonical absolute paths; reject symlinks in every existing component."""
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts or str(p) != str(value):
        raise HandoffError('UNSAFE_PATH')
    for part in [*reversed(p.parents), p]:
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise HandoffError('SYMLINK_PATH')
    return p


def read_json(path):
    p = safe_path(path)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise HandoffError('UNSAFE_FILE')
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data.encode()) > 1024 * 1024:
            raise HandoffError('INPUT_TOO_LARGE')
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise HandoffError('DUPLICATE_JSON_KEY')
                result[key] = value
            return result
        return json.loads(data, object_pairs_hook=unique_object)
    finally:
        os.close(fd)


BINDING_KEYS = {'handoff_id', 'version', 'source_thread', 'target_thread',
                'task_id', 'run_id', 'context_sha256'}
RECEIPT_KEYS = {'receipt_id', 'kind', 'binding_digest', 'handoff_id', 'version',
                'task_id', 'run_id', 'source_thread_id', 'target_thread_id',
                'event_id', 'payload_sha256', 'provenance'}
CONSUMER_SCHEMA = 'dispatch.taskline.consumer/v1'
EVENT_SCHEMA = 'dispatch.taskline.event/v1'


def sha256(value):
    return digest(value)


def hash_value(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def local_id(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value), 'INVALID_ID')
    return value


def validate_binding(binding):
    require(isinstance(binding, dict) and set(binding) == BINDING_KEYS, 'INVALID_BINDING')
    canonical(binding)
    local_id(binding['handoff_id'])
    require(type(binding['version']) is int and binding['version'] >= 1, 'INVALID_VERSION')
    for key in ('task_id', 'run_id'):
        local_id(binding[key])
    require(hash_value(binding['context_sha256']), 'INVALID_CONTEXT_HASH')
    for key in ('source_thread', 'target_thread'):
        thread = binding[key]
        require(isinstance(thread, dict) and set(thread) == {'thread_id', 'link'}, 'INVALID_THREAD')
        ident = nonempty(thread['thread_id'])
        require(re.fullmatch(r'[A-Za-z0-9_:.-]{1,256}', ident), 'INVALID_THREAD_ID')
        link = nonempty(thread['link'])
        # Tool-returned display links are opaque and preserved byte-for-byte.
        match = re.fullmatch(r'<t3-thread://v2/([^/<>\s]+)/([^<>\s]+)>', link)
        require(match is not None and match[2] == ident, 'INVALID_THREAD_LINK')
    require(binding['source_thread'] != binding['target_thread'] and
            binding['source_thread']['thread_id'] != binding['target_thread']['thread_id'], 'SAME_THREAD')


def validate_metadata(binding, metadata):
    require(isinstance(metadata, dict) and set(metadata) ==
            {'context_ref', 'authorization_ref', 'current_caller', 'scope', 'worker_active', 'todos'},
            'INVALID_METADATA')
    canonical(metadata)
    for key in ('context_ref', 'authorization_ref'):
        ref = metadata[key]
        require(isinstance(ref, dict) and set(ref) == {'ref', 'sha256'} and
                hash_value(ref['sha256']), 'INVALID_REFERENCE')
        nonempty(ref['ref'])
    require(metadata['context_ref']['sha256'] == binding['context_sha256'], 'CONTEXT_MISMATCH')
    nonempty(metadata['current_caller'])
    require(metadata['scope'] in ('engineering', 'paused', 'read_only'), 'INVALID_SCOPE')
    require(type(metadata['worker_active']) is bool, 'INVALID_WORKER_STATE')
    require(isinstance(metadata['todos'], list), 'INVALID_TODOS')
    for todo in metadata['todos']:
        nonempty(todo)


def receipt_binding(binding):
    return dict(binding_digest=digest(binding), handoff_id=binding['handoff_id'],
                version=binding['version'], task_id=binding['task_id'], run_id=binding['run_id'],
                source_thread_id=binding['source_thread']['thread_id'],
                target_thread_id=binding['target_thread']['thread_id'])


def validate_receipt(binding, receipt, event=None):
    require(isinstance(receipt, dict) and RECEIPT_KEYS <= set(receipt), 'INVALID_RECEIPT')
    canonical(receipt)
    for key, value in receipt_binding(binding).items():
        require(type(receipt.get(key)) is type(value) and receipt[key] == value, 'RECEIPT_BINDING_MISMATCH')
    require(receipt['kind'] == 'route_consumed', 'INVALID_RECEIPT_KIND')
    nonempty(receipt['receipt_id'])
    nonempty(receipt['provenance'])
    nonempty(receipt['event_id'])
    require(hash_value(receipt['payload_sha256']), 'INVALID_PAYLOAD_HASH')
    if event is not None:
        require(receipt['event_id'] == event['event_id'] and
                receipt['payload_sha256'] == event['payload_sha256'], 'RECEIPT_EVENT_MISMATCH')
    if 'context_sha256' in receipt:
        require(receipt['context_sha256'] == binding['context_sha256'], 'RECEIPT_CONTEXT_MISMATCH')
    require(not ({'ready', 'archived'} & set(receipt)), 'INVALID_RECEIPT_FIELDS')


def probe_id(binding):
    return 'probe-' + digest(binding)


def _run_owner(task, run_id, owner):
    if not isinstance(owner, str) or not owner or not isinstance(task.get('caller'), str):
        return False
    expected = task['caller']
    if owner == expected:
        return True
    transfers = task.get('transfers', [])
    if not isinstance(transfers, list):
        return False
    for event in reversed(transfers):
        if (not isinstance(event, dict) or event.get('new_caller') != expected
                or not isinstance(event.get('runs'), list)):
            return False
        # Same fields written by dispatch-tasks.transfer_task; no grants inferred.
        if not all(isinstance(event.get(k), str) and event[k] for k in
                   ('old_caller', 'at', 'reason', 'duty_path')) or not hash_value(event.get('duty_sha256')):
            return False
        if owner == event['old_caller'] and run_id in event['runs']:
            return True
        expected = event['old_caller']
    return False


def observe_dispatch(base, task_id, run_id):
    """Bounded readonly snapshot of actual dispatch schema, never an authority grant."""
    base = safe_path(base)
    local_id(task_id)
    local_id(run_id)
    task_path = str(base / 'tasks' / (task_id + '.json'))
    meta_path = str(base / 'dispatches' / run_id / 'meta.json')
    task, meta = read_json(task_path), read_json(meta_path)
    require(isinstance(task, dict) and isinstance(meta, dict) and
            task.get('task_id') == task_id and meta.get('task_id') == task_id and
            meta.get('id') == run_id and isinstance(task.get('runs'), list) and
            run_id in task['runs'], 'DISPATCH_BINDING_MISMATCH')
    require(_run_owner(task, run_id, meta.get('session')), 'DISPATCH_AUTHORITY_EVIDENCE_MISSING')
    # A concurrent formal update is not silently treated as an atomic snapshot.
    require(canonical(read_json(task_path)) == canonical(task) and
            canonical(read_json(meta_path)) == canonical(meta), 'DISPATCH_SNAPSHOT_CHANGED')
    result = dict(task_id=task_id, run_id=run_id, task_status=task.get('status'),
                  run_status=meta.get('status'), current_run=task.get('current_run'),
                  exit_code=meta.get('exit_code'), finished=meta.get('finished'),
                  task_sha256=digest(task), meta_sha256=digest(meta), atomic=False,
                  provenance={'operation': 'readonly_dispatch_files',
                              'task_ref': task_path, 'run_ref': meta_path})
    # No raw logs/prompts/environment. Only the known final answer artifact, bounded.
    answer = safe_path(str(base / 'dispatches' / run_id / 'final-answer.txt'))
    if answer.exists():
        fd = os.open(answer, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_size <= 256 * 1024, 'UNSAFE_RESULT_FILE')
            content = stream.read(256 * 1024 + 1)
        require(len(content) <= 256 * 1024, 'RESULT_TOO_LARGE')
        result['result'] = dict(ref=str(answer), sha256=hashlib.sha256(content).hexdigest(),
                                text=content.decode('utf-8'))
    result['source_signature'] = digest(result)
    return result


class RouteStore:
    """Dedicated SQLite journal, FULL commits and flock; no orchestration state."""
    def __init__(self, state_dir, adapter=None):
        self.root = safe_path(state_dir)
        require(not any(p in {'tasks', 'dispatches', 'claims', 'task-claims'} for p in self.root.parts),
                'STATE_OVERLAPS_DISPATCH_DATA')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and
                not info.st_mode & 0o022, 'UNSAFE_STATE_PERMISSIONS')
        require(not (self.root / 'journal.sqlite3').exists(), 'LEGACY_STATE_REQUIRES_REVIEW')
        self.adapter = adapter
        if isinstance(adapter, T3Relay):
            adapter.runtime_store = self
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS routes(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    route TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                    receipt TEXT, attempts INTEGER NOT NULL DEFAULT 0, UNIQUE(route,id));
                CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runtimes(binding_digest TEXT PRIMARY KEY, body TEXT NOT NULL);
            ''')

    @contextmanager
    def _db(self, lock='.lock'):
        safe_path(str(self.root))
        info = self.root.stat()
        require(info.st_uid == os.getuid() and not info.st_mode & 0o022, 'UNSAFE_STATE_PERMISSIONS')
        fd = os.open(safe_path(str(self.root / lock)), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_nlink == 1, 'UNSAFE_LOCK')
            fcntl.flock(fd, fcntl.LOCK_EX)
            for name in ('routes.sqlite3', 'routes.sqlite3-journal', 'routes.sqlite3-wal', 'routes.sqlite3-shm'):
                path = safe_path(str(self.root / name))
                if path.exists():
                    info = path.stat()
                    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'UNSAFE_STATE_FILE')
            db = sqlite3.connect(self.root / 'routes.sqlite3', timeout=30)
            try:
                db.execute('PRAGMA synchronous=FULL')
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        finally:
            os.close(fd)

    def _route(self, db, handoff_id):
        row = db.execute('SELECT body FROM routes WHERE id=?', (local_id(handoff_id),)).fetchone()
        require(row is not None, 'ROUTE_NOT_FOUND')
        record = json.loads(row[0])
        require(record['binding_digest'] == digest(record['binding']), 'CORRUPT_BINDING')
        return record

    def prepare(self, binding, metadata):
        binding, metadata = deepcopy(binding), deepcopy(metadata)
        validate_binding(binding)
        validate_metadata(binding, metadata)
        record = dict(binding=binding, binding_digest=digest(binding), metadata=metadata)
        with self._db() as db:
            old = db.execute('SELECT body FROM routes WHERE id=?', (binding['handoff_id'],)).fetchone()
            if old:
                require(old[0] == canonical(record), 'ROUTE_CONFLICT')
                return json.loads(old[0])
            db.execute('INSERT INTO routes VALUES (?,?)', (binding['handoff_id'], canonical(record)))
        return record

    def record(self, handoff_id):
        with self._db() as db:
            return self._route(db, handoff_id)

    def publish(self, handoff_id, event_id, payload):
        nonempty(event_id)
        canonical(payload)
        with self._db() as db:
            route = self._route(db, handoff_id)
            event = dict(event_id=event_id, payload=deepcopy(payload), payload_sha256=digest(payload),
                         **receipt_binding(route['binding']))
            # event_id is task/run scoped globally, as well as route scoped.
            for (body,) in db.execute('SELECT body FROM events WHERE id=?', (event_id,)):
                other = json.loads(body)
                require(all(other[k] == event[k] for k in ('task_id', 'run_id', 'payload_sha256')),
                        'EVENT_CONFLICT')
            old = db.execute('SELECT body FROM events WHERE route=? AND id=?', (handoff_id, event_id)).fetchone()
            if old:
                require(old[0] == canonical(event), 'EVENT_CONFLICT')
                return json.loads(old[0])
            db.execute('INSERT INTO events(route,id,body) VALUES (?,?,?)', (handoff_id, event_id, canonical(event)))
        return event

    def pending(self, handoff_id):
        with self._db() as db:
            self._route(db, handoff_id)
            return [dict(json.loads(body), seq=seq, attempts=attempts) for seq, body, attempts in
                    db.execute('SELECT seq,body,attempts FROM events WHERE route=? AND receipt IS NULL ORDER BY seq',
                               (handoff_id,))]

    def _authorized(self, route):
        adapter = self.adapter
        fn = getattr(adapter, 'verify_authorization', None)
        if not callable(fn):
            raise CapabilityMissing('BLOCKED_CAPABILITY:authority_query')
        if isinstance(adapter, T3Relay):
            adapter.runtime_store = self
            adapter.verify_session(route['binding'], route['metadata'])
        binding, metadata = route['binding'], route['metadata']
        result = fn(deepcopy(binding), deepcopy(metadata['authorization_ref']))
        expected = dict(binding_digest=route['binding_digest'], current_caller=metadata['current_caller'],
                        authorization_ref=metadata['authorization_ref'], scope=metadata['scope'])
        require(isinstance(result, dict) and result.get('authorized') is True and
                all(canonical(result.get(k)) == canonical(v) for k, v in expected.items()), 'BLOCKED_AUTHORITY')
        nonempty(result.get('provenance'))
        return result

    def acknowledge(self, handoff_id, event_id, receipt):
        candidate = deepcopy(receipt)
        with self._db() as db:
            route = self._route(db, handoff_id)
            row = db.execute('SELECT body,receipt FROM events WHERE route=? AND id=?', (handoff_id, event_id)).fetchone()
        require(row is not None, 'EVENT_NOT_FOUND')
        event = json.loads(row[0])
        validate_receipt(route['binding'], candidate, event)
        self._authorized(route)
        verify = getattr(self.adapter, 'verify_receipt', None)
        if not callable(verify):
            raise CapabilityMissing('BLOCKED_CAPABILITY:receipt_query')
        queried = verify(deepcopy(route['binding']), deepcopy(candidate))
        require(isinstance(queried, dict), 'UNVERIFIED_RECEIPT')
        immutable = {k: v for k, v in candidate.items() if k != 'persistent_route'}
        require(canonical({k: v for k, v in queried.items() if k != 'persistent_route'}) ==
                canonical(immutable), 'UNVERIFIED_RECEIPT')
        validate_receipt(route['binding'], queried, event)
        body = canonical(immutable)
        with self._db() as db:
            current = db.execute('SELECT body,receipt FROM events WHERE route=? AND id=?', (handoff_id,event_id)).fetchone()
            require(current is not None and current[0] == row[0], 'EVENT_CONFLICT')
            prior = db.execute('SELECT body FROM receipts WHERE id=?', (candidate['receipt_id'],)).fetchone()
            require(prior is None or prior[0] == body, 'RECEIPT_CONFLICT')
            require(current[1] is None or current[1] == body, 'RECEIPT_CONFLICT')
            db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?)', (candidate['receipt_id'], body))
            db.execute('UPDATE events SET receipt=? WHERE route=? AND id=?', (body, handoff_id, event_id))
        return immutable

    def observe(self, handoff_id, dispatch_root):
        binding = self.record(handoff_id)['binding']
        snapshot = observe_dispatch(dispatch_root, binding['task_id'], binding['run_id'])
        return self.publish(handoff_id, 'dispatch-' + snapshot['source_signature'], snapshot)

    def note_attempt(self, handoff_id, event_id):
        with self._db() as db:
            db.execute('UPDATE events SET attempts=attempts+1 WHERE route=? AND id=? AND receipt IS NULL',
                       (handoff_id, event_id))

    def status(self, handoff_id):
        with self._db() as db:
            route = self._route(db, handoff_id)
            rows = list(db.execute('SELECT seq,body,receipt FROM events WHERE route=? ORDER BY seq', (handoff_id,)))
        consumed = [json.loads(r) for _, _, r in rows if r is not None]
        health, verified = None, False
        probe = next((r for r in consumed if r['event_id'] == probe_id(route['binding'])), None)
        blocker = None
        if probe is not None and self.adapter is not None:
            try:
                self._authorized(route)
                checked = self.adapter.verify_receipt(deepcopy(route['binding']), deepcopy(probe))
                health = self.adapter.route_health(deepcopy(route['binding']))
                verified = isinstance(checked, dict) and checked.get('persistent_route') is True and health is not None
            except Exception as exc:
                blocker = str(exc) if isinstance(exc, HandoffError) else 'BLOCKED_TRANSPORT'
        return dict(**route, pending_count=sum(r is None for _, _, r in rows),
                    consumed_count=len(consumed), cursor=max((seq for seq, _, r in rows if r is not None), default=0),
                    receipts=consumed, persistent_route=verified, runtime_health=health, blocker=blocker)


def t3_payload(raw):
    for _ in range(5):
        require(isinstance(raw, dict) and raw.get('isError') is not True and
                raw.get('ok') is not False and not raw.get('error'), 'BLOCKED_T3_TOOL_ERROR')
        canonical(raw, max_bytes=2 * 1024 * 1024)
        if 'structuredContent' in raw:
            raw = raw['structuredContent']
        elif 'content' in raw:
            content = raw['content']
            require(isinstance(content, list) and len(content) == 1 and
                    content[0].get('type') == 'text', 'BLOCKED_T3_ENVELOPE')
            raw = json.loads(content[0]['text'])
        elif 'result' in raw and 'thread' not in raw:
            raw = raw['result']
        else:
            return raw
    raise HandoffError('BLOCKED_T3_ENVELOPE')


class T3Relay:
    """Concrete formal-tool relay. tool_call(name, arguments) is host-bound RPC.

    Only read/send are used, with actual positional pagination and auto mode.
    Runtime health is verified by the local RouteRuntime lease, never input JSON.
    """
    def __init__(self, tool_call=None, *, authority_query=None, host_bridge=None, send_mode='auto',
                 max_pages=20, max_items=1000, page_limit=100, max_chars_per_item=50000):
        self.tool_call = tool_call
        self.authority_query = authority_query
        self.session_gate = HostSessionGate(host_bridge)
        self.runtime_store = None
        require(send_mode == 'auto', 'INVALID_SEND_MODE')
        self.send_mode = send_mode
        require(type(max_pages) is int and 1 <= max_pages <= 100 and
                type(max_items) is int and 1 <= max_items <= 10000 and
                type(page_limit) is int and 1 <= page_limit <= 100 and
                type(max_chars_per_item) is int and 1024 <= max_chars_per_item <= 50000,
                'INVALID_SCAN_LIMIT')
        self.max_pages, self.max_items = max_pages, max_items
        self.page_limit, self.max_chars_per_item = page_limit, max_chars_per_item

    def capabilities(self):
        caps = set()
        if callable(self.tool_call):
            caps |= {'thread_read', 'thread_send', 'receipt_query'}
        if callable(self.authority_query):
            caps.add('authorization_query')
        return caps

    def verify_session(self, binding, metadata, *, engineering=False):
        if not callable(self.tool_call):
            raise CapabilityMissing('BLOCKED_RPC_SESSION_UNAVAILABLE')
        try:
            return self.session_gate.verify(binding, metadata, engineering=engineering)
        except SessionBlocked as exc:
            raise HandoffError(str(exc)) from None

    def verify_authorization(self, binding, authorization_ref):
        if not callable(self.authority_query):
            raise CapabilityMissing('BLOCKED_CAPABILITY:authority_query')
        return self.authority_query(deepcopy(binding), deepcopy(authorization_ref))

    def route_health(self, binding):
        if self.runtime_store is None:
            return None
        result = RouteRuntime.query(self.runtime_store, binding)
        if result is not None:
            self._available_target(binding)
        return result

    def _tool(self, name, args):
        if not callable(self.tool_call):
            raise CapabilityMissing('BLOCKED_CAPABILITY:transport')
        require(name in ('t3_thread_read', 't3_thread_send'), 'UNSUPPORTED_TOOL')
        return t3_payload(self.tool_call(name, deepcopy(args)))

    def _receipts(self, binding):
        validate_binding(binding)
        target = binding['target_thread']
        require(self.runtime_store is not None, 'BLOCKED_CAPABILITY:route_binding')
        metadata = self.runtime_store.record(binding['handoff_id'])['metadata']
        session = self.verify_session(binding, metadata)
        proof, identity = session['proof'], self.session_gate.pin
        position = 0
        args = {'threadId': target['thread_id'], 'afterPosition': position,
                'limit': self.page_limit, 'maxCharsPerItem': self.max_chars_per_item}
        found, cursors, count, byte_count = {}, set(), 0, 0
        for _ in range(self.max_pages):
            page = self._tool('t3_thread_read', args)
            byte_count += len(canonical(page).encode())
            require(byte_count <= 2 * 1024 * 1024, 'BLOCKED_SCAN_BYTE_LIMIT')
            thread, items = page.get('thread'), page.get('items')
            require(isinstance(thread, dict) and
                    thread.get('threadId', thread.get('id', thread.get('thread_id'))) == target['thread_id'] and
                    thread.get('link') == target['link'] and type(thread.get('archived')) is bool and
                    isinstance(items, list), 'BLOCKED_T3_THREAD_SHAPE')
            # An already-relayed target can still be queried for its prior receipt.
            # Availability for new sends is checked independently by send().
            count += len(items)
            require(count <= self.max_items, 'BLOCKED_SCAN_ITEM_LIMIT')
            for item in items:
                if not isinstance(item, dict) or item.get('type') != 'assistant_message':
                    continue
                if (item.get('createdBy') != 'agent' or item.get('visibility') != 'local'
                        or item.get('status') != 'completed'
                        or item.get('sourceThreadId') != target['thread_id']
                        or any(item.get(k, False) is not False for k in
                               ('truncated', 'textTruncated', 'isTruncated', 'contentTruncated'))):
                    continue
                text = item.get('text')
                if not isinstance(text, str):
                    continue
                try:
                    marker = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(marker, dict):
                    continue
                if marker.get('schema') == CONSUMER_SCHEMA:
                    require(set(marker) == {'schema','receipt'}, 'INVALID_CONSUMER_MARKER')
                    candidates = [marker['receipt']]
                elif marker.get('schema') == 'portable.agent-consumption/v1':
                    require(set(marker) == {'schema','session','receipts'}, 'INVALID_CONSUMER_MARKER')
                    if canonical(marker['session']) != canonical(identity):
                        continue
                    require(isinstance(marker['receipts'],list), 'INVALID_CONSUMER_MARKER')
                    candidates = marker['receipts']
                else:
                    continue
                relevant = [r for r in candidates if isinstance(r,dict) and
                            r.get('handoff_id') == binding['handoff_id'] and r.get('kind') == 'route_consumed']
                if not relevant:
                    continue
                nonempty(item.get('itemId'))
                verify_actor = getattr(self.session_gate.bridge,'verify_item_actor',None)
                require(callable(verify_actor), 'BLOCKED_RECEIPT_ACTOR_MAPPING_UNAVAILABLE')
                actor = verify_actor(deepcopy(item),deepcopy(proof))
                require(isinstance(actor,dict) and actor.get('session_digest') == digest(identity) and
                        actor.get('item_id') == item['itemId'] and isinstance(actor.get('provenance'),str) and
                        bool(actor['provenance']), 'BLOCKED_RECEIPT_ACTOR_UNVERIFIED')
                for receipt in relevant:
                    receipt = deepcopy(receipt)
                    validate_receipt(binding, receipt)
                    if 'session_digest' in receipt:
                        require(receipt['session_digest'] == digest(identity) and
                                receipt.get('native_caller') == proof['native_caller'] and
                                type(receipt.get('generation')) is int and receipt['generation'] == proof['generation'],
                                'BLOCKED_RECEIPT_WRONG_SESSION')
                    receipt.pop('persistent_route',None)
                    key = receipt['receipt_id']
                    require(key not in found or canonical(found[key]) == canonical(receipt), 'RECEIPT_CONFLICT')
                    found[key] = receipt
            more, next_position = page.get('hasMore'), page.get('nextPosition')
            require(type(more) is bool and 'nextPosition' in page, 'BLOCKED_SCAN_INCOMPLETE')
            numeric = type(next_position) in (int, float) and math.isfinite(next_position)
            if not more:
                require(next_position is None or (numeric and next_position >= position),
                        'BLOCKED_SCAN_INCONSISTENT')
                self.verify_session(binding,metadata)
                return list(found.values())
            require(numeric and next_position > position and next_position not in cursors,
                    'BLOCKED_SCAN_POSITION_INVALID')
            cursors.add(next_position)
            position = next_position
            args = {'threadId': target['thread_id'], 'afterPosition': position,
                    'limit': self.page_limit, 'maxCharsPerItem': self.max_chars_per_item}
        raise HandoffError('BLOCKED_SCAN_PAGE_LIMIT')

    def _runtime_receipt(self, binding, receipt):
        result = deepcopy(receipt)
        if result['event_id'] == probe_id(binding):
            try:
                result['persistent_route'] = self.route_health(binding) is not None
            except Exception:
                # Consumption remains a fact when the separately queried runtime fails.
                result['persistent_route'] = False
        return result

    def fetch_receipts(self, binding):
        return [self._runtime_receipt(binding, receipt) for receipt in self._receipts(binding)]

    def receipt_query(self, binding, receipt_id=None):
        """Portable host bridge: query(binding, None|receipt_id), never cached ACK."""
        receipts = self.fetch_receipts(binding)
        if receipt_id is None:
            return receipts
        matches = [r for r in receipts if r['receipt_id'] == receipt_id]
        require(len(matches) == 1, 'BLOCKED_CONSUMPTION_NOT_FOUND')
        return matches[0]

    def verify_receipt(self, binding, receipt):
        validate_receipt(binding, receipt)
        matches = [r for r in self._receipts(binding) if r['receipt_id'] == receipt['receipt_id']]
        require(len(matches) == 1, 'BLOCKED_CONSUMPTION_NOT_FOUND')
        actual = self._runtime_receipt(binding, matches[0])
        supplied = {k: v for k, v in receipt.items() if k != 'persistent_route'}
        require(canonical(matches[0]) == canonical(supplied), 'UNVERIFIED_RECEIPT')
        if 'persistent_route' in receipt:
            require(receipt['persistent_route'] is actual.get('persistent_route'), 'BLOCKED_RUNTIME_LEASE')
        return actual

    def find_receipt(self, binding, event):
        matches = [r for r in self.fetch_receipts(binding) if r['event_id'] == event['event_id']]
        require(len(matches) <= 1, 'RECEIPT_CONFLICT')
        if not matches:
            return None
        validate_receipt(binding, matches[0], event)
        return matches[0]

    def probe_payload(self, binding):
        return {'kind':'route_probe','binding_digest':digest(binding)}

    def wire_event(self, binding, event):
        # One canonical wire. Portable's product DispatchRouteBridge validates
        # this durable event and adds manifest/context envelope without changing payload.
        return dict(schema=EVENT_SCHEMA,binding=deepcopy(binding),binding_digest=digest(binding),
                    event_id=event['event_id'],payload_sha256=event['payload_sha256'],payload=deepcopy(event['payload']))

    def _available_target(self, binding):
        target = binding['target_thread']
        page = self._tool('t3_thread_read', {'threadId': target['thread_id'],
                                                'afterPosition': 0, 'limit': 1,
                                                'maxCharsPerItem': 1024})
        thread = page.get('thread', {})
        require(thread.get('threadId', thread.get('id', thread.get('thread_id'))) == target['thread_id'] and
                thread.get('link') == target['link'] and thread.get('archived') is False and
                thread.get('status') not in ('failed', 'deleted', 'error'), 'BLOCKED_TARGET_UNAVAILABLE')

    def send(self, binding, event):
        require(self.runtime_store is not None, 'BLOCKED_CAPABILITY:route_binding')
        route = self.runtime_store.record(binding['handoff_id'])
        require(route['binding_digest'] == digest(binding), 'RECEIPT_BINDING_MISMATCH')
        self.runtime_store._authorized(route)
        require(route['metadata']['scope'] == 'engineering',
                'BLOCKED_SCOPE_' + route['metadata']['scope'].upper())
        require(all(event.get(k) == v for k, v in receipt_binding(binding).items()) and
                event.get('payload_sha256') == digest(event.get('payload')), 'EVENT_CONFLICT')
        target = binding['target_thread']
        self._available_target(binding)
        key = 'dispatch-taskline-' + digest([digest(binding), event['event_id'], event['payload_sha256']])
        envelope = self.wire_event(binding,event)
        result = self._tool('t3_thread_send', dict(threadId=target['thread_id'], message=canonical(envelope),
                                                  clientRequestId=key, mode=self.send_mode))
        require(isinstance(result.get('messageId'), str) and result['messageId'] and
                'runId' in result and 'delivery' in result, 'BLOCKED_SEND_OUTCOME_UNKNOWN')
        return dict(client_request_id=key, message_id=result['messageId'], accepted=True, consumed=False)


def pump_once(store, handoff_id, *, dispatch_root=None):
    """Caller-owned single iteration. Never starts a worker or changes task state."""
    try:
        route = store.record(handoff_id)
        store._authorized(route)
        relay = store.adapter
        require(isinstance(relay, T3Relay), 'BLOCKED_CAPABILITY:T3Relay')
        if dispatch_root is not None:
            store.observe(handoff_id, dispatch_root)
        binding = route['binding']
        # Persistent routing requires an actual consumed probe plus a live local runtime lease.
        store.publish(handoff_id, probe_id(binding), relay.probe_payload(binding))
        for event in store.pending(handoff_id):
            receipt = relay.find_receipt(binding, event)
            if receipt is None:
                require(route['metadata']['scope'] == 'engineering',
                        'BLOCKED_SCOPE_' + route['metadata']['scope'].upper())
                store.note_attempt(handoff_id, event['event_id'])
                relay.send(binding, event)
                receipt = relay.find_receipt(binding, event)
            if receipt is not None:
                store.acknowledge(handoff_id, event['event_id'], receipt)
        result = store.status(handoff_id)
        result['blocked'] = result['pending_count'] > 0
        if result['blocked']:
            result['blocker'] = ('AWAITING_TARGET_CONSUMPTION' if result['pending_count'] else
                                 result['blocker'] or 'BLOCKED_CAPABILITY:runtime_health')
        return result
    except Exception as exc:
        return dict(handoff_id=handoff_id, blocked=True,
                    blocker=str(exc) if isinstance(exc, HandoffError) else 'BLOCKED_TRANSPORT_UNKNOWN',
                    pending_count=len(store.pending(handoff_id)))




class RouteRuntime:
    """Explicit caller-owned pump lease, held by a real process and flock.

    Lease files/rows alone cannot prove an active runtime. query checks kernel
    PID/start identity, current heartbeat, matching lock token AND busy flock.
    """
    def __init__(self, store, handoff_id, *, ttl=15):
        require(type(ttl) in (int, float) and math.isfinite(ttl) and 1 <= ttl <= 60,
                'INVALID_RUNTIME_TTL')
        self.store, self.handoff_id, self.ttl = store, handoff_id, ttl
        self.binding = store.record(handoff_id)['binding']
        self.key = digest(self.binding)
        self.fd = None
        self.lease = None

    @staticmethod
    def _lock_path(store, binding):
        return safe_path(str(store.root / ('.runtime-' + digest(binding) + '.lock')))

    def __enter__(self):
        route = self.store.record(self.handoff_id)
        require(route['metadata']['scope'] == 'engineering', 'BLOCKED_SCOPE_' + route['metadata']['scope'].upper())
        require(isinstance(self.store.adapter,T3Relay), 'BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE')
        self.store._authorized(route)
        path = self._lock_path(self.store, self.binding)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'UNSAFE_RUNTIME_LOCK')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HandoffError('BLOCKED_RUNTIME_ALREADY_ACTIVE') from None
            session = self.store.adapter.verify_session(self.binding, route['metadata'], engineering=True)
            identity = process_identity(os.getpid())
            self.lease = dict(host_session={k: session['proof'][k] for k in
                              ('environment_id','app_thread_id','native_caller','provider_session_id','provider_instance_id','generation','scope')}, binding_digest=self.key, runtime_id=uuid.uuid4().hex,
                              lease_id=uuid.uuid4().hex, **identity)
            os.ftruncate(fd, 0)
            os.write(fd, canonical(self.lease).encode())
            os.fsync(fd)
            self.fd = fd
            self.heartbeat()
            return self
        except BaseException:
            self.fd = None
            os.close(fd)
            raise

    def heartbeat(self):
        require(self.fd is not None and process_identity(os.getpid()) ==
                {k: self.lease[k] for k in ('pid', 'process_start')}, 'BLOCKED_RUNTIME_IDENTITY')
        route = self.store.record(self.handoff_id)
        session = self.store.adapter.verify_session(self.binding, route['metadata'], engineering=True)
        require(self.lease['host_session'] == self.store.adapter.session_gate.pin,
                'BLOCKED_SESSION_GENERATION_STALE')
        now = time.time()
        body = dict(self.lease, heartbeat=now, expires_at=now + self.ttl,
                    provenance='RouteRuntime:kernel-identity+flock+heartbeat')
        with self.store._db() as db:
            db.execute('INSERT INTO runtimes VALUES (?,?) ON CONFLICT(binding_digest) DO UPDATE SET body=excluded.body',
                       (self.key, canonical(body)))
        return body

    def __exit__(self, *exc):
        if self.fd is None:
            return
        if os.getpid() != self.lease['pid']:
            os.close(self.fd)
            self.fd = None
            return
        try:
            with self.store._db() as db:
                row = db.execute('SELECT body FROM runtimes WHERE binding_digest=?', (self.key,)).fetchone()
                if row and json.loads(row[0]).get('lease_id') == self.lease['lease_id']:
                    db.execute('DELETE FROM runtimes WHERE binding_digest=?', (self.key,))
        finally:
            os.close(self.fd)
            self.fd = None

    @staticmethod
    def query(store, binding):
        with store._db() as db:
            row = db.execute('SELECT body FROM runtimes WHERE binding_digest=?', (digest(binding),)).fetchone()
        if row is None:
            return None
        lease = json.loads(row[0])
        try:
            route = store.record(binding['handoff_id'])
            require(isinstance(store.adapter, T3Relay), 'BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE')
            store.adapter.verify_session(binding, route['metadata'], engineering=True)
            require(lease.get('host_session') == store.adapter.session_gate.pin, 'BLOCKED_SESSION_GENERATION_STALE')
            now = time.time()
            require(lease.get('binding_digest') == digest(binding) and
                    type(lease.get('heartbeat')) in (int, float) and
                    type(lease.get('expires_at')) in (int, float) and
                    now - 60 <= lease['heartbeat'] <= now < lease['expires_at'] <= now + 60,
                    'BLOCKED_RUNTIME_LEASE')
            require(process_identity(lease.get('pid')) ==
                    {k: lease.get(k) for k in ('pid', 'process_start')}, 'BLOCKED_RUNTIME_IDENTITY')
            fd = os.open(RouteRuntime._lock_path(store, binding), os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 16384,
                        'UNSAFE_RUNTIME_LOCK')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    token = json.loads(os.read(fd, 16385))
                    require(all(token.get(k) == lease.get(k) for k in
                                ('binding_digest', 'runtime_id', 'lease_id', 'pid', 'process_start')),
                            'BLOCKED_RUNTIME_IDENTITY')
                    return dict(lease, checked_at=now)
                # Acquiring this lock proves there is no live registered pump holder.
                fcntl.flock(fd, fcntl.LOCK_UN)
                return None
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError):
            return None


def pump_loop(store, handoff_id, *, dispatch_root=None, iterations=1, interval=1, stop_event=None):
    """Explicit synchronous loop, no detached process, no automatic startup."""
    require(type(iterations) is int and 1 <= iterations <= 10000 and
            type(interval) in (int, float) and 0 <= interval <= 5, 'INVALID_LOOP_LIMIT')
    with RouteRuntime(store, handoff_id) as runtime:
        for index in range(iterations):
            if stop_event is not None and stop_event.is_set():
                break
            runtime.heartbeat()
            result = pump_once(store, handoff_id, dispatch_root=dispatch_root)
            runtime.heartbeat()
            if index + 1 < iterations:
                if stop_event is not None:
                    stop_event.wait(interval)
                else:
                    time.sleep(interval)
        last = store.status(handoff_id)
    # A completed finite loop is no longer a persistent runtime.
    result = store.status(handoff_id)
    result.update(runtime_stopped=True, last_iteration_persistent_route=last['persistent_route'],
                  blocked=result['pending_count'] > 0)
    return result
