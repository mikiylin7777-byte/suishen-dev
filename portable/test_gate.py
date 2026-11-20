"""Test execution gate (test-gate): enforces the two full-test rules in code.

1. Do not rerun the full suite from scratch on failure: rerun only failed and affected tests
   unless the fix touches core areas (billing, auth, workflow, schema, app shell) or trunk moved.
   At most 2 full runs per batch.
2. Sync trunk first and run at most one full suite on this machine at a time (process lock).
"""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from .core import Blocked, Store

STATE_PREFIX = 'test_gate:'
MAX_FULL_PER_BATCH = 2
DEFAULT_LOCK = Path(tempfile.gettempdir()) / 'suishen-dev-full-test.lock'

CORE_RULES = (
    ('billing', re.compile(r'(?:^|/|_)('
                           r'cost|bill|billing|pay|payment|price|pricing|money|finance|invoice|order_amount|wallet|ledger'
                           r')(?:$|/|_|\.)', re.I)),
    ('auth', re.compile(r'(?:^|/|_)('
                        r'auth|authn|authz|perm|permission|role|rbac|acl|token|login|session_auth|privacy|sensitive|security'
                        r')(?:$|/|_|\.)', re.I)),
    ('workflow', re.compile(r'(?:^|/|_)('
                            r'workflow|state_machine|lifecycle|status_flow|taskline|orchestrat|approval_flow|pipeline'
                            r')(?:$|/|_|\.)', re.I)),
    ('schema', re.compile(r'(?:^|/|_)(schema|migrat\w*|models|database|db_adapters|ddl)(?:$|/|_|\.)|\.sql$', re.I)),
    ('shell', re.compile(r'(?:^|/)(__main__\.py|core\.py|protocol\.json|distribution\.py|ci\.py|App\.[jt]sx?|layout\.[jt]sx?|router\.[jt]sx?|shell\.[jt]sx?)$', re.I)),
)


def _git(repo, *args, timeout=30):
    r = subprocess.run(['git', '-C', str(repo), '-c', 'core.quotepath=off', *args],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def touches_core(files):
    """Return list of '<category>:<path>' for files touching core areas."""
    hits = []
    for raw in files or ():
        path = str(raw or '').strip().lstrip('./')
        if not path:
            continue
        for category, rx in CORE_RULES:
            if rx.search(path):
                hits.append(f'{category}:{path}')
                break
    return hits


def _trunk_ref(repo, git=_git):
    from .board import REMOTE_DEV, REMOTE_MAIN
    for ref in (*REMOTE_DEV, *REMOTE_MAIN, 'refs/heads/development', 'refs/heads/main'):
        code, sha, _ = git(repo, 'rev-parse', '-q', '--verify', ref + '^{commit}')
        if code == 0 and sha:
            return ref, sha
    return None, ''


def check_trunk_synced(repo, git=_git):
    """Verify HEAD includes the latest trunk commit. Non-git or single-branch repos pass."""
    repo = Path(repo)
    if not (repo / '.git').exists():
        return True, None, ''
    ref, trunk_sha = _trunk_ref(repo, git)
    if not ref or not trunk_sha:
        return True, None, ''
    code, head_sha, _ = git(repo, 'rev-parse', '-q', '--verify', 'HEAD^{commit}')
    if code != 0 or not head_sha:
        return True, ref, trunk_sha
    if head_sha == trunk_sha:
        return True, ref, trunk_sha
    anc_code, _, _ = git(repo, 'merge-base', '--is-ancestor', trunk_sha, 'HEAD')
    return anc_code == 0, ref, trunk_sha


def _changed_since(repo, since_sha, git=_git):
    files = set()
    if since_sha:
        code, out, _ = git(repo, 'diff', '--name-only', since_sha)
        if code == 0 and out:
            files.update(x.strip() for x in out.splitlines() if x.strip())
    code, out, _ = git(repo, 'status', '--porcelain')
    if code == 0 and out:
        for line in out.splitlines():
            if len(line) > 3:
                part = line[3:].strip().split(' -> ')[-1]
                if part:
                    files.add(part)
    return sorted(files)


def affected_tests(repo, failed_tests, changed_files):
    """Compute partial test modules to run: previously failed modules + modules affected by changed_files."""
    repo = Path(repo)
    chosen = list(dict.fromkeys(t for t in (failed_tests or ()) if t and t.startswith('tests.test_')))
    tests_dir = repo / 'tests'
    for rel in changed_files or ():
        p = Path(rel)
        if p.name.startswith('test_') and p.suffix == '.py':
            mod = 'tests.' + p.stem
            if mod not in chosen:
                chosen.append(mod)
            continue
        if p.suffix == '.py' and tests_dir.is_dir():
            stem = p.stem
            candidate = tests_dir / f'test_{stem}.py'
            if candidate.is_file():
                mod = f'tests.test_{stem}'
                if mod not in chosen:
                    chosen.append(mod)
            for tf in sorted(tests_dir.glob('test_*.py')):
                mod = f'tests.{tf.stem}'
                if mod in chosen:
                    continue
                try:
                    text = tf.read_text(encoding='utf-8', errors='ignore')
                except OSError:
                    continue
                if re.search(r'\b' + re.escape(stem) + r'\b', text):
                    chosen.append(mod)
    return chosen


class FullTestLock:
    """Machine-wide process lock ensuring only one full test suite runs at a time."""
    def __init__(self, project='portable', lock_path=None, wait=False, timeout=1800, sleep=time.sleep):
        self.project = project
        self.path = Path(lock_path or os.environ.get('PORTABLE_TEST_LOCK') or DEFAULT_LOCK)
        self.wait = wait
        self.timeout = timeout
        self.sleep = sleep
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, 'a+', encoding='utf-8')
        deadline = time.monotonic() + max(0, self.timeout)
        while True:
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                holder = self._read_holder()
                if not self.wait or time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    who = f"{holder.get('project') or 'unknown'} (PID {holder.get('pid')})" if holder else 'another process'
                    raise Blocked(f'本机同一时间只跑一个全量测试：{who} 正在运行') from None
                self.sleep(0.2)
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(json.dumps({'project': self.project, 'pid': os.getpid(), 'started': int(time.time())}, ensure_ascii=False))
        self.handle.flush()
        return self

    def _read_holder(self):
        try:
            self.handle.seek(0)
            return json.loads(self.handle.read() or '{}')
        except Exception:
            return {}

    def __exit__(self, *exc):
        if self.handle is not None:
            try:
                self.handle.seek(0)
                self.handle.truncate()
                fcntl.flock(self.handle, fcntl.LOCK_UN)
            except OSError:
                pass
            self.handle.close()
            self.handle = None


