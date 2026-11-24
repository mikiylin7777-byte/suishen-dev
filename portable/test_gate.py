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
RESUMABLE_STAGES = ('python', 'bots', 'bot-sdk', 'capabilities')

CORE_RULES = (
    ('billing', re.compile(r'(?:^|/|_)('
                           r'cost|bill|billing|pay|payment|price|pricing|money|finance|invoice|order_amount|wallet|ledger'
                           r')(?:$|/|_|\.)', re.I)),
    ('auth', re.compile(r'(?:^|/|_)('
                        r'auth|authn|authz|perm|permission|role|rbac|acl|token|login|session_auth|privacy|sensitive|security'
                        r')(?:$|/|_|\.)', re.I)),
    ('workflow', re.compile(r'(?:^|/|_)('
                            r'workflows?|state_machine|lifecycle|status_flow|taskline|orchestration|approval_flow|pipeline'
                            r')(?:$|/|_|\.)', re.I)),
    ('schema', re.compile(r'(?:^|/|_)(schema|migrat\w*|models|database|db_adapters|ddl)(?:$|/|_|\.)|\.sql$', re.I)),
    ('shell', re.compile(r'(?:^|/)(__main__\.py|core\.py|protocol\.json|distribution\.py|ci\.py|App\.[jt]sx?|layout\.[jt]sx?|router\.[jt]sx?|shell\.[jt]sx?)$', re.I)),
    ('test gate', re.compile(r'^(?:scripts/(?:verify\.sh|prepare_test_env\.sh|sdk_cache\.py|mcp-sdk\.lock\.txt)|portable/test_gate\.py)$')),
)


