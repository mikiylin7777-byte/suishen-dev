"""Read-only dispatch inventory and versioned trusted-host session checks.

No dispatch entrypoint imports, state initialization, locks or task writes.
Public T3 currently provides no caller/thread proof bridge: absent -> blocked.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import sys


class SessionBlocked(ValueError):
    pass


def check(ok, code):
    if not ok:
        raise SessionBlocked(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sha256(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 4096 and not any(ord(c) < 32 for c in value)


def ident(value):
    return isinstance(value, str) and re.fullmatch('[A-Za-z0-9_-]{1,120}', value) is not None


def safe_path(value):
    path = Path(value)
    check(path.is_absolute() and '..' not in path.parts and str(path) == str(value), 'INVENTORY_UNSAFE_PATH')
    for part in (*reversed(path.parents), path):
        try:
            check(not stat.S_ISLNK(part.lstat().st_mode), 'INVENTORY_UNSAFE_PATH')
        except FileNotFoundError:
            continue
    return path


SESSION_KEYS = {'schema', 'environment_id', 'app_thread_id', 'native_caller',
                'provider_session_id', 'provider_instance_id', 'generation', 'scope',
                'issued_at', 'expires_at', 'provenance'}
PIN_KEYS = ('environment_id', 'app_thread_id', 'native_caller', 'provider_session_id',
            'provider_instance_id', 'generation', 'scope')
PROVENANCE_KEYS = {'issuer', 'proof_id', 'mapping_source'}
MAP_KEYS = {'schema', 'environment_id', 'provider_session_id', 'provider_instance_id',
            'generation', 'caller_history_complete', 'caller_history', 'provenance'}


def valid_provenance(value):
    return isinstance(value, dict) and set(value) == PROVENANCE_KEYS and all(text(v) for v in value.values())


class HostSessionGate:
    """Uses portable.host-session/v1 without translating or self-signing proof."""
    def __init__(self, host_bridge=None, *, clock=time.time):
        self.bridge, self.clock, self.pin = host_bridge, clock, None

    def verify(self, binding=None, metadata=None, *, engineering=False):
        resolve = getattr(self.bridge, 'resolve_session', None)
        verify = getattr(self.bridge, 'verify_session', None)
        history_query = getattr(self.bridge, 'caller_thread_history', None)
        check(callable(resolve) and callable(verify) and callable(history_query),
              'BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE')
        try:
            proof = deepcopy(resolve())
        except SessionBlocked:
            raise
        except Exception:
            raise SessionBlocked('BLOCKED_RPC_SESSION_UNAVAILABLE') from None
        check(isinstance(proof, dict) and set(proof) == SESSION_KEYS and
              proof['schema'] == 'portable.host-session/v1', 'BLOCKED_SESSION_PROOF_INVALID')
        check(all(text(proof[k]) for k in PIN_KEYS if k != 'generation'), 'BLOCKED_SESSION_PROOF_INVALID')
        check(type(proof['generation']) is int and proof['generation'] >= 1 and
              proof['scope'] in ('engineering', 'paused', 'read_only') and valid_provenance(proof['provenance']),
              'BLOCKED_SESSION_PROOF_INVALID')
        now = self.clock()
        check(all(type(proof[k]) in (int,float) and math.isfinite(proof[k]) for k in ('issued_at','expires_at')),
              'BLOCKED_SESSION_PROOF_INVALID')
        check(proof['issued_at'] <= now < proof['expires_at'], 'BLOCKED_SESSION_EXPIRED')
        try:
            checked = verify(deepcopy(proof))
        except SessionBlocked:
            raise
        except Exception:
            raise SessionBlocked('BLOCKED_HOST_SESSION_UNVERIFIED') from None
        check(canonical(checked) == canonical(proof), 'BLOCKED_HOST_SESSION_UNVERIFIED')
        pin = {k: proof[k] for k in PIN_KEYS}
        if self.pin is not None:
            check(canonical(self.pin) == canonical(pin), 'BLOCKED_SESSION_GENERATION_STALE')
        if binding is not None:
            target = binding['target_thread']
            match = re.fullmatch(r'<t3-thread://v2/([^/<>\s]+)/([^<>\s]+)>', target['link'])
            check(match is not None and proof['app_thread_id'] == target['thread_id'] == match[2]
                  and proof['environment_id'] == match[1] and metadata is not None
                  and proof['native_caller'] == metadata['current_caller'] and proof['scope'] == metadata['scope'],
                  'BLOCKED_SESSION_BINDING')
        if engineering:
            check(proof['scope'] == 'engineering', 'BLOCKED_SCOPE_' + proof['scope'].upper())
        try:
            mapping = deepcopy(history_query(deepcopy(proof)))
        except SessionBlocked:
            raise
        except Exception:
            raise SessionBlocked('BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE') from None
        check(isinstance(mapping,dict) and set(mapping) == MAP_KEYS and
              mapping['schema'] == 'portable.caller-thread-history/v1', 'BLOCKED_MAPPING_INVALID')
        check(all(type(mapping[k]) is type(proof[k]) and mapping[k] == proof[k] for k in
                  ('environment_id','provider_session_id','provider_instance_id','generation')),
              'BLOCKED_SESSION_GENERATION_STALE')
        check(mapping['caller_history_complete'] is True and isinstance(mapping['caller_history'],list),
              'BLOCKED_MAPPING_INCOMPLETE')
        check(valid_provenance(mapping['provenance']), 'BLOCKED_MAPPING_INVALID')
        callers = {}
        for row in mapping['caller_history']:
            check(isinstance(row,dict) and set(row) ==
                  {'native_caller','app_thread_id','provider_session_id','provider_instance_id','generation','provenance'}
                  and all(text(row[k]) for k in ('native_caller','app_thread_id','provider_session_id','provider_instance_id'))
                  and type(row['generation']) is int and row['generation'] >= 1 and valid_provenance(row['provenance']),
                  'BLOCKED_MAPPING_INVALID')
            previous = callers.get(row['native_caller'])
            check(previous is None or previous['app_thread_id'] == row['app_thread_id'], 'BLOCKED_MAPPING_AMBIGUOUS')
            if previous is None or row['generation'] >= previous['generation']:
                callers[row['native_caller']] = row
        current = callers.get(proof['native_caller'])
        check(current is not None and all(current[k] == proof[k] for k in
              ('app_thread_id','provider_session_id','provider_instance_id','generation')),
              'BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE')
        self.pin = deepcopy(pin)
        return dict(proof=proof,mapping=mapping,mapping_digest=sha256(mapping),callers=callers)


TASK_STATES = {'open', 'running', 'queued', 'pending', 'interrupted', 'awaiting_acceptance',
               'cleanup_unknown', 'completed', 'cancelled'}
RUN_ACTIVE = {'running', 'queued', 'pending', 'cleanup_unknown'}
RUN_STATES = RUN_ACTIVE | {'done', 'failed', 'interrupted', 'cancelled', 'upstream_unentered'}


def owner_matches(task, run_id, owner):
    if not text(owner) or not text(task.get('caller')):
        return False
    expected = task['caller']
    if owner == expected:
        return True
    transfers = task.get('transfers', [])
    if not isinstance(transfers, list):
        return False
    for row in reversed(transfers):
        if (not isinstance(row, dict) or row.get('new_caller') != expected
                or not isinstance(row.get('runs'), list)
                or not all(text(row.get(k)) for k in ('old_caller', 'at', 'reason', 'duty_path'))
                or not isinstance(row.get('duty_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', row['duty_sha256'])):
            return False
        if owner == row['old_caller'] and run_id in row['runs']:
            return True
        expected = row['old_caller']
    return False


def process_identity(pid):
    """Kernel start identity only; no task modules, shell or process mutation."""
    check(type(pid) is int and pid > 1, 'BLOCKED_PROCESS_IDENTITY')
    if sys.platform == 'darwin':
        # SDK sys/proc_info.h: proc_bsdinfo, PROC_PIDTBSDINFO=3 (136 bytes).
        import ctypes
        class BsdInfo(ctypes.Structure):
            _fields_ = [('header', ctypes.c_uint32 * 12), ('comm', ctypes.c_char * 16),
                        ('name', ctypes.c_char * 32), ('tail', ctypes.c_uint32 * 6),
                        ('sec', ctypes.c_uint64), ('usec', ctypes.c_uint64)]
        lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                    ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidinfo.restype = ctypes.c_int
        data = BsdInfo()
        size = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(data), ctypes.sizeof(data))
        check(size == ctypes.sizeof(data) and data.header[3] == pid and
                data.header[1] != 5 and data.sec > 0, 'BLOCKED_PROCESS_IDENTITY')
        return dict(pid=pid, process_start=f'darwin:{data.sec}:{data.usec}')
    if sys.platform.startswith('linux'):
        text = Path(f'/proc/{pid}/stat').read_text()
        parts = text[text.rfind(')') + 2:].split()
        check(len(parts) > 19 and parts[0] not in ('Z', 'X'), 'BLOCKED_PROCESS_IDENTITY')
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return dict(pid=pid, process_start=f'linux:{boot}:{parts[19]}')
    raise SessionBlocked('BLOCKED_CAPABILITY:process_identity')


def process_state(meta):
    pid = meta.get('pid')
    if pid in (None,0):
        return 'not_recorded'
    if type(pid) is not int or pid <= 1:
        return 'unknown'
    try:
        current = process_identity(pid)
    except (OSError,ValueError):
        try:
            os.kill(pid,0)
        except ProcessLookupError:
            state = 'gone'
        except OSError:
            state = 'unknown'
        else:
            state = 'unknown'
    else:
        started = current['process_start']
        if started.startswith('darwin:'):
            epoch = int(started.split(':')[1])
        else:
            boot = next((line.split()[1] for line in Path('/proc/stat').read_text().splitlines()
                         if line.startswith('btime ')), None)
            if boot is None:
                return 'unknown'
            epoch = int(boot) + int(started.rsplit(':',1)[1]) / os.sysconf('SC_CLK_TCK')
        tm = time.localtime(epoch)
        lstart = (('Mon','Tue','Wed','Thu','Fri','Sat','Sun')[tm.tm_wday] + ' ' +
                  ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')[tm.tm_mon-1] +
                  f' {tm.tm_mday} {tm.tm_hour:02}:{tm.tm_min:02}:{tm.tm_sec:02} {tm.tm_year}')
        expected = meta.get('pid_started')
        state = ('alive' if expected == lstart else 'reused') if text(expected) else 'alive_identity_unconfirmed'
    # An old group may outlive a vanished/reused leader. Never discard its unknown children.
    if meta.get('process_group') is True and state in ('gone','reused'):
        try:
            os.killpg(pid,0)
        except ProcessLookupError:
            pass
        except OSError:
            return 'group_unconfirmed'
        else:
            return 'group_unconfirmed'
    return state


class ReadOnlyTaskInventory:
    def __init__(self, dispatch_root, host_bridge=None, *, session_gate=None,
                 max_files=10000, max_bytes=64 * 1024 * 1024, process_query=process_state):
        self.root = safe_path(dispatch_root)
        self.gate = session_gate or HostSessionGate(host_bridge)
        check(type(max_files) is int and 1 <= max_files <= 100000 and
              type(max_bytes) is int and max_bytes > 0, 'INVENTORY_INVALID_LIMIT')
        self.max_files, self.max_bytes, self.process_query = max_files, max_bytes, process_query

    def _snapshot(self, block):
        manifest, data, listings = {}, {}, {}
        total = 0
        entry_count = 0
        def listing(relative):
            nonlocal entry_count
            try:
                path = safe_path(str(self.root / relative))
                check(stat.S_ISDIR(path.stat().st_mode), 'INVENTORY_NOT_DIRECTORY')
                with os.scandir(path) as entries:
                    rows = []
                    for entry in entries:
                        entry_count += 1
                        check(entry_count <= self.max_files * 4, 'INVENTORY_FILE_LIMIT')
                        check(len(rows) < self.max_files, 'INVENTORY_FILE_LIMIT')
                        mode = entry.stat(follow_symlinks=False).st_mode
                        rows.append((entry.name, mode))
                listings[relative] = sorted(rows)
                return sorted(rows)
            except (OSError, SessionBlocked) as exc:
                block(str(exc) if isinstance(exc, SessionBlocked) else 'INVENTORY_DIRECTORY_UNREADABLE', relative)
                listings[relative] = None
                return []
        def load(relative):
            nonlocal total
            try:
                check(len(manifest) < self.max_files, 'INVENTORY_FILE_LIMIT')
                path = safe_path(str(self.root / relative))
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    check(stat.S_ISREG(info.st_mode) and info.st_size <= 1024 * 1024, 'INVENTORY_UNSAFE_FILE')
                    raw = stream.read(1024 * 1024 + 1)
                total += len(raw)
                check(len(raw) <= 1024 * 1024 and total <= self.max_bytes, 'INVENTORY_BYTE_LIMIT')
                manifest[relative] = dict(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                                          inode=info.st_ino, mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)
                def pairs(values):
                    result = {}
                    for k, v in values:
                        check(k not in result, 'INVENTORY_BAD_JSON')
                        result[k] = v
                    return result
                value = json.loads(raw, object_pairs_hook=pairs)
                check(isinstance(value, dict), 'INVENTORY_BAD_JSON')
                canonical(value)
                data[relative] = value
            except SessionBlocked as exc:
                block(str(exc), relative)
                manifest.setdefault(relative, {'error': str(exc)})
            except (UnicodeError, ValueError):
                block('INVENTORY_BAD_JSON', relative)
                manifest.setdefault(relative, {'error': 'INVENTORY_BAD_JSON'})
            except OSError:
                block('INVENTORY_FILE_UNREADABLE', relative)
                manifest.setdefault(relative, {'error': 'INVENTORY_FILE_UNREADABLE'})
        for folder in ('tasks', 'task-claims'):
            for name, mode in listing(folder):
                if name.endswith('.json'):
                    load(folder + '/' + name)
                elif stat.S_ISLNK(mode) or (folder == 'task-claims' and not name.startswith('.')):
                    block('INVENTORY_UNSUPPORTED_ENTRY', folder + '/' + name)
        for name, mode in listing('dispatches'):
            if not ident(name) or not stat.S_ISDIR(mode):
                block('INVENTORY_UNSUPPORTED_ENTRY', 'dispatches/' + name)
                continue
            # Complete run directory enumeration, but never read logs/prompts/artifacts.
            listing('dispatches/' + name)
            load('dispatches/' + name + '/meta.json')
        return dict(digest=sha256([manifest, listings]), files=manifest, data=data,
                    listings=listings, bytes=total)

    def scan(self, source_thread_id, expected_task_runs=None):
        check(text(source_thread_id), 'INVENTORY_INVALID_THREAD')
        blockers = []
        def block(code, ref=None):
            value = dict(code=code)
            if ref is not None:
                value['ref'] = ref
            if value not in blockers:
                blockers.append(value)
        verified = None
        try:
            verified = self.gate.verify()
        except SessionBlocked as exc:
            block(str(exc))
        before = self._snapshot(block)
        data = before['data']
        tasks, runs, claims = {}, {}, {}
        for path, value in data.items():
            if path.startswith('tasks/'):
                task_id = Path(path).stem
                if not ident(task_id) or value.get('task_id') != task_id:
                    block('TASK_BINDING_INVALID', path)
                else:
                    tasks[task_id] = value
            elif path.startswith('task-claims/'):
                task_id = Path(path).stem
                if not ident(task_id) or value.get('task_id') != task_id:
                    block('CLAIM_BINDING_INVALID', path)
                else:
                    claims[task_id] = value
            else:
                run_id = path.split('/')[1]
                if value.get('id') != run_id:
                    block('RUN_BINDING_INVALID', path)
                else:
                    runs[run_id] = value
        candidates, activity, referenced = {}, [], set()
        for task_id, task in tasks.items():
            history = task.get('runs')
            status, current = task.get('status'), task.get('current_run')
            if not isinstance(status, str) or status not in TASK_STATES:
                block('TASK_UNKNOWN_STATE', 'tasks/' + task_id + '.json')
            if not text(task.get('caller')) or not isinstance(history, list) or not all(ident(r) for r in history) or len(set(history)) != len(history):
                block('TASK_HISTORY_INVALID', task_id)
                continue
            if current is not None and (not ident(current) or current not in history):
                block('TASK_CURRENT_RUN_INVALID', task_id)
            unfinished = status not in ('completed', 'cancelled')
            selected = {current} if ident(current) else ({history[-1]} if unfinished and history else set())
            if unfinished and not history:
                block('UNFINISHED_TASK_WITHOUT_RUN', task_id)
            for run_id in history:
                referenced.add(run_id)
                meta = runs.get(run_id)
                if meta is None:
                    block('RUN_MISSING', run_id)
                    continue
                if meta.get('task_id') != task_id or not owner_matches(task, run_id, meta.get('session')):
                    block('RUN_AUTHORITY_EVIDENCE_MISSING', run_id)
                run_status = meta.get('status')
                if not isinstance(run_status, str) or run_status not in RUN_STATES:
                    block('RUN_UNKNOWN_STATE', run_id)
                try:
                    proc = self.process_query(deepcopy(meta))
                except Exception:
                    proc = 'unknown'
                active = ((isinstance(run_status,str) and run_status in RUN_ACTIVE and proc not in ('gone','reused'))
                          or proc not in ('not_recorded','gone','reused'))
                if proc not in ('not_recorded','gone','reused','alive'):
                    block('RUN_PROCESS_IDENTITY_UNCONFIRMED', run_id)
                if active:
                    selected.add(run_id)
                    if not unfinished:
                        block('TERMINAL_TASK_ACTIVE_RUN', run_id)
                if run_id in selected:
                    candidates[(task_id, run_id)] = {task['caller']}
                    if text(meta.get('session')):
                        candidates[(task_id, run_id)].add(meta['session'])
                    activity.append(dict(task_id=task_id, run_id=run_id, task_status=status if isinstance(status, str) and status in TASK_STATES else None,
                                         run_status=run_status if isinstance(run_status, str) and run_status in RUN_STATES else None, declared_active=active,
                                         claim_present=task_id in claims))
            if unfinished and verified is not None:
                source_history = []
                for run_id in history:
                    meta = runs.get(run_id)
                    if meta is None:
                        continue
                    caller = meta.get('session')
                    row = verified['callers'].get(caller) if text(caller) else None
                    if row is None:
                        block('BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE','caller-sha256:'+sha256(caller))
                    elif row['app_thread_id'] == source_thread_id:
                        source_history.append(run_id)
                if source_history:
                    run_id = source_history[-1]
                    candidates.setdefault((task_id,run_id),set()).update((task['caller'],runs[run_id]['session']))
                    if not any(a['task_id']==task_id and a['run_id']==run_id for a in activity):
                        status_value=runs[run_id].get('status')
                        activity.append(dict(task_id=task_id,run_id=run_id,
                            task_status=status if isinstance(status,str) and status in TASK_STATES else None,
                            run_status=status_value if isinstance(status_value,str) and status_value in RUN_STATES else None,
                            declared_active=False,claim_present=task_id in claims))
        for run_id, meta in runs.items():
            run_status = meta.get('status')
            if not isinstance(run_status, str) or run_status not in RUN_STATES:
                block('RUN_UNKNOWN_STATE', run_id)
            if run_id in referenced:
                continue
            try:
                proc = self.process_query(deepcopy(meta))
            except Exception:
                proc = 'unknown'
            if ((isinstance(run_status,str) and run_status in RUN_ACTIVE and proc not in ('gone','reused'))
                    or proc not in ('not_recorded','gone','reused')):
                block('ORPHAN_ACTIVE_RUN', run_id)
        for task_id, claim in claims.items():
            run_id, task = claim.get('run_id'), tasks.get(task_id)
            if claim.get('phase') not in ('queued', 'running'):
                block('CLAIM_UNKNOWN_STATE', task_id)
            if task is None or not ident(run_id) or not isinstance(task.get('runs'), list) or run_id not in task['runs']:
                block('ORPHAN_CLAIM', task_id)
                continue
            if task.get('current_run') != run_id:
                block('CLAIM_CURRENT_RUN_MISMATCH', task_id)
            if run_id not in runs:
                block('CLAIM_RUN_MISSING', task_id)
            if not owner_matches(task, run_id, claim.get('caller')):
                block('CLAIM_AUTHORITY_EVIDENCE_MISSING', task_id)
            callers = candidates.setdefault((task_id, run_id), set())
            for caller in (task.get('caller'), claim.get('caller')):
                if text(caller):
                    callers.add(caller)
        selected_rows = []
        if verified is not None:
            mapping = verified['callers']
            if not any(row['app_thread_id'] == source_thread_id for row in mapping.values()):
                block('SOURCE_THREAD_UNMAPPED')
            for pair, callers in sorted(candidates.items()):
                threads = set()
                for caller in callers:
                    if caller not in mapping:
                        block('BLOCKED_CALLER_THREAD_MAPPING_UNAVAILABLE', 'caller-sha256:' + sha256(caller))
                    else:
                        threads.add(mapping[caller]['app_thread_id'])
                if source_thread_id in threads:
                    selected_rows.append(dict(task_id=pair[0], run_id=pair[1]))
        after = self._snapshot(block)
        changed = sorted(k for k in set(before['files']) | set(after['files'])
                         if before['files'].get(k) != after['files'].get(k))
        stable = before['digest'] == after['digest']
        if not stable:
            block('INVENTORY_SNAPSHOT_CHANGED')
        if verified is not None:
            try:
                fresh = self.gate.verify()
                if fresh['mapping_digest'] != verified['mapping_digest']:
                    block('BLOCKED_MAPPING_CHANGED')
            except SessionBlocked as exc:
                block(str(exc))
        if expected_task_runs is not None:
            valid = isinstance(expected_task_runs, list) and all(isinstance(r, dict) and
                     set(r) == {'task_id', 'run_id'} and all(ident(v) for v in r.values()) for r in expected_task_runs)
            if not valid:
                block('INVENTORY_EXPECTATION_INVALID')
            elif sorted(map(canonical, expected_task_runs)) != sorted(map(canonical, selected_rows)):
                block('INVENTORY_MEMBERSHIP_MISMATCH')
        return dict(schema='dispatch.task-inventory/v1', source_thread_id=source_thread_id,
                    complete=not blockers, blockers=blockers, task_runs=selected_rows, activity=activity, atomic=False,
                    session=({k: verified['proof'][k] for k in PIN_KEYS} if verified else None),
                    environment_id=verified['proof']['environment_id'] if verified else None,
                    checked_at=time.time(), caller_history_complete=verified is not None and not blockers,
                    caller_history=([deepcopy(row) for row in verified['mapping']['caller_history']
                                     if row['app_thread_id'] == source_thread_id] if verified else []),
                    mapping_digest=verified['mapping_digest'] if verified else None,
                    scope=dict(dispatch_root=str(self.root), directories=['tasks', 'dispatches', 'task-claims'],
                               max_files=self.max_files, max_bytes=self.max_bytes),
                    snapshot=dict(before_digest=before['digest'], after_digest=after['digest'],
                                  stable=stable, changed_paths=changed),
                    files=[dict(path=k, **v) for k, v in sorted(before['files'].items())],
                    file_set_digest=sha256(before['files']),
                    provenance='ReadOnlyTaskInventory:full-directory-scan+trusted-host-mapping')

    def for_manifest(self, manifest):
        check(isinstance(manifest, dict) and isinstance(manifest.get('bindings'), list) and
              isinstance(manifest.get('source_thread'), dict), 'INVENTORY_EXPECTATION_INVALID')
        expected = [{k: row[k] for k in ('task_id', 'run_id')} for row in manifest['bindings']]
        result = self.scan(manifest['source_thread']['thread_id'], expected)
        result.update(manifest_id=manifest['manifest_id'], manifest_version=manifest['version'],
                      manifest_digest=sha256(manifest))
        return result


class ReadOnlyNextStepQuery:
    """Read back an already-started formal same-task successor run, never start it."""
    def __init__(self, dispatch_root, host_bridge=None, *, binding_query=None):
        self.binding_query = binding_query
        self.inventory = ReadOnlyTaskInventory(dispatch_root,host_bridge=host_bridge)

    def query(self, binding, next_run_id):
        check(isinstance(binding,dict) and set(binding)==
              {'handoff_id','version','task_id','run_id','source_thread','target_thread','context_sha256'} and
              type(binding['version']) is int and binding['version']>=1,
              'NEXT_STEP_BINDING_INVALID')
        check(ident(next_run_id), 'NEXT_STEP_RUN_INVALID')
        report = self.inventory.scan(binding['source_thread']['thread_id'])
        blockers = list(report['blockers'])
        def block(code):
            if {'code':code} not in blockers:
                blockers.append({'code':code})
        receipt = None
        try:
            check(callable(self.binding_query),'BLOCKED_HANDOFF_BINDING_UNAVAILABLE')
            check(canonical(self.binding_query(binding['handoff_id']))==canonical(binding),'NEXT_STEP_BINDING_INVALID')
            verified = self.inventory.gate.verify()
            proof = verified['proof']
            check(proof['scope']=='engineering' and proof['app_thread_id']==binding['target_thread']['thread_id'],
                  'BLOCKED_SESSION_BINDING')
            task_id, old = binding['task_id'], binding['run_id']
            check(ident(task_id) and ident(old) and next_run_id!=old,'NEXT_STEP_RUN_INVALID')
            records = self.inventory._snapshot(lambda code,ref=None:block(code))
            data = records['data']
            task = data.get('tasks/'+task_id+'.json',{})
            prior = data.get('dispatches/'+old+'/meta.json',{})
            next_meta = data.get('dispatches/'+next_run_id+'/meta.json',{})
            runs = task.get('runs')
            check(task.get('task_id')==task_id and isinstance(runs,list) and old in runs and next_run_id in runs
                  and runs.index(old)<runs.index(next_run_id) and task.get('current_run')==next_run_id,
                  'NEXT_STEP_TASK_RUN_LINK_INVALID')
            check(dict(task_id=task_id,run_id=old) in report['task_runs'],'NEXT_STEP_SOURCE_LINK_UNVERIFIED')
            self.inventory.gate.verify(binding,{'current_caller':task.get('caller'),'scope':'engineering'},engineering=True)
            check(prior.get('task_id')==task_id and prior.get('id')==old and next_meta.get('task_id')==task_id
                  and next_meta.get('id')==next_run_id, 'NEXT_STEP_TASK_RUN_LINK_INVALID')
            check(task.get('caller')==proof['native_caller'] and next_meta.get('session')==proof['native_caller']
                  and owner_matches(task,old,prior.get('session')), 'NEXT_STEP_TRANSFER_AUDIT_MISSING')
            check(text(task.get('workspace')) and prior.get('work')==task['workspace']==next_meta.get('work'),
                  'NEXT_STEP_PROJECT_MISMATCH')
            check(task.get('status')=='running' and next_meta.get('status')=='running'
                  and process_state(next_meta)=='alive' and text(next_meta.get('started')),
                  'NEXT_STEP_NOT_STARTED')
            after=self.inventory._snapshot(lambda code,ref=None:block(code))
            check(records['digest']==after['digest']==report['snapshot']['after_digest'],'INVENTORY_SNAPSHOT_CHANGED')
            fresh=self.inventory.gate.verify()
            check(fresh['mapping_digest']==verified['mapping_digest'],'BLOCKED_MAPPING_CHANGED')
            common=dict(binding_digest=sha256(binding),handoff_id=binding['handoff_id'],version=binding['version'],
                task_id=task_id,run_id=old,next_run_id=next_run_id,source_thread_id=binding['source_thread']['thread_id'],
                target_thread_id=binding['target_thread']['thread_id'],context_sha256=binding['context_sha256'],
                session_digest=sha256({k:proof[k] for k in PIN_KEYS}),native_caller=proof['native_caller'],
                generation=proof['generation'],kind='next_step_started',
                operation_id='dispatch-readback:'+sha256([task,prior,next_meta]),
                provenance='ReadOnlyNextStepQuery:formal-task-run-transfer+kernel-start-readback')
            receipt=dict(common,receipt_id=sha256([common,records['digest']]))
        except SessionBlocked as exc:
            block(str(exc))
        return dict(complete=not blockers,blockers=blockers,receipt=receipt if not blockers else None,
                    next_run_id=next_run_id,file_set_digest=report['file_set_digest'])

    def verify(self,binding,receipt):
        check(isinstance(receipt,dict) and ident(receipt.get('next_run_id')),'NEXT_STEP_RUN_INVALID')
        result=self.query(binding,receipt['next_run_id'])
        check(result['complete'] and canonical(result['receipt'])==canonical(receipt),'NEXT_STEP_READBACK_MISMATCH')
        return result['receipt']