def _resolve_project(store, project):
    from .ci import _project
    from .board import load
    repo = _project(store, project).resolve()
    for e in load(store):
        if Path(e['path']).resolve() == repo:
            return e['id'], e.get('name') or e['id'], repo
    key = repo.name + '-' + hashlib.sha256(str(repo).encode()).hexdigest()[:12]
    return key, repo.name, repo


def plan_run(store, project, *, force_full=False, require_sync=True, changed_files=None, git=_git):
    """Decide whether to run 'full' or 'partial', or block on unsynced trunk / >2 full runs."""
    pid, name, repo = _resolve_project(store, project)
    synced, trunk_ref, trunk_sha = check_trunk_synced(repo, git)
    if require_sync and not synced:
        raise Blocked(f'主干 {trunk_ref} 有新改动尚未合入当前分支：先同步主干再跑全量')
    _, head_sha, _ = git(repo, 'rev-parse', '-q', '--verify', 'HEAD^{commit}')
    key = STATE_PREFIX + pid
    last = dict(store.get(key) or {})
    new_trunk = bool(trunk_sha and last.get('trunk_sha') and last.get('trunk_sha') != trunk_sha)
    if new_trunk:
        last = {'trunk_sha': trunk_sha, 'full_runs': 0, 'failed_tests': [], 'head_sha': head_sha}
    patch_files = list(changed_files) if changed_files is not None else _changed_since(repo, last.get('head_sha') or '', git)
    core_hits = touches_core(patch_files)
    failed = [t for t in (last.get('failed_tests') or ()) if t and t.startswith('tests.test_')]
    full_runs = int(last.get('full_runs') or 0)

    if failed and not new_trunk and not core_hits and not force_full:
        targets = affected_tests(repo, failed, patch_files)
        if targets:
            return {
                'project': name,
                'mode': 'partial',
                'targets': targets,
                'failed_tests': failed,
                'changed_files': patch_files,
                'core_hits': [],
                'full_runs': full_runs,
                'trunk': trunk_ref or 'local',
            }

    if full_runs >= MAX_FULL_PER_BATCH and not new_trunk:
        hint = ', '.join(failed) if failed else 'failed modules'
        raise Blocked(f'这一批全量测试已连续跑满 {full_runs} 次（一批最多两次）：请只单跑失败项（{hint}）或用 review {name} --kind 卡住')

    return {
        'project': name,
        'mode': 'full',
        'targets': ['full'],
        'core_hits': core_hits,
        'full_runs': full_runs,
        'trunk': trunk_ref or 'local',
    }