def _git(repo, *args, timeout=30):
    r = subprocess.run(['git', '--no-replace-objects', '-C', str(repo), '-c', 'core.quotepath=off', *args],
                       capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
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


def _cached_trunk_ref(repo, git):
    from .board import REMOTE_DEV, REMOTE_MAIN
    for ref in (*REMOTE_DEV, *REMOTE_MAIN):
        code, sha, _ = git(repo, 'rev-parse', '-q', '--verify', ref + '^{commit}')
        if code == 0 and sha:
            return ref, sha
    return None, ''


def _remote_trunk(repo, git):
    if not (repo / '.git').exists():
        return None, ''
    code, out, _ = git(repo, 'remote')
    if code:
        raise Blocked('无法读取 Git 远端配置')
    remotes = out.splitlines()
    if not remotes:
        return None, ''
    remote = next((name for name in ('gitea', 'origin') if name in remotes), None)
    if remote is None:
        if len(remotes) != 1:
            raise Blocked('有多个 Git 远端，无法确定主干来源：请核对远端配置')
        remote = remotes[0]

    branches = ('refs/heads/development', 'refs/heads/main', 'refs/heads/master')
    try:
        code, out, _ = git(repo, 'ls-remote', '--symref', '--', remote, 'HEAD', *branches, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        code, out = 1, ''
    refs = {}
    default_ref = ''
    if code == 0:
        for line in out.splitlines():
            value, _, ref = line.partition('\t')
            if ref == 'HEAD' and value.startswith('ref: refs/heads/'):
                default_ref = value.removeprefix('ref: ')
            elif ref in (*branches, 'HEAD') and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
                refs[ref] = value
    for ref in branches:
        if ref in refs:
            return remote + '/' + ref.removeprefix('refs/heads/'), refs[ref]
    if default_ref and 'HEAD' in refs:
        return remote + '/' + default_ref.removeprefix('refs/heads/'), refs['HEAD']
    raise Blocked(f'无法核实 {remote} 的最新主干：请检查连接及远端默认分支')


def _test_revision(repo, git, require_sync, last):
    if not (repo / '.git').exists():
        return None, '', ''
    _, head_sha, _ = git(repo, 'rev-parse', '-q', '--verify', 'HEAD^{commit}')
    if not require_sync:
        if 'trunk_sha' in last:
            return last.get('trunk_ref'), last['trunk_sha'], head_sha
        return (*_cached_trunk_ref(repo, git), head_sha)
    ref, trunk_sha = _remote_trunk(repo, git)
    if trunk_sha and trunk_sha != head_sha:
        code, _, _ = git(repo, 'merge-base', '--is-ancestor', trunk_sha, head_sha)
        if code:
            raise Blocked(f'主干 {ref} 有新改动尚未合入当前分支：先同步主干再跑全量')
    return ref, trunk_sha, head_sha


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
    key = STATE_PREFIX + pid
    last = dict(store.get(key) or {})
    trunk_ref, trunk_sha, head_sha = _test_revision(repo, git, require_sync, last)
    state_version = int(last.get('state_version') or 0)
    new_trunk = bool(trunk_sha and 'trunk_sha' in last and last['trunk_sha'] != trunk_sha)
    if new_trunk:
        last = {'trunk_sha': trunk_sha, 'full_runs': 0, 'failed_tests': [], 'head_sha': head_sha}
    patch_files = list(changed_files) if changed_files is not None else _changed_since(repo, last.get('head_sha') or '', git)
    core_hits = touches_core(patch_files)
    failed = [t for t in (last.get('failed_tests') or ()) if t and t.startswith('tests.test_')]
    resume_stage = last.get('resume_stage')
    if resume_stage not in RESUMABLE_STAGES:
        resume_stage = None
    full_runs = int(last.get('full_runs') or 0)
    plan = {
        'project': name,
        'full_runs': full_runs,
        'trunk': trunk_ref or 'local',
        'trunk_sha': trunk_sha,
        'head_sha': head_sha,
    }

    if (failed or resume_stage) and not new_trunk and not core_hits and not force_full:
        targets = affected_tests(repo, failed, patch_files)
        if resume_stage and targets:
            resume_stage = 'python'
        elif resume_stage and any(
            path.startswith('adapters/bots/') and path != 'adapters/bots/sdk-smoke.mjs' for path in patch_files
        ):
            resume_stage = 'bots'
        elif resume_stage == 'capabilities' and 'adapters/bots/sdk-smoke.mjs' in patch_files:
            resume_stage = 'bot-sdk'
        if targets or resume_stage:
            return {
                **plan,
                'mode': 'partial',
                'targets': targets,
                'resume_stage': resume_stage,
                'state_version': state_version,
                'failed_tests': failed,
                'changed_files': patch_files,
                'core_hits': [],
            }

    if full_runs >= MAX_FULL_PER_BATCH and not new_trunk:
        hint = ', '.join(failed) if failed else resume_stage or '未识别到具体失败项'
        raise Blocked(f'这一批全量测试已连续跑满 {full_runs} 次（一批最多两次）：请只单跑失败项（{hint}）或用 review {name} --kind 卡住')

    return {
        **plan,
        'mode': 'full',
        'targets': ['full'],
        'core_hits': core_hits,
    }


def record_run(store, project, mode, passed, failed_tests=(), *, plan=None, resume_stage=None, require_sync=True, git=_git):
    """Record a full or partial test execution result."""
    pid, name, repo = _resolve_project(store, project)
    key = STATE_PREFIX + pid
    if plan is None:
        trunk_ref, trunk_sha, head_sha = _test_revision(repo, git, require_sync, store.get(key) or {})
    else:
        trunk_ref = plan['trunk'] if plan['trunk_sha'] else None
        trunk_sha, head_sha = plan['trunk_sha'], plan['head_sha']
    with store.db:
        last = store.get(key) or {}
        version = int(last.get('state_version') or 0)
        if mode == 'partial' and plan is not None and version != plan['state_version']:
            raise Blocked('局部测试期间已有更新的测试结果：请重新运行守门，按最新结果续跑')
        full_runs = int(last.get('full_runs') or 0)
        if passed or (trunk_sha and 'trunk_sha' in last and last['trunk_sha'] != trunk_sha):
            full_runs = 0
        if not passed and mode == 'full' and resume_stage != 'setup':
            full_runs += 1
        state = {
            'project': name,
            'trunk_ref': trunk_ref,
            'trunk_sha': trunk_sha,
            'head_sha': head_sha,
            'last_mode': mode,
            'passed': bool(passed),
            'failed_tests': [] if passed else [t for t in (failed_tests or ()) if t],
            'resume_stage': None if passed else resume_stage,
            'full_runs': full_runs,
            'state_version': version + 1,
            'at': int(time.time()),
        }
        store.put(key, state)
    return state


def _extract_failed_modules(output):
    found = []
    for m in re.finditer(r'^(?:FAIL|ERROR):[ \t]+\w+[ \t]+\(([\w.]+)\)', output or '', re.MULTILINE):
        parts = m.group(1).split('.')
        # unittest discover -s tests reports test_foo, while a partial run
        # imports the module using its tests.test_foo name.
        module = parts[1] if len(parts) >= 2 and parts[0] == 'tests' else parts[0]
        target = f'tests.{module}'
        if module.startswith('test_') and target not in found:
            found.append(target)
    return found


def verify_entry(repo, *, store_root=None, runner=None, lock_path=None):
    import sys
    repo = Path(repo).resolve()
    # macOS 的 /var、/tmp 是符号链接。不解析的话，运行目录的上级会被认成逃逸，全量守门还没开测就停。
    default_store = Path(tempfile.gettempdir()).resolve() / 'suishen-dev-test-gate-store'
    store_dir = Path(store_root or os.environ.get('PORTABLE_GATE_ROOT') or default_store)
    s = Store(store_dir)
    try:
        options = {
            'force_full': bool(os.environ.get('PORTABLE_FORCE_FULL')),
            'require_sync': not bool(os.environ.get('PORTABLE_NO_SYNC_CHECK')),
        }
        plan = plan_run(s, str(repo), **options)
        if plan['mode'] == 'full':
            with FullTestLock(project=repo.name, lock_path=lock_path, wait=True):
                # An earlier request may have spent the last full run or found partial targets.
                plan = plan_run(s, str(repo), **options)
                if plan['mode'] == 'full':
                    rc, failed, stage = _run_verification(repo, runner)
                    record_run(s, str(repo), 'full', rc == 0, failed, plan=plan, resume_stage=stage)
                    return rc, plan
        targets = plan['targets']
        stage = plan['resume_stage']
        if stage:
            rc, failed, stage = _run_verification(repo, runner, stage=stage, targets=targets)
        else:
            cmd = [sys.executable, '-m', 'unittest', '-v', *targets]
            rc, out = runner(cmd, repo) if runner else _run_streaming(cmd, repo, add_test_directory=True)
            failed = (_extract_failed_modules(out) or targets) if rc else []
        record_run(s, str(repo), 'partial', rc == 0, failed, plan=plan, resume_stage=stage)
        return rc, plan
    finally:
        s.db.close()


def _run_verification(repo, runner, *, stage=None, targets=()):
    cmd = ['bash', 'scripts/verify.sh', '--raw']
    if stage:
        cmd.extend(['--resume-from', stage, *targets])
    started = ''
    if runner:
        rc, out = runner(cmd, repo)
    else:
        with tempfile.TemporaryDirectory(prefix='portable-verify-') as directory:
            marker = Path(directory) / 'stage'
            rc, out = _run_streaming(cmd, repo, stage_file=marker)
            if marker.is_file():
                started = marker.read_text(encoding='utf-8').strip()
    if not rc:
        return rc, [], None
    if started == 'setup':
        # No test ran. Preserve a pending resume and leave the full-run budget intact.
        return rc, list(targets), stage or 'setup'
    failed = _extract_failed_modules(out) if started in ('', 'python') else []
    if started == 'python':
        failed = failed or list(targets)
    resume_stage = started if started in RESUMABLE_STAGES else None
    if not started and stage:
        resume_stage = stage
        failed = failed or list(targets)
    if resume_stage == 'python' and not failed:
        resume_stage = None
    return rc, failed, resume_stage


def _run_streaming(cmd, cwd, *, add_test_directory=False, stage_file=None):
    import sys
    env = dict(os.environ, PORTABLE_FULL_LOCK_HELD='1')
    if stage_file is not None:
        env['PORTABLE_VERIFY_STAGE_FILE'] = str(stage_file)
    if add_test_directory:
        # Match discovery's sibling import path for tests that import helpers.
        paths = (str(cwd / 'tests'), env.get('PYTHONPATH'))
        env['PYTHONPATH'] = os.pathsep.join(path for path in paths if path)
    captured = []
    # 父进程已经占着全机锁。套件里再申请同一路径会一直等到超时，所以套件进程用自己的锁文件。
    with tempfile.TemporaryDirectory(prefix='portable-suite-lock-') as directory:
        env['PORTABLE_TEST_LOCK'] = str(Path(directory).resolve() / 'suite.lock')
        with subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                captured.append(line)
    return proc.returncode, ''.join(captured)


def run(args):
    s = Store(args.root)
    try:
        if args.action == 'plan':
            return plan_run(s, args.project, force_full=args.force_full, require_sync=not args.no_sync_check)
        if args.action == 'record':
            failed = [x.strip() for x in (args.failed or '').split(',') if x.strip()]
            return record_run(s, args.project, args.mode, args.passed, failed, require_sync=not args.no_sync_check)
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
