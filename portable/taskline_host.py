"""Taskline host candidate: official ACP transport, local consumer and lifecycle.

No credential cache, guessed HTTP, private database or user-loaded plugin. The
formal HostBridge is deliberately unavailable until T3 can attest native caller.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
import itertools
import math
import os
from pathlib import Path
import selectors
import signal
import shlex
import stat
import subprocess
import time
from typing import Protocol

from .core import Blocked, atomic
from .taskline_handoff import (HandoffStore, T3Adapter, canonical, hash_value,
                               manifest_scope, nonempty, require, sha256, t3_payload)

SESSION_SCHEMA = 'portable.host-session/v1'
EVENT_SCHEMA = 'portable.host-event/v1'
PUBLICATION_SCHEMA = 'portable.agent-consumption/v1'
SESSION_KEYS = {'schema', 'environment_id', 'app_thread_id', 'native_caller',
                'provider_session_id', 'provider_instance_id', 'generation', 'scope',
                'issued_at', 'expires_at', 'provenance'}
IDENTITY_KEYS = ('environment_id', 'app_thread_id', 'native_caller', 'provider_session_id',
                 'provider_instance_id', 'generation', 'scope')
TOOLS = {'t3_thread_read', 't3_thread_update', 't3_thread_organize', 't3_thread_send'}


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'BLOCKED_RPC_JSON')
            result[key] = value
        return result
    def constant(_):
        raise Blocked('BLOCKED_RPC_JSON')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, TypeError):
        raise Blocked('BLOCKED_RPC_JSON') from None


class ACPCLITransport:
    """Invoke the documented t3 acp-mcp-call argv; inherit current env only."""
    def __init__(self, executable='t3', *, timeout=20, max_output=1024 * 1024):
        require(nonempty(executable), 'BLOCKED_RPC_EXECUTABLE')
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 60,
                'INVALID_RPC_TIMEOUT')
        require(type(max_output) is int and 1024 <= max_output <= 4 * 1024 * 1024,
                'INVALID_RPC_OUTPUT_LIMIT')
        self.executable, self.timeout, self.max_output = executable, timeout, max_output
        self.send_authorizer = None

    def available(self):
        return all(nonempty(os.environ.get(key)) for key in
                   ('T3_ACP_MCP_ENDPOINT', 'T3_ACP_MCP_AUTHORIZATION'))

    @staticmethod
    def _secret_echo(raw):
        return any(os.environ.get(key) and os.environ[key].encode() in raw for key in
                   ('T3_ACP_MCP_ENDPOINT', 'T3_ACP_MCP_AUTHORIZATION'))

    @classmethod
    def _json_secret_echo(cls, value):
        pending = [value]
        while pending:
            part = pending.pop()
            if isinstance(part, str) and cls._secret_echo(part.encode()):
                return True
            if isinstance(part, dict):
                pending.extend(part.keys())
                pending.extend(part.values())
            elif isinstance(part, list):
                pending.extend(part)
        return False

    def __call__(self, tool, arguments):
        require(isinstance(tool, str) and tool in TOOLS and isinstance(arguments, dict), 'BLOCKED_RPC_TOOL_ALLOWLIST')
        if tool == 't3_thread_send':
            require(callable(self.send_authorizer) and
                    set(arguments) == {'threadId', 'message', 'clientRequestId', 'mode'} and
                    arguments['mode'] == 'auto', 'BLOCKED_RPC_TOOL_ALLOWLIST')
            self.send_authorizer(arguments)
        elif tool == 't3_thread_update':
            require(set(arguments) == {'action', 'threadId', 'title', 'clientRequestId'} and
                    arguments['action'] == 'rename', 'BLOCKED_RPC_TOOL_ALLOWLIST')
        elif tool == 't3_thread_organize':
            require(set(arguments) == {'action', 'threadId'} and arguments['action'] == 'archive',
                    'BLOCKED_RPC_TOOL_ALLOWLIST')
        else:
            require(set(arguments) <= {'threadId', 'itemId', 'textOffset', 'view', 'afterPosition',
                                        'limit', 'runLimit', 'maxCharsPerItem'} and
                    nonempty(arguments.get('threadId')), 'BLOCKED_RPC_TOOL_ALLOWLIST')
        require(self.available(), 'BLOCKED_RPC_SESSION_UNAVAILABLE')
        try:
            encoded = canonical(arguments)
            require(len(encoded.encode()) <= self.max_output, 'BLOCKED_RPC_INPUT_LIMIT')
            require(not self._secret_echo(encoded.encode()), 'BLOCKED_RPC_SECRET_ARGUMENT')
            # No env copy, shell, token argv or persisted credential object.
            process = subprocess.Popen([self.executable, 'acp-mcp-call', tool, encoded],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
        except (OSError, ValueError, UnicodeError):
            raise Blocked('BLOCKED_RPC_EXECUTABLE') from None
        output, total = bytearray(), 0
        completed = False
        deadline = time.monotonic() + self.timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, 'BLOCKED_RPC_TIMEOUT')
                    for key, _ in selector.select(min(remaining, 0.1)):
                        raw = os.read(key.fd, 8192)
                        if not raw:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(raw)
                        require(total <= self.max_output, 'BLOCKED_RPC_OUTPUT_LIMIT')
                        if key.data == 'stdout':
                            output.extend(raw)
                        # stderr is never retained or returned, even on success.
                try:
                    code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise Blocked('BLOCKED_RPC_TIMEOUT') from None
                require(code == 0, 'BLOCKED_RPC_TOOL_ERROR')
            require(not self._secret_echo(output), 'BLOCKED_RPC_SECRET_ECHO')
            result = strict_json(bytes(output))
            require(isinstance(result, dict), 'BLOCKED_RPC_JSON')
            require(not self._json_secret_echo(result), 'BLOCKED_RPC_SECRET_ECHO')
            t3_payload(result)  # reject actual MCP error envelopes
            completed = True
            return result
        except OSError:
            raise Blocked('BLOCKED_RPC_IO') from None
        finally:
            if not completed or process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            process.stdout.close()
            process.stderr.close()


class HostBridge(Protocol):
    """Versioned service boundary. None of these are claimed existing T3 tools."""
    def resolve_session(self) -> dict: ...
    def verify_session(self, proof: dict) -> dict: ...
    def authorization(self, binding: dict, authorization_ref: dict, proof: dict) -> dict: ...
    def inventory(self, source_thread_id: str, proof: dict) -> dict: ...
    def verify_route_event(self, binding: dict, event: dict, proof: dict) -> dict: ...
    def verify_item_actor(self, item: dict, proof: dict) -> dict: ...
    def route_runtime(self, binding: dict, proof: dict): ...
    def pump_once(self, binding: dict, runtime, proof: dict): ...


def validate_session(proof, now):
    require(isinstance(proof, dict) and set(proof) == SESSION_KEYS and
            proof['schema'] == SESSION_SCHEMA, 'BLOCKED_HOST_SESSION_PROOF')
    require(all(nonempty(proof[key]) for key in IDENTITY_KEYS if key != 'generation') and
            type(proof['generation']) is int and proof['generation'] >= 1 and
            proof['scope'] in ('engineering', 'paused', 'read_only'), 'BLOCKED_HOST_SESSION_PROOF')
    require(all(type(proof[key]) in (int, float) and math.isfinite(proof[key])
                for key in ('issued_at', 'expires_at')) and
            proof['issued_at'] <= now < proof['expires_at'], 'BLOCKED_HOST_SESSION_EXPIRED')
    provenance = proof['provenance']
    require(isinstance(provenance, dict) and set(provenance) == {'issuer', 'proof_id', 'mapping_source'} and
            all(nonempty(v) for v in provenance.values()), 'BLOCKED_HOST_SESSION_PROOF')


def identity(proof):
    return {key: proof[key] for key in IDENTITY_KEYS}


def session_digest(proof):
    return sha256(identity(proof))


def read_file(path, limit=1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= limit,
                'BLOCKED_HOST_FILE')
        return stream.read(limit + 1)


class HostJournal:
    def __init__(self, root):
        self.files = HandoffStore(root)

    def load(self, mid):
        try:
            value = strict_json(read_file(self.files.path(mid)))
        except FileNotFoundError:
            return {'schema': 'portable.host-journal/v1', 'phase': 'STOPPED', 'session': None,
                    'retired': [], 'events': {}, 'stop_requested': False}
        require(isinstance(value, dict) and value.get('schema') == 'portable.host-journal/v1' and
                isinstance(value.get('events'), dict) and isinstance(value.get('retired'), list),
                'BLOCKED_HOST_JOURNAL')
        return value

    def save(self, mid, value):
        path = self.files.path(mid)
        require(not path.is_symlink(), 'BLOCKED_HOST_FILE')
        raw = canonical(value)
        require(len(raw.encode()) <= 1024 * 1024, 'BLOCKED_HOST_JOURNAL_LIMIT')
        atomic(path, raw)

    @contextmanager
    def lease(self, mid):
        import fcntl
        path = self.files.root / (self.files.path(mid).stem + '.loop-lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_nlink == 1,
                    'BLOCKED_HOST_FILE')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Blocked('BLOCKED_HOST_ALREADY_RUNNING') from None
            yield
        finally:
            os.close(fd)


class TasklineHost:
    def __init__(self, state_root, *, transport=None, bridge=None, clock=time.time):
        self.store = HandoffStore(state_root)
        self.journal = HostJournal(self.store.root / 'host')
        self.transport = transport if transport is not None else ACPCLITransport()
        self.bridge, self.clock = bridge, clock

    def doctor(self):
        blockers = []
        if hasattr(self.transport, 'available') and not self.transport.available():
            blockers.append('BLOCKED_RPC_SESSION_UNAVAILABLE')
        if self.bridge is None:
            blockers.append('BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE')
        blockers.append('BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE')
        return {'status': 'BLOCKED',
                'proof_source': 'MISSING' if self.bridge is None else 'BRIDGE_INJECTED_REQUIRES_VALIDATION',
                'blockers': blockers, 'schema': SESSION_SCHEMA,
                'atomic_conditional_archive': False, 'strict_archive_blocker': 'BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE',
                'observed_current_env_is_verified_scope': False}

    def observe_current_env(self):
        # Non-authoritative observation, never read by _session or inventory.
        app, native = os.environ.get('T3CODE_THREAD_ID'), os.environ.get('CODEX_THREAD_ID')
        observation = {'trust': 'observed_current_env', 'verified_scope': False,
                       'app_thread_observed': app, 'native_thread_observed': native,
                       'caller_observed': ('codex:' + native) if nonempty(native) else None,
                       'native_caller_mapping_verified': False, 'history': 'unknown',
                       'source': 'current-process:T3CODE_THREAD_ID+CODEX_THREAD_ID',
                       'policy_source_sha256': 'a5f8fd71d55a8741e7cbda3b668e01c6ffacfea8bb6bc55b8591ec9355fd5ee5',
                       'installed_runtime_match_rechecked': False, 'observed_at': self.clock()}
        require(not ACPCLITransport._secret_echo(canonical(observation).encode()),
                'BLOCKED_HOST_PROOF_CREDENTIAL_ECHO')
        with self.journal.files.locked():
            path = self.journal.files.root / '.observed-current-env.json'
            try:
                records = strict_json(read_file(path))
                require(records.get('schema') == 'portable.host-observations/v1', 'BLOCKED_HOST_JOURNAL')
            except FileNotFoundError:
                records = {'schema': 'portable.host-observations/v1', 'observations': []}
            records['observations'].append(observation)
            require(len(canonical(records).encode()) <= 1024 * 1024, 'BLOCKED_HOST_JOURNAL_LIMIT')
            atomic(path, canonical(records))
        return observation

    def _session(self, state, expected=None):
        if hasattr(self.transport, 'available'):
            require(self.transport.available(), 'BLOCKED_RPC_SESSION_UNAVAILABLE')
        require(self.bridge is not None and callable(getattr(self.bridge, 'resolve_session', None)) and
                callable(getattr(self.bridge, 'verify_session', None)),
                'BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE')
        try:
            proof = self.bridge.resolve_session()
            require(not ACPCLITransport._secret_echo(canonical(proof).encode()),
                    'BLOCKED_HOST_PROOF_CREDENTIAL_ECHO')
            validate_session(proof, self.clock())
            checked = self.bridge.verify_session(deepcopy(proof))
            require(isinstance(checked, dict) and checked == proof, 'BLOCKED_HOST_SESSION_UNVERIFIED')
            require(proof['scope'] == 'engineering' and
                    all(m['scope'] == 'engineering' for m in state['member_metadata'].values()),
                    'BLOCKED_HOST_SCOPE')
            require(proof['app_thread_id'] == state['manifest']['target_thread']['thread_id'] and
                    all(m['current_caller'] == proof['native_caller']
                        for m in state['member_metadata'].values()), 'BLOCKED_HOST_CALLER_THREAD_MISMATCH')
            if expected is not None:
                require(identity(proof) == expected, 'BLOCKED_HOST_SESSION_CHANGED')
            return proof
        except Blocked:
            raise
        except Exception:
            raise Blocked('BLOCKED_HOST_BRIDGE_ERROR') from None

    def _inventory(self, state, proof):
        require(callable(getattr(self.bridge, 'inventory', None)),
                'BLOCKED_INVENTORY_CALLER_HISTORY_UNAVAILABLE')
        try:
            result = self.bridge.inventory(state['manifest']['source_thread']['thread_id'], proof)
            require(isinstance(result, dict) and result.get('complete') is True and
                    result.get('caller_history_complete') is True and
                    result.get('environment_id') == proof['environment_id'] and
                    result.get('source_thread_id') == state['manifest']['source_thread']['thread_id'] and
                    nonempty(result.get('provenance')) and isinstance(result.get('caller_history'), list) and
                    bool(result['caller_history']), 'BLOCKED_INVENTORY_CALLER_HISTORY_UNAVAILABLE')
            require(type(result.get('checked_at')) in (int, float) and
                    math.isfinite(result['checked_at']) and
                    self.clock() - 5 <= result['checked_at'] <= self.clock() + 1,
                    'BLOCKED_INVENTORY_STALE')
            require(all(isinstance(row, dict) and nonempty(row.get('native_caller')) and
                        nonempty(row.get('provider_session_id')) and type(row.get('generation')) is int
                        for row in result['caller_history']), 'BLOCKED_INVENTORY_CALLER_HISTORY_UNAVAILABLE')
            pairs = result.get('task_runs')
            require(isinstance(pairs, list) and all(isinstance(p, dict) and
                    set(p) == {'task_id', 'run_id'} for p in pairs), 'BLOCKED_INVENTORY_UNKNOWN')
            expected = [{'task_id': b['task_id'], 'run_id': b['run_id']}
                        for b in state['manifest']['bindings']]
            require(sorted(map(canonical, pairs)) == sorted(map(canonical, expected)),
                    'BLOCKED_INVENTORY_MISMATCH')
            return {'complete': True, **manifest_scope(state['manifest']),
                    'source_thread_id': result['source_thread_id'], 'task_runs': pairs,
                    'provenance': result['provenance'],
                    'observed_source_task_runs': result.get('observed_source_task_runs', pairs),
                    'successor_current_task_runs': result.get('successor_current_task_runs', []),
                    'continuations': result.get('continuations', [])}
        except Blocked:
            raise
        except Exception:
            raise Blocked('BLOCKED_HOST_BRIDGE_ERROR') from None

    def _activate(self, mid, state, proof, resume):
        with self.journal.files.locked():
            record = self.journal.load(mid)
            key = session_digest(proof)
            require(key not in record['retired'], 'BLOCKED_HOST_SESSION_RETIRED')
            previous = record['session']
            if previous and previous != identity(proof):
                require(not resume, 'BLOCKED_HOST_SESSION_CHANGED')
                require(previous['native_caller'] != proof['native_caller'] and
                        previous['provider_session_id'] != proof['provider_session_id'],
                        'BLOCKED_HOST_NEW_CALLER_REQUIRED')
                if previous['app_thread_id'] == proof['app_thread_id']:
                    require(proof['generation'] > previous['generation'], 'BLOCKED_HOST_SESSION_RETIRED')
                record['retired'].append(sha256(previous))
            require(record['phase'] != 'INVALIDATED' or previous != identity(proof),
                    'BLOCKED_HOST_SESSION_RETIRED')
            record.update(phase='RUNNING', session=identity(proof), manifest_digest=state['manifest_digest'],
                          stop_requested=False)
            self.journal.save(mid, record)

    def stop(self, mid):
        with self.journal.files.locked():
            record = self.journal.load(mid)
            record['stop_requested'] = True
            self.journal.save(mid, record)
        return {'status': 'STOP_REQUESTED', 'manifest_id': mid}

    def status(self, mid):
        with self.journal.files.locked():
            record = self.journal.load(mid)
        return {'status': record['phase'], 'stop_requested': record['stop_requested'],
                'local_events': len(record['events']), 'fresh_session_verified': False}

    def _route_evidence(self, binding, event, proof):
        require(callable(getattr(self.bridge, 'verify_route_event', None)),
                'BLOCKED_ROUTE_CONSUMPTION_QUERY_UNAVAILABLE')
        observed = self.bridge.verify_route_event(binding, event, proof)
        expected = {'binding_digest': sha256(binding), 'event_id': event['event_id'],
                    'payload_sha256': event['payload_sha256'],
                    'target_thread_id': binding['target_thread']['thread_id']}
        require(isinstance(observed, dict) and all(observed.get(k) == v for k, v in expected.items()) and
                type(observed.get('persistent_route')) is bool and nonempty(observed.get('provenance')),
                'BLOCKED_ROUTE_EVENT_UNVERIFIED')
        if event.get('kind') == 'route_consumed':
            require(event.get('persistent_route') == observed['persistent_route'],
                    'BLOCKED_ROUTE_RUNTIME_UNVERIFIED')
        return observed

    def consume(self, mid, event, context_root):
        state = self.store.status(mid)
        proof = self._session(state)
        if isinstance(event, dict) and event.get('schema') == 'dispatch.taskline.event/v1':
            require(callable(getattr(self.bridge, 'translate_event', None)), 'BLOCKED_DISPATCH_EVENT_BRIDGE_UNAVAILABLE')
            event = self.bridge.translate_event(event, proof)
        with self.journal.files.locked():
            record = self.journal.load(mid)
            require(record['phase'] == 'RUNNING' and not record['stop_requested'],
                    'BLOCKED_HOST_NOT_RUNNING')
            require(record['session'] == identity(proof), 'BLOCKED_HOST_SESSION_CHANGED')
            require(session_digest(proof) not in record['retired'], 'BLOCKED_HOST_SESSION_RETIRED')
            require(record['manifest_digest'] == state['manifest_digest'], 'BLOCKED_HOST_MANIFEST_CHANGED')
            require(isinstance(event, dict) and set(event) in (
                    {'schema', 'event_id', 'binding', 'manifest_id', 'manifest_version', 'manifest_digest',
                     'payload', 'payload_sha256'},
                    {'schema', 'event_id', 'binding', 'manifest_id', 'manifest_version', 'manifest_digest',
                     'payload', 'payload_sha256', 'context_file', 'dispatch_wire_sha256'}) and event['schema'] == EVENT_SCHEMA and
                    nonempty(event['event_id']) and isinstance(event['payload'], dict) and
                    hash_value(event['payload_sha256']) and sha256(event['payload']) == event['payload_sha256'],
                    'BLOCKED_HOST_EVENT')
            require(all(type(event.get(k)) is type(v) and event.get(k) == v
                        for k, v in manifest_scope(state['manifest']).items()) and
                    event['binding'] in state['manifest']['bindings'], 'BLOCKED_HOST_EVENT_BINDING')
            binding = event['binding']
            route = self._route_evidence(binding, event, proof)
            root = Path(context_root).absolute()
            for part in (root, *root.parents):
                require(not part.is_symlink(), 'BLOCKED_CONTEXT_PATH')
            relative = event.get('context_file', event['payload'].get('context_file'))
            require(isinstance(relative, str) and bool(relative) and not Path(relative).is_absolute() and
                    '..' not in Path(relative).parts, 'BLOCKED_CONTEXT_PATH')
            context_path = root / relative
            for part in (context_path, *context_path.parents):
                require(not part.is_symlink(), 'BLOCKED_CONTEXT_PATH')
            context_bytes = read_file(context_path)
            require(not ACPCLITransport._secret_echo(context_bytes), 'BLOCKED_CONTEXT_CREDENTIAL_ECHO')
            import hashlib
            require(hashlib.sha256(context_bytes).hexdigest() == binding['context_sha256'],
                    'BLOCKED_CONTEXT_HASH')
            context = strict_json(context_bytes)
            require(not ACPCLITransport._json_secret_echo(context), 'BLOCKED_CONTEXT_CREDENTIAL_ECHO')
            digest = sha256(event)
            common = {'binding_digest': sha256(binding), 'handoff_id': binding['handoff_id'],
                      'version': binding['version'], 'task_id': binding['task_id'], 'run_id': binding['run_id'],
                      'source_thread_id': binding['source_thread']['thread_id'],
                      'target_thread_id': binding['target_thread']['thread_id'],
                      **manifest_scope(state['manifest']), 'session_digest': session_digest(proof),
                      'native_caller': proof['native_caller'], 'generation': proof['generation'],
                      'host_session': identity(proof),
                      'provenance': 'portable.host.consumer/v1:' + proof['provenance']['proof_id']}
            previous = record['events'].get(event['event_id'])
            if previous:
                require(previous['request_digest'] == digest and previous['session_digest'] == session_digest(proof),
                        'BLOCKED_HOST_EVENT_CONFLICT')
                receipts = previous['receipts']
            else:
                receipts = []
                for kind in ('context_read', 'route_consumed'):
                    receipt = {**common, 'kind': kind,
                               'receipt_id': sha256([digest, kind, session_digest(proof)])}
                    if kind == 'context_read':
                        receipt['context_sha256'] = binding['context_sha256']
                    else:
                        receipt.update(event_id=event['event_id'], payload_sha256=event['payload_sha256'],
                                       persistent_route=route['persistent_route'])
                    receipts.append(receipt)
                record['events'][event['event_id']] = {'request_digest': digest,
                    'session_digest': session_digest(proof), 'receipts': receipts, 'publication': 'LOCAL_ONLY'}
            if not state['member_metadata'][binding['handoff_id']]['worker_active'] and not any(
                    r['kind'] == 'next_step_started' for r in receipts):
                query = getattr(self.bridge, 'next_step', None)
                if callable(query):
                    try:
                        operation = query(binding, proof)
                        require(isinstance(operation, dict) and operation.get('task_id') == binding['task_id'] and
                                operation.get('previous_run_id') == binding['run_id'] and
                                nonempty(operation.get('next_run_id')) and nonempty(operation.get('operation_id')),
                                'BLOCKED_NEXT_STEP_READBACK_UNAVAILABLE')
                        receipts.append({**common, 'kind': 'next_step_started',
                            'receipt_id': sha256([operation, common]), 'operation_id': operation['operation_id'],
                            'previous_run_id': operation['previous_run_id'], 'next_run_id': operation['next_run_id'],
                            'operation_sha256': sha256(operation)})
                    except Blocked:
                        pass  # context/route consumption continues; readiness query reports the exact gap
            self._session(state, identity(proof))
            self.journal.save(mid, record)
        # The actual successor agent must read this context and emit this JSON
        # in its own assistant message. stdout is NOT remotely published proof.
        return {'status': 'LOCAL_CANDIDATE_NOT_PUBLISHED', 'context': context,
                'agent_output': {'schema': PUBLICATION_SCHEMA, 'session': identity(proof), 'receipts': receipts},
                'agent_outputs': [({'schema': 'dispatch.taskline.consumer/v1', 'receipt': r}
                                   if r['kind'] == 'route_consumed' else
                                   {'schema': PUBLICATION_SCHEMA, 'session': identity(proof), 'receipts': [r]})
                                  for r in receipts]}

    def _remote_receipts(self, state, binding, proof):
        thread_id = state['manifest']['target_thread']['thread_id']
        receipts, position = {}, None
        for _ in range(20):
            args = {'threadId': thread_id, 'view': 'messages', 'limit': 100, 'maxCharsPerItem': 50000}
            if position is not None:
                args['afterPosition'] = position
            page = t3_payload(self.transport('t3_thread_read', args))
            require(isinstance(page.get('thread'), dict) and
                    page['thread'].get('threadId', page['thread'].get('id')) == thread_id and
                    isinstance(page.get('items'), list) and type(page.get('hasMore')) is bool,
                    'BLOCKED_RECEIPT_READBACK_UNAVAILABLE')
            for item in page['items']:
                if not (isinstance(item, dict) and item.get('type') == 'assistant_message' and
                        item.get('visibility') == 'local' and item.get('sourceThreadId') == thread_id and
                        item.get('createdBy') == 'agent' and item.get('status') == 'completed' and
                        nonempty(item.get('itemId')) and
                        item.get('textTruncated') is False and nonempty(item.get('text'))):
                    continue
                try:
                    publication = strict_json(item['text'])
                except Blocked:
                    continue
                if isinstance(publication, dict) and publication.get('schema') == 'dispatch.taskline.consumer/v1':
                    require(set(publication) == {'schema', 'receipt'} and isinstance(publication['receipt'], dict),
                            'BLOCKED_RECEIPT_READBACK_UNAVAILABLE')
                    receipt = publication['receipt']
                    publication = {'schema': PUBLICATION_SCHEMA, 'session': receipt.get('host_session'),
                                   'receipts': [receipt]}
                if not isinstance(publication, dict) or publication.get('schema') != PUBLICATION_SCHEMA:
                    continue
                if publication.get('session') != identity(proof):
                    continue  # historical/late old-session output is not evidence
                require(callable(getattr(self.bridge, 'verify_item_actor', None)),
                        'BLOCKED_RECEIPT_ACTOR_MAPPING_UNAVAILABLE')
                actor = self.bridge.verify_item_actor(item, proof)
                require(isinstance(actor, dict) and actor.get('session_digest') == session_digest(proof) and
                        actor.get('item_id') == item.get('itemId') and nonempty(actor.get('provenance')),
                        'BLOCKED_RECEIPT_ACTOR_UNVERIFIED')
                require(isinstance(publication.get('receipts'), list), 'BLOCKED_RECEIPT_READBACK_UNAVAILABLE')
                for receipt in publication['receipts']:
                    require(isinstance(receipt, dict), 'BLOCKED_RECEIPT_READBACK_UNAVAILABLE')
                    if receipt.get('binding_digest') != sha256(binding) or any(
                            receipt.get(k) != v for k, v in manifest_scope(state['manifest']).items()):
                        continue  # retained history never migrates into current readiness
                    require(receipt.get('session_digest') == session_digest(proof) and
                            receipt.get('native_caller') == proof['native_caller'] and
                            type(receipt.get('generation')) is int and receipt['generation'] == proof['generation'] and
                            all(receipt.get(k) == v for k, v in manifest_scope(state['manifest']).items()),
                            'BLOCKED_RECEIPT_WRONG_SESSION')
                    require(receipt.get('kind') in ('context_read', 'route_consumed', 'next_step_started'),
                            'BLOCKED_NEXT_STEP_QUERY_UNAVAILABLE')
                    with self.journal.files.locked():
                        local = self.journal.load(state['manifest']['manifest_id'])
                    candidates = [r for event in local['events'].values() for r in event['receipts']]
                    require(receipt in candidates, 'BLOCKED_RECEIPT_LOCAL_READ_UNVERIFIED')
                    if receipt.get('kind') == 'next_step_started':
                        query = getattr(self.bridge, 'next_step', None)
                        require(callable(query), 'BLOCKED_NEXT_STEP_QUERY_UNAVAILABLE')
                        operation = query(binding, proof)
                        require(receipt.get('operation_sha256') == sha256(operation) and
                                receipt.get('operation_id') == operation.get('operation_id') and
                                receipt.get('previous_run_id') == binding['run_id'] and
                                receipt.get('next_run_id') == operation.get('next_run_id'),
                                'BLOCKED_NEXT_STEP_READBACK_UNAVAILABLE')
                    if receipt.get('kind') == 'route_consumed':
                        # Official bridge must cross-check the original durable
                        # dispatch event AND current RouteRuntime lease.
                        self._route_evidence(binding, receipt, proof)
                    rid = receipt.get('receipt_id')
                    require(nonempty(rid) and (rid not in receipts or receipts[rid] == receipt),
                            'BLOCKED_RECEIPT_ID_CONFLICT')
                    receipts[rid] = receipt
            if not page['hasMore']:
                return list(receipts.values())
            next_position = page.get('nextPosition')
            require(type(next_position) is int and (position is None or next_position > position),
                    'BLOCKED_RECEIPT_PAGINATION')
            position = next_position
        raise Blocked('BLOCKED_RECEIPT_PAGINATION')

    def adapter(self, state, expected):
        def current():
            mid = state['manifest']['manifest_id']
            # advance already holds the orchestration lock. Atomic read
            # avoids re-entering its non-reentrant flock from an adapter.
            live = self.store.load(mid)
            require(live['manifest_digest'] == state['manifest_digest'], 'BLOCKED_HOST_MANIFEST_CHANGED')
            with self.journal.files.locked():
                record = self.journal.load(mid)
            require(record['phase'] == 'RUNNING' and record['session'] == expected and
                    not record['stop_requested'], 'BLOCKED_HOST_NOT_RUNNING')
            return self._session(live, expected)
        def authorization(binding, ref):
            require(callable(getattr(self.bridge, 'authorization', None)),
                    'BLOCKED_AUTHORIZATION_QUERY_UNAVAILABLE')
            return self.bridge.authorization(binding, ref, current())
        def inventory(manifest):
            require(manifest == state['manifest'], 'BLOCKED_HOST_MANIFEST_CHANGED')
            return self._inventory(state, current())
        def receipt_query(binding, rid):
            if not state['member_metadata'][binding['handoff_id']]['worker_active']:
                query = getattr(self.bridge, 'next_step', None)
                require(callable(query), 'BLOCKED_NEXT_STEP_QUERY_UNAVAILABLE')
                query(binding, current())
            observed = self._remote_receipts(state, binding, current())
            if rid is None:
                return observed
            return next((r for r in observed if r['receipt_id'] == rid), None)
        def transport(tool, args):
            current()  # revalidate generation immediately before every RPC
            return self.transport(tool, args)
        adapter = T3Adapter(transport, authority_query=authorization,
                            receipt_query=receipt_query, inventory_query=inventory)
        def strict_archive_guard(manifest):
            current()
            raise Blocked('BLOCKED_ATOMIC_ARCHIVE_UNAVAILABLE')
        adapter.before_archive = strict_archive_guard
        return adapter

    def loop(self, mid, *, iterations=1, interval=0, resume=False, stop_event=None):
        require((iterations is None or (type(iterations) is int and 1 <= iterations <= 10000)) and
                type(interval) in (int, float) and math.isfinite(interval) and 0 <= interval <= 5 and
                (iterations is not None or interval > 0),
                'INVALID_HOST_LOOP_LIMIT')
        state = self.store.status(mid)
        require(all(m['scope'] == 'engineering' for m in state['member_metadata'].values()), 'BLOCKED_HOST_SCOPE')
        proof = self._session(state)
        if callable(getattr(self.bridge, 'bind_manifest', None)):
            self.bridge.bind_manifest(state['manifest'], state['member_metadata'])
        expected = identity(proof)
        self._inventory(state, proof)  # prove full window before starting route pumps
        require(callable(getattr(self.bridge, 'route_runtime', None)) and
                callable(getattr(self.bridge, 'pump_once', None)), 'BLOCKED_ROUTE_RUNTIME_UNAVAILABLE')
        with self.journal.lease(mid):
            self._activate(mid, state, proof, resume)
            try:
                with ExitStack() as stack:
                    runtimes = [(b, stack.enter_context(self.bridge.route_runtime(b, proof)))
                                for b in state['manifest']['bindings']]
                    for _ in (itertools.count() if iterations is None else range(iterations)):
                        proof = self._session(self.store.status(mid), expected)
                        with self.journal.files.locked():
                            record = self.journal.load(mid)
                        if record['stop_requested'] or (stop_event and stop_event.is_set()):
                            break
                        require(state['manifest_digest'] == self.store.status(mid)['manifest_digest'],
                                'BLOCKED_HOST_MANIFEST_CHANGED')
                        self._inventory(state, proof)
                        for binding, runtime in runtimes:
                            with self.journal.files.locked():
                                if self.journal.load(mid)['stop_requested']:
                                    break
                            if stop_event and stop_event.is_set():
                                break
                            runtime.heartbeat()
                            self.bridge.pump_once(binding, runtime, proof)
                        with self.journal.files.locked():
                            stopped = self.journal.load(mid)['stop_requested']
                        if stopped or (stop_event and stop_event.is_set()):
                            break
                        result = self.store.advance(mid, self.adapter(state, expected))
                        self._session(self.store.status(mid), expected)
                        # A blocked handoff never stops the original route pump;
                        # late results keep flowing, including after ARCHIVED.
                        if interval:
                            if stop_event:
                                stop_event.wait(interval)
                            else:
                                time.sleep(interval)
                return {'status': 'STOPPED', 'routing_loop_stopped': True, 'persistent_route': False,
                        'handoff_phase': result['phase'] if 'result' in locals() else None,
                        'archive_status': ('ARCHIVED' if result['phase'] == 'ARCHIVED' else
                                           'BLOCKED' if result.get('blocker') else 'PENDING') if 'result' in locals() else 'NOT_ATTEMPTED',
                        'archive_blocker': result.get('blocker') if 'result' in locals() else None}
            except BaseException as exc:
                with self.journal.files.locked():
                    record = self.journal.load(mid)
                    invalid = isinstance(exc, Blocked) and str(exc) in {
                        'BLOCKED_HOST_SESSION_CHANGED', 'BLOCKED_HOST_SESSION_EXPIRED',
                        'BLOCKED_HOST_SESSION_UNVERIFIED', 'BLOCKED_HOST_CALLER_THREAD_MISMATCH',
                        'BLOCKED_HOST_SESSION_RETIRED', 'BLOCKED_HOST_SESSION_PROOF',
                        'BLOCKED_HOST_PROOF_CREDENTIAL_ECHO'}
                    record['phase'] = 'INVALIDATED' if invalid else 'STOPPED'
                    if invalid:
                        key = sha256(record['session'])
                        if key not in record['retired']:
                            record['retired'].append(key)
                    self.journal.save(mid, record)
                if isinstance(exc, Blocked):
                    raise
                if not isinstance(exc, Exception):
                    raise
                raise Blocked('BLOCKED_HOST_BRIDGE_ERROR') from None
            finally:
                with self.journal.files.locked():
                    record = self.journal.load(mid)
                    if record['phase'] != 'INVALIDATED':
                        record['phase'] = 'STOPPED'
                    self.journal.save(mid, record)


def register_host(actions):
    parser = actions.add_parser('host', help='官方 ACP transport 与宿主生命周期候选')
    parser.add_argument('--t3', default='t3', help='官方 t3 CLI 可执行文件')
    sub = parser.add_subparsers(dest='host_action', required=True)
    sub.add_parser('doctor')
    sub.add_parser('observe-current-env')
    for name in ('start', 'resume', 'status', 'stop'):
        action = sub.add_parser(name)
        action.add_argument('manifest_id')
        if name in ('start', 'resume'):
            action.add_argument('--iterations', type=int, default=None, help='省略时前台持续运行；测试可显式限定次数')
            action.add_argument('--interval', type=float, default=1)
    action = sub.add_parser('consume')
    action.add_argument('manifest_id')
    action.add_argument('event')
    action.add_argument('--context-root', required=True)


def run_host(args, host=None):
    host = host or TasklineHost(args.state_root, transport=ACPCLITransport(args.t3))
    if args.host_action == 'doctor':
        return host.doctor()
    if args.host_action == 'observe-current-env':
        return host.observe_current_env()
    if args.host_action in ('status', 'stop'):
        return getattr(host, args.host_action)(args.manifest_id)
    if args.host_action == 'consume':
        return host.consume(args.manifest_id, strict_json(read_file(args.event)), args.context_root)
    return host.loop(args.manifest_id, iterations=args.iterations, interval=args.interval,
                     resume=args.host_action == 'resume')


class DispatchRouteBridge:
    """Fixed product composition, not a plugin loader or another proof issuer.

    api is the trusted installed dispatch taskline module supplied by host code.
    No CLI path/env field loads Python. services must implement the formal T3 /
    dispatch identity proof bridge; absent services remain explicitly blocked.
    """
    def __init__(self, api, services, transport, *, route_root, dispatch_root, clock=time.time):
        require(all(callable(getattr(api, name, None)) for name in
                    ('RouteStore', 'T3Relay', 'RouteRuntime', 'ReadOnlyTaskInventory', 'HostSessionGate', 'pump_once')),
                'BLOCKED_DISPATCH_PRODUCT_API_UNAVAILABLE')
        self.api, self.services, self.transport = api, services, transport
        self.route_root, self.dispatch_root, self.clock = Path(route_root).absolute(), str(Path(dispatch_root).absolute()), clock
        self.manifest, self.metadata, self.routes, self.relay = None, None, None, None
        self.next_step_reader = ReadOnlyNextStep(self.dispatch_root)
        if isinstance(transport, ACPCLITransport):
            transport.send_authorizer = self._authorize_send

    def resolve_session(self):
        require(callable(getattr(self.services, 'resolve_session', None)),
                'BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE')
        return self.services.resolve_session()

    def verify_session(self, proof):
        require(callable(getattr(self.services, 'verify_session', None)),
                'BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE')
        return self.services.verify_session(proof)

    def _current(self):
        proof = self.resolve_session()
        validate_session(proof, self.clock())
        require(self.verify_session(deepcopy(proof)) == proof, 'BLOCKED_HOST_SESSION_UNVERIFIED')
        return proof

    def caller_thread_history(self, proof):
        require(callable(getattr(self.services, 'caller_thread_history', None)),
                'BLOCKED_INVENTORY_CALLER_HISTORY_UNAVAILABLE')
        require(identity(self._current()) == identity(proof), 'BLOCKED_HOST_SESSION_CHANGED')
        return self.services.caller_thread_history(proof)

    def authorization(self, binding, authorization_ref, proof):
        require(identity(self._current()) == identity(proof), 'BLOCKED_HOST_SESSION_CHANGED')
        return self.services.authorization(binding, authorization_ref, proof)

    def verify_item_actor(self, item, proof):
        require(callable(getattr(self.services, 'verify_item_actor', None)),
                'BLOCKED_RECEIPT_ACTOR_MAPPING_UNAVAILABLE')
        return self.services.verify_item_actor(item, proof)

    def bind_manifest(self, manifest, metadata):
        from .taskline_handoff import validate_manifest
        validate_manifest(manifest, metadata)
        self.manifest, self.metadata = deepcopy(manifest), deepcopy(metadata)
        self.relay = self.api.T3Relay(self._tool, authority_query=lambda b, ref:
                                     self.authorization(b, ref, self._current()), host_bridge=self)
        self.relay.session_gate = self.api.HostSessionGate(self, clock=self.clock)
        # Keep previous journals. New manifest/session probes cannot reuse a
        # prior version's consumption or overwrite an old pending result.
        self.routes = self.api.RouteStore(str(self.route_root / sha256(manifest)), self.relay)
        for binding in manifest['bindings']:
            self.routes.prepare(binding, metadata[binding['handoff_id']])

    def inventory(self, source_thread_id, proof):
        gate = self.api.HostSessionGate(self, clock=self.clock)
        scanner = self.api.ReadOnlyTaskInventory(self.dispatch_root, session_gate=gate)
        observed = scanner.scan(source_thread_id)
        require(observed.get('complete') is True, 'BLOCKED_DISPATCH_INVENTORY_INCOMPLETE')
        history = self.caller_thread_history(proof)
        require(history.get('caller_history_complete') is True, 'BLOCKED_INVENTORY_CALLER_HISTORY_UNAVAILABLE')
        callers = [{'native_caller': row['native_caller'], 'provider_session_id': row['provider_session_id'],
                    'generation': row['generation']} for row in history['caller_history']
                   if row['app_thread_id'] == source_thread_id]
        covered, continuations = deepcopy(observed['task_runs']), []
        successor = None
        if self.manifest is not None:
            for binding in self.manifest['bindings']:
                pair = {'task_id': binding['task_id'], 'run_id': binding['run_id']}
                if pair in covered or self.metadata[binding['handoff_id']]['worker_active']:
                    continue
                operation = self.next_step(binding, proof)
                require(any(row['native_caller'] == operation['source_run_owner'] and
                            row['app_thread_id'] == source_thread_id for row in history['caller_history']),
                        'BLOCKED_NEXT_STEP_SOURCE_MAPPING')
                if successor is None:
                    successor = scanner.scan(proof['app_thread_id'])
                require(successor.get('complete') is True and
                        {'task_id': binding['task_id'], 'run_id': operation['next_run_id']} in successor['task_runs'],
                        'BLOCKED_NEXT_STEP_INVENTORY')
                covered.append(pair)  # original reference, explicitly accompanied by actual next run
                continuations.append(operation)
        return {'complete': True, 'caller_history_complete': True, 'caller_history': callers,
                'environment_id': proof['environment_id'], 'source_thread_id': source_thread_id,
                'task_runs': covered, 'observed_source_task_runs': observed['task_runs'],
                'successor_current_task_runs': successor['task_runs'] if successor else [],
                'continuations': continuations, 'checked_at': self.clock(),
                'provenance': observed['provenance'] + ':' + observed['file_set_digest']}

    def _binding(self, binding):
        require(self.manifest is not None and binding in self.manifest['bindings'], 'BLOCKED_HOST_EVENT_BINDING')
        proof = self._current()
        metadata = self.metadata[binding['handoff_id']]
        require(proof['app_thread_id'] == binding['target_thread']['thread_id'] and
                proof['native_caller'] == metadata['current_caller'] and proof['scope'] == 'engineering',
                'BLOCKED_HOST_CALLER_THREAD_MISMATCH')
        return proof

    def _authorize_send(self, args):
        require(set(args) == {'threadId', 'message', 'clientRequestId', 'mode'} and args['mode'] == 'auto',
                'BLOCKED_RPC_TOOL_ALLOWLIST')
        packet = strict_json(args['message'])
        require(isinstance(packet, dict) and packet.get('schema') == 'dispatch.taskline.event/v1',
                'BLOCKED_HOST_EVENT')
        binding = packet.get('binding')
        proof = self._binding(binding)
        require(args['threadId'] == binding['target_thread']['thread_id'] and
                packet.get('binding_digest') == sha256(binding) and
                packet.get('payload_sha256') == sha256(packet.get('payload')), 'BLOCKED_HOST_EVENT_BINDING')
        expected_key = 'dispatch-taskline-' + sha256([sha256(binding), packet['event_id'], packet['payload_sha256']])
        require(args['clientRequestId'] == expected_key, 'BLOCKED_RPC_SEND_IDEMPOTENCY')
        require(any(row['event_id'] == packet['event_id'] and row['payload_sha256'] == packet['payload_sha256']
                    for row in self.routes.pending(binding['handoff_id'])), 'BLOCKED_ROUTE_EVENT_UNVERIFIED')
        authority = self.authorization(binding, self.metadata[binding['handoff_id']]['authorization_ref'], proof)
        require(isinstance(authority, dict) and authority.get('authorized') is True and
                authority.get('binding_digest') == sha256(binding) and
                authority.get('current_caller') == proof['native_caller'], 'BLOCKED_AUTHORITY')

    def _tool(self, tool, args):
        self._current()
        if tool == 't3_thread_send':
            self._authorize_send(args)
        else:
            require(tool == 't3_thread_read', 'BLOCKED_RPC_TOOL_ALLOWLIST')
        return self.transport(tool, args)

    def translate_event(self, packet, proof):
        require(set(packet) == {'schema', 'binding', 'binding_digest', 'event_id', 'payload_sha256', 'payload'} and
                packet['schema'] == 'dispatch.taskline.event/v1', 'BLOCKED_HOST_EVENT')
        binding = packet['binding']
        require(identity(self._binding(binding)) == identity(proof) and packet['binding_digest'] == sha256(binding)
                and packet['payload_sha256'] == sha256(packet['payload']), 'BLOCKED_HOST_EVENT_BINDING')
        self.verify_route_event(binding, packet, proof)
        return {'schema': EVENT_SCHEMA, 'binding': deepcopy(binding), **manifest_scope(self.manifest),
                'event_id': packet['event_id'], 'payload': deepcopy(packet['payload']),
                'payload_sha256': packet['payload_sha256'], 'dispatch_wire_sha256': sha256(packet),
                'context_file': binding['handoff_id'] + '.json'}

    def verify_route_event(self, binding, event, proof):
        require(identity(self._binding(binding)) == identity(proof), 'BLOCKED_HOST_SESSION_CHANGED')
        records = self.routes.pending(binding['handoff_id'])
        matched = any(row['event_id'] == event['event_id'] and row['payload_sha256'] == event['payload_sha256']
                      for row in records)
        if not matched:
            # status exposes acknowledged durable receipt identity; never read
            # dispatch's private database or task ownership files directly.
            matched = any(row['event_id'] == event['event_id'] and row['payload_sha256'] == event['payload_sha256']
                          for row in self.routes.status(binding['handoff_id'])['receipts'])
        require(matched, 'BLOCKED_ROUTE_EVENT_UNVERIFIED')
        return {'binding_digest': sha256(binding), 'event_id': event['event_id'],
                'payload_sha256': event['payload_sha256'], 'target_thread_id': proof['app_thread_id'],
                'persistent_route': self.api.RouteRuntime.query(self.routes, binding) is not None,
                'provenance': 'DispatchRouteBridge:RouteStore+RouteRuntime'}

    def next_step(self, binding, proof):
        require(identity(self._binding(binding)) == identity(proof), 'BLOCKED_HOST_SESSION_CHANGED')
        local = self.next_step_reader.query(binding, proof)
        query_type = getattr(self.api, 'ReadOnlyNextStepQuery', None)
        require(callable(query_type), 'BLOCKED_DISPATCH_NEXT_STEP_QUERY_UNAVAILABLE')
        query = query_type(self.dispatch_root, host_bridge=self,
                           binding_query=lambda hid: self.routes.record(hid)['binding'])
        query.inventory.gate = self.api.HostSessionGate(self, clock=self.clock)
        result = query.query(binding, local['next_run_id'])
        require(isinstance(result, dict) and result.get('complete') is True and
                isinstance(result.get('receipt'), dict), 'BLOCKED_DISPATCH_NEXT_STEP_READBACK')
        receipt = query.verify(binding, result['receipt'])
        require(receipt['task_id'] == binding['task_id'] and receipt['run_id'] == binding['run_id'] and
                receipt['next_run_id'] == local['next_run_id'] and
                receipt['native_caller'] == proof['native_caller'] and
                receipt['session_digest'] == session_digest(proof), 'BLOCKED_NEXT_STEP_RUN_BINDING')
        return {**local, 'operation_id': receipt['operation_id'], 'dispatch_receipt': receipt,
                'dispatch_file_set_digest': result['file_set_digest']}

    def route_runtime(self, binding, proof):
        self._binding(binding)
        return self.api.RouteRuntime(self.routes, binding['handoff_id'])

    def pump_once(self, binding, runtime, proof):
        self._binding(binding)
        return self.api.pump_once(self.routes, binding['handoff_id'], dispatch_root=self.dispatch_root)


class ReadOnlyNextStep:
    """Read back an already-admitted original-task continue; never executes it.

    Reads exact task/old meta/new meta/claim, requires formal host identity,
    official manual_continuation admission and actual runner PID/start/argv.
    Missing entry/lineage/process evidence is an explicit blocked branch.
    """
    def __init__(self, root, *, process_reader=None):
        self.root = Path(root).absolute()
        require('..' not in self.root.parts, 'BLOCKED_OPERATION_PATH')
        for part in (self.root, *self.root.parents):
            require(not part.is_symlink(), 'BLOCKED_OPERATION_PATH')
        self.process_reader = process_reader or self._process

    @staticmethod
    def _process(pid):
        require(type(pid) is int and pid > 1, 'BLOCKED_NEXT_STEP_PROCESS_UNAVAILABLE')
        try:
            result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'pid=,lstart=,command='],
                                    capture_output=True, text=True, timeout=2)
            fields = result.stdout.strip().split(None, 6)
            require(result.returncode == 0 and len(fields) == 7 and fields[0] == str(pid),
                    'BLOCKED_NEXT_STEP_PROCESS_UNAVAILABLE')
            return {'pid': pid, 'pid_started': ' '.join(fields[1:6]), 'command': fields[6]}
        except (OSError, subprocess.SubprocessError):
            raise Blocked('BLOCKED_NEXT_STEP_PROCESS_UNAVAILABLE') from None

    def query(self, binding, proof):
        import re
        require(all(isinstance(value, str) and re.fullmatch('[A-Za-z0-9_-]{1,120}', value)
                    for value in (binding['task_id'], binding['run_id'])), 'BLOCKED_OPERATION_PATH')
        paths = []
        def load(relative):
            path = self.root / relative
            for part in (path, *path.parents):
                require(not part.is_symlink(), 'BLOCKED_OPERATION_PATH')
            try:
                raw = read_file(path)
            except OSError:
                raise Blocked('BLOCKED_NEXT_STEP_READBACK_UNAVAILABLE') from None
            paths.append((path, raw))
            return strict_json(raw)
        task = load('tasks/' + binding['task_id'] + '.json')
        require(task.get('task_id') == binding['task_id'] and task.get('caller') == proof['native_caller'],
                'BLOCKED_NEXT_STEP_TASK_OWNER')
        runs = task.get('runs')
        next_run = task.get('current_run')
        require(isinstance(runs, list) and len(set(runs)) == len(runs) and binding['run_id'] in runs and
                isinstance(next_run, str) and re.fullmatch('[A-Za-z0-9_-]{1,120}', next_run) and
                next_run in runs and runs.index(next_run) > runs.index(binding['run_id']) and
                runs[-1] == next_run, 'BLOCKED_NEXT_STEP_LINEAGE')
        previous = load('dispatches/' + binding['run_id'] + '/meta.json')
        current = load('dispatches/' + next_run + '/meta.json')
        claim = load('task-claims/' + binding['task_id'] + '.json')
        require(previous.get('id') == binding['run_id'] and current.get('id') == next_run and
                previous.get('task_id') == current.get('task_id') == binding['task_id'] and
                nonempty(previous.get('session')) and current.get('session') == proof['native_caller'] and
                nonempty(previous.get('session_id')) and previous['session_id'] == current.get('session_id'),
                'BLOCKED_NEXT_STEP_RUN_BINDING')
        audit, owner = [], proof['native_caller']
        if previous['session'] != owner:
            transfers = task.get('transfers')
            require(isinstance(transfers, list) and bool(transfers), 'BLOCKED_NEXT_STEP_TRANSFER_AUDIT')
            for row in reversed(transfers):
                require(isinstance(row, dict) and row.get('new_caller') == owner and
                        all(nonempty(row.get(k)) for k in ('old_caller', 'at', 'reason', 'duty_path')) and
                        hash_value(row.get('duty_sha256')) and isinstance(row.get('runs'), list) and
                        binding['run_id'] in row['runs'] and next_run not in row['runs'],
                        'BLOCKED_NEXT_STEP_TRANSFER_AUDIT')
                audit.append(row)
                owner = row['old_caller']
                if owner == previous['session']:
                    break
            require(owner == previous['session'], 'BLOCKED_NEXT_STEP_TRANSFER_AUDIT')
        require(task.get('status') == 'running' and current.get('status') == 'running' and
                current.get('executor_entered') is not False and current.get('reverify_only') is not True and
                claim.get('task_id') == binding['task_id'] and claim.get('run_id') == next_run and
                claim.get('caller') == proof['native_caller'] and claim.get('session_id') == current['session_id'] and
                claim.get('phase') == 'running' and claim.get('manual_continuation') is True and
                nonempty(current.get('started')), 'BLOCKED_NEXT_STEP_NOT_STARTED')
        require(type(claim.get('child_pid')) is int and claim['child_pid'] > 1,
                'BLOCKED_NEXT_STEP_PROCESS_UNAVAILABLE')
        proc = self.process_reader(claim['child_pid'])
        try:
            argv = shlex.split(proc.get('command', '')) if isinstance(proc, dict) else []
        except ValueError:
            argv = []
        wrapper = argv
        if argv and Path(argv[0]).name in ('bash', 'zsh', 'sh'):
            wrapper = argv[1:]
        require(isinstance(proc, dict) and proc.get('pid') == claim.get('child_pid') and
                nonempty(claim.get('child_pid_started')) and proc.get('pid_started') == claim['child_pid_started'] and
                len(wrapper) >= 3 and Path(wrapper[0]).name == 'codex-task' and
                wrapper[1:3] == ['--continue', next_run],
                'BLOCKED_NEXT_STEP_PROCESS_UNAVAILABLE')
        require(all(read_file(path) == raw for path, raw in paths), 'BLOCKED_NEXT_STEP_SNAPSHOT_CHANGED')
        summary = {'task_id': binding['task_id'], 'previous_run_id': binding['run_id'],
                   'next_run_id': next_run, 'native_caller': proof['native_caller'],
                   'session_digest': session_digest(proof), 'generation': proof['generation'],
                   'branch': 'transfer_then_continue' if audit else 'same_owner_continue',
                   'source_run_owner': previous['session'],
                   'audit_sha256': sha256(audit), 'process_identity': {
                       'pid': proc['pid'], 'pid_started': proc['pid_started'],
                       'command_sha256': __import__('hashlib').sha256(proc['command'].encode()).hexdigest(),
                       'launch_verified': True},
                   'worker_session_id': current['session_id']}
        return {**summary, 'operation_id': 'continue-' + sha256(summary),
                'provenance': 'ReadOnlyNextStep:task+run-lineage+transfer+claim+kernel-identity'}