def record_run(store, project, mode, passed, failed_tests=(), *, git=_git):
    """Record a full or partial test execution result."""
    pid, name, repo = _resolve_project(store, project)
    _, trunk_ref, trunk_sha = check_trunk_synced(repo, git)
    _, head_sha, _ = git(repo, 'rev-parse', '-q', '--verify', 'HEAD^{commit}')
    key = STATE_PREFIX + pid
    last = dict(store.get(key) or {})
    if trunk_sha and last.get('trunk_sha') and last.get('trunk_sha') != trunk_sha:
        last = {}
    full_runs = int(last.get('full_runs') or 0)
    if passed:
        full_runs = 0
    elif mode == 'full':
        full_runs = full_runs + 1
    state = {
        'project': name,
        'trunk_ref': trunk_ref,
        'trunk_sha': trunk_sha,
        'head_sha': head_sha,
        'last_mode': mode,
        'passed': bool(passed),
        'failed_tests': [] if passed else [t for t in (failed_tests or ()) if t],
        'full_runs': full_runs,
        'at': int(time.time()),
    }
    store.put(key, state)
    return state


def _extract_failed_modules(output):
    found = []
    for m in re.finditer(r'(?:FAIL|ERROR):\s+\w+\s+\(([\w.]+)\)', output or ''):
        parts = m.group(1).split('.')
        mod = '.'.join(parts[:2]) if len(parts) >= 2 and parts[0] == 'tests' else parts[0]
        if mod.startswith('tests.test_') and mod not in found:
            found.append(mod)
    return found


def verify_entry(repo, *, store_root=None, runner=None, lock_path=None):
    import sys
    repo = Path(repo).resolve()
    store_dir = Path(store_root or os.environ.get('PORTABLE_GATE_ROOT') or (Path(tempfile.gettempdir()) / 'suishen-dev-test-gate-store'))
    s = Store(store_dir)
    try:
        plan = plan_run(s, str(repo), force_full=bool(os.environ.get('PORTABLE_FORCE_FULL')),
                        require_sync=not bool(os.environ.get('PORTABLE_NO_SYNC_CHECK')))
        if plan['mode'] == 'partial':
            targets = plan['targets']
            cmd = [sys.executable, '-m', 'unittest', '-v', *targets]
            rc, out = (runner(cmd, repo) if runner else _run_streaming(cmd, repo))
            failed = _extract_failed_modules(out) or (targets if rc != 0 else [])
            record_run(s, str(repo), 'partial', rc == 0, failed)
            return rc, plan
        with FullTestLock(project=repo.name, lock_path=lock_path, wait=True):
            cmd = ['bash', 'scripts/verify.sh', '--raw']
            rc, out = (runner(cmd, repo) if runner else _run_streaming(cmd, repo))
            failed = _extract_failed_modules(out)
            record_run(s, str(repo), 'full', rc == 0, failed)
            return rc, plan
    finally:
        s.db.close()


def _run_streaming(cmd, cwd):
    import sys
    env = dict(os.environ, PORTABLE_FULL_LOCK_HELD='1')
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    captured = []
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    proc.wait()
    return proc.returncode, ''.join(captured)


def run(args):
    s = Store(args.root)
    try:
        if args.action == 'plan':
            return plan_run(s, args.project, force_full=args.force_full, require_sync=not args.no_sync_check)
        if args.action == 'record':
            failed = [x.strip() for x in (args.failed or '').split(',') if x.strip()]
            return record_run(s, args.project, args.mode, args.passed, failed)
        if args.action == 'status':
            pid, name, _ = _resolve_project(s, args.project)
            return s.get(STATE_PREFIX + pid) or {'project': name, 'status': 'empty'}
        raise Blocked('未知动作：' + str(args.action))
    finally:
        s.db.close()


def register(subparser):
    p = subparser.add_parser('test-gate', help='全量测试守门：plan / record / status')
    p.add_argument('action', choices=['plan', 'record', 'status'])
    p.add_argument('project')
    p.add_argument('--force-full', action='store_true')
    p.add_argument('--no-sync-check', action='store_true')
    p.add_argument('--mode', choices=['full', 'partial'], default='full')
    p.add_argument('--passed', action='store_true')
    p.add_argument('--failed', default='')
    p.set_defaults(test_gate_run=run)
    return p
