"""Explicit legacy asset selection; inert history, independent Git, atomic transport.

Callbacks are trusted local Python, never serialized commands. preflight reads Git
metadata/status only; capture reads ONLY selected sources. See LEGACY-ASSETS.md.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from .core import Blocked
from . import git_portable as gp
from . import sealed_bundle as sealed
from .migration import SECRET_NAME, SECRET_CONTENT

VERSION = 'portable.legacy-assets/v1'
DENIED = {'.git', 'codex-home', 'auth', 'keychain', 'accounts', 'account-cache',
          'credentials', 'cookies', 'locks', 'pids'}
CONTROL = {'HEAD', 'index', 'ORIG_HEAD', 'MERGE_HEAD', 'MERGE_MSG',
           'MERGE_MODE', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'refs', 'logs',
           'config.worktree', 'AUTO_MERGE', 'SQUASH_MSG', 'FETCH_HEAD',
           'rebase-merge', 'rebase-apply', 'sequencer', 'BISECT_LOG',
           'BISECT_NAMES', 'BISECT_START', 'BISECT_TERMS'}


def _path(value):
    p = Path(os.path.abspath(value))
    if any(q.is_symlink() for q in (p, *p.parents)):
        raise Blocked('symlink source or parent')
    return p


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise Blocked('invalid asset id')
    return value


def _relative(root, name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise Blocked('invalid selection')
    p = Path(name)
    if p.is_absolute() or any(x in ('', '.', '..') for x in name.split('/')):
        raise Blocked('invalid selection')
    if any(x.lower() in DENIED or x.lower().endswith(('.lock', '.pid')) for x in p.parts):
        raise Blocked('execution or account state is not archival input')
    return _path(root / p)


def _records(source):
    raw = gp.git(source, 'worktree', 'list', '--porcelain', '-z').stdout
    records = []
    item = {}
    for field in raw.split(b'\0'):
        if not field:
            if item:
                records.append(item)
                item = {}
            continue
        key, _, value = field.partition(b' ')
        item[os.fsdecode(key)] = os.fsdecode(value)
    if item:
        records.append(item)
    if not records or any('worktree' not in r for r in records):
        raise Blocked('invalid Git worktree metadata')
    return records


def _status_flags(raw):
    records = iter(raw.split(b'\0'))
    staged = unstaged = untracked = False
    for record in records:
        if not record:
            continue
        if len(record) < 3 or record[2:3] != b' ':
            raise Blocked('invalid status metadata')
        xy = record[:2]
        if xy == b'??':
            untracked = True
        elif xy != b'!!':
            staged |= xy[:1] != b' '
            unstaged |= xy[1:2] != b' '
            if b'R' in xy or b'C' in xy:
                next(records, None)  # -z rename/copy second pathname
    return staged, unstaged, untracked


def preflight(projects, *, workspaces=()):
    """Metadata-only report. Aliases collapse by common.git, never enroll WTs."""
    report = dict(format=VERSION, projects=[], workspaces=list(workspaces),
                  complete=False, scope='explicit selection only', unresolved=[])
    common_seen = {}
    ids = set()
    for entry in projects:
        ident = _id(entry['id'])
        if ident in ids:
            raise Blocked('duplicate project id')
        ids.add(ident)
        source = _path(entry['path'])
        if not source.is_dir():
            raise Blocked('registered repository missing')
        _path(source / '.git')
        gd, common = gp.locations(source)
        _path(gd)
        _path(common)
        if common in common_seen:
            common_seen[common]['aliases'].append(ident)
            if entry.get('context_roots') or entry.get('project_home'):
                raise Blocked('put context selection on canonical project')
            continue
        project = dict(id=ident, source=str(source), common=str(common), aliases=[],
                       worktrees=[], contexts=[], suggestions=[])
        common_seen[common] = project
        report['projects'].append(project)
        seen = set()
        for r in _records(source):
            path = _path(r['worktree'])
            if path in seen:
                continue
            seen.add(path)
            exists = path.is_dir()
            status = gp.git(path, 'status', '--porcelain=v1', '-z', '--untracked-files=all').stdout if exists else b''
            if exists and gp.locations(path)[1] != common:
                raise Blocked('worktree common directory mismatch')
            staged, unstaged, untracked = _status_flags(status)
            w = dict(source=str(path), head=r.get('HEAD'), branch=r.get('branch'),
                     exists=exists, dirty=bool(status), staged=staged,
                     unstaged=unstaged, untracked=untracked, locked='locked' in r,
                     prunable='prunable' in r, state=('dirty' if status else 'clean') if exists else 'missing',
                     restore='independent repository' if exists else 'historical control only',
                     unavailable=[] if exists else ['unstaged files', 'untracked files', 'working directory'])
            project['worktrees'].append(w)
            if not exists:
                report['unresolved'].append(dict(project=ident, source=str(path), reason='missing working directory; working bytes unavailable'))
        for value in entry.get('context_roots', []):
            project['contexts'].append(str(_path(value)))
        if entry.get('project_home'):
            home = _path(entry['project_home'])
            # Explicit home is a boundary, not permission to recursively copy it.
            for name in ('CLAUDE.md', 'AGENTS.md', 'README.md', 'docs'):
                p = home / name
                if p.exists():
                    project['suggestions'].append(dict(path=str(p), selected=str(p) in project['contexts']))
        elif source.name == 'repo':
            for name in ('CLAUDE.md', 'AGENTS.md', 'README.md'):
                p = source.parent / name
                if p.is_file() and not p.is_symlink():
                    project['suggestions'].append(dict(path=str(p), selected=False))
    return report


def format_preflight(report):
    """Metadata only: no filenames from status, specs, logs or credentials."""
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)


def _history_reference(name):
    """Validate only the supported state layouts; never read task content."""
    if not isinstance(name, str) or not name or '\\' in name:
        raise Blocked('invalid history selection')
    parts = name.split('/')
    if any(x in ('', '.', '..') for x in parts):
        raise Blocked('invalid history selection')
    if len(parts) == 2 and parts[0] == 'tasks' and parts[1].endswith('.json'):
        return 'tasks', _id(parts[1][:-5])
    if len(parts) >= 3 and parts[0] in ('tasks', 'dispatches'):
        return parts[0], _id(parts[1])
    raise Blocked('select explicit task metadata or task/run evidence')


def history_index(state_root):
    """List task/dispatch ids and flat task JSON paths, without reading content."""
    root = _path(state_root)
    result = []
    for group in ('tasks', 'dispatches'):
        folder = _path(root / group)
        if folder.is_dir():
            for p in sorted(folder.iterdir()):
                if p.is_symlink():
                    continue
                flat = group == 'tasks' and p.is_file() and p.suffix == '.json'
                if not (p.is_dir() or flat):
                    continue
                ident = p.stem if flat else p.name
                try:
                    _id(ident)
                    _relative(root, p.relative_to(root).as_posix())
                except Blocked:
                    continue
                result.append(dict(group=group, id=ident, source=str(p),
                                   layout='flat-json' if flat else 'directory',
                                   checkpoint='unknown; read-only review required'))
    return result


def _private_json(data):
    """Recognize quoted JSON secret fields missed by the legacy text scanner."""
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        return False
    def private(node):
        if isinstance(node, dict):
            return any((re.search(r'(?:password|secret|token|api[_-]?key|authorization|cookie)', key, re.I)
                        and v not in (None, '', False)) or private(v)
                       for key, v in node.items())
        if isinstance(node, list):
            return any(private(v) for v in node)
        return False
    return private(value)


def _scan_copy(source, target, *, sensitive=False):
    """No links, devices, databases or account/runtime artifacts, even encrypted."""
    source = _path(source)
    paths = [source]
    if source.is_dir():
        paths += sorted(source.rglob('*'))
    for p in paths:
        _path(p)
        s = p.lstat()
        if not (stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode)):
            raise Blocked('special archival input')
        if any(x.lower() in DENIED or x.lower().endswith(('.lock', '.pid')) for x in p.relative_to(source.parent).parts):
            raise Blocked('account/runtime artifact in selection; narrow selection required')
        if p.is_file():
            if p.suffix.lower() in ('.db', '.sqlite', '.sqlite3') or p.read_bytes()[:16] == b'SQLite format 3\x00':
                raise Blocked('database requires existing database component')
            if not sensitive and (SECRET_NAME.search(p.relative_to(source.parent).as_posix()) or SECRET_CONTENT.search(p.read_bytes()) or _private_json(p.read_bytes())):
                raise Blocked('private archival input requires age or explicit exclusion')
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


def _signature(paths):
    return {str(p): gp.fingerprint(p) if p.is_dir() else (p.stat().st_mode, p.stat().st_size, p.stat().st_mtime_ns, sealed._digest(p)) for p in paths}


def capture(projects, target, *, histories=(), archives=(), exclusions=(),
            sensitive=False, recipient=None, normalize=None, stopped=None,
            reserve_bytes=0, history_plaintext=False):
    """Atomically pack selected assets. normalize(source,target) must preserve Git.

    histories: [{id,state_root,selections:[tasks/id/spec.json,...]}].
    archives: [{id,path}]. stopped() must return exactly True before publication
    when supplied; false/unknown blocks. It is a local quiescence attestation.
    """
    target = _path(target)
    if os.path.lexists(target):
        raise Blocked('target exists')
    if histories and not sensitive and not history_plaintext:
        raise Blocked('history requires age or explicit public-history selection')
    report = preflight(projects)
    roots = []
    def add(p):
        p = _path(p)
        if not p.exists():
            raise Blocked('selected source missing')
        roots.append(p)
        return p
    code_roots = []
    for p in report['projects']:
        add(p['common'])
        for w in p['worktrees']:
            if w['exists']:
                code_roots.append(add(w['source']))
        for c in p['contexts']:
            add(c)
    # Overlap of common.git under its own main worktree is intentional only.
    selected = code_roots + [Path(c) for p in report['projects'] for c in p['contexts']]
    extra = []
    used = set()
    for role, entries in (('history', histories), ('archives', archives)):
        for entry in entries:
            ident = _id(entry['id'])
            if (role, ident) in used:
                raise Blocked('duplicate asset id')
            used.add((role, ident))
            if role == 'history':
                root = _path(entry['state_root'])
                names = entry['selections']
                if not names:
                    raise Blocked('empty history selection')
                for n in names:
                    _history_reference(n)
                    if len(Path(n).parts) == 2 and not _relative(root, n).is_file():
                        raise Blocked('flat task metadata must be a regular JSON file')
                    extra.append((role, ident, n, add(_relative(root, n))))
            else:
                extra.append((role, ident, 'original', add(entry['path'])))
    selected += [e[3] for e in extra]
    for i, a in enumerate(selected):
        for b in selected[i+1:]:
            if a == b or a in b.parents or b in a.parents:
                raise Blocked('overlapping selected sources')
    if any(target == p or p in target.parents or target in p.parents for p in roots):
        raise Blocked('package overlaps source')
    before = _signature(set(roots))
    estimate = sum(p.stat().st_size for root in set(roots) for p in ([root] if root.is_file() else root.rglob('*')) if p.is_file() and not p.is_symlink())
    if shutil.disk_usage(target.parent).free < estimate * 4 + reserve_bytes:
        raise Blocked('insufficient staging space')
    if stopped is not None and stopped() is not True:
        raise Blocked('stop not confirmed')
    original_discovery = json.loads(json.dumps(report['projects']))
    report.update(histories=[], archives=[], exclusions=list(exclusions),
                  authority='historical evidence only; no approvals, callers or queues activated',
                  complete=False)
    def populate(stage):
        for p in report['projects']:
            base = stage / 'projects' / p['id']
            for i, w in enumerate(p['worktrees']):
                if not w['exists']:
                    continue
                rel = f'projects/{p["id"]}/code/{i}'
                dest = stage / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                (normalize or gp.export)(Path(w['source']), dest)
                if not (dest / '.git').is_dir():
                    raise Blocked('normalizer must produce independent Git')
                gp.git(dest, 'fsck', '--full')
                gd, common = gp.locations(dest)
                if gd != (dest / '.git').resolve() or common != gd or (gd / 'objects/info/alternates').exists():
                    raise Blocked('normalizer left external Git dependency')
                w['restored_path'] = rel
            common = Path(p['common'])
            control = common / 'worktrees'
            p['historical_controls'] = []
            if control.exists():
                for directory in sorted(control.iterdir()):
                    if not directory.is_dir() or directory.is_symlink():
                        raise Blocked('invalid worktree control metadata')
                    pointer = directory / 'gitdir'
                    origin = pointer.read_text().strip() if pointer.is_file() and not pointer.is_symlink() else None
                    p['historical_controls'].append(dict(control_id=directory.name, source_gitdir=origin, restored_path=f'projects/{p["id"]}/historical-control/{directory.name}', omitted=['gitdir', 'commondir', 'locked', 'PID and locks'], policy='inert evidence; working directory unavailable if missing'))
                    unknown = [f.name for f in directory.iterdir() if f.name not in CONTROL and not f.name.startswith('sharedindex.') and f.name not in ('gitdir', 'commondir', 'locked') and not f.name.endswith(('.lock', '.pid'))]
                    if unknown:
                        report['unresolved'].append(dict(project=p['id'], control_id=directory.name, reason='unknown control assets not archived', entries=sorted(unknown)))
                    for f in sorted(directory.iterdir()):
                        if f.name in CONTROL or f.name.startswith('sharedindex.'):
                            _scan_copy(f, base / 'historical-control' / directory.name / f.name, sensitive=sensitive)
            for i, c in enumerate(p['contexts']):
                _scan_copy(Path(c), base / 'context' / str(i), sensitive=sensitive)
        for role, ident, name, source in extra:
            rel = f'{role}/{ident}/{name}'
            if role == 'history' and len(Path(name).parts) == 2:
                try:
                    if not isinstance(json.loads(source.read_bytes()), dict):
                        raise ValueError
                except (ValueError, UnicodeError):
                    raise Blocked('invalid or incomplete flat task JSON metadata') from None
            _scan_copy(source, stage / rel, sensitive=sensitive)
            report['histories' if role == 'history' else 'archives'].append(dict(id=ident, source=str(source), restored_path=rel, checkpoint='unknown; review original evidence'))
        if before != _signature(set(roots)):
            raise Blocked('source changed during capture')
        fresh = preflight(projects)
        original = json.loads(json.dumps(report['projects']))
        for p in original:
            p.pop('historical_controls', None)
        for p in original:
            for w in p['worktrees']:
                w.pop('restored_path', None)
        if fresh['projects'] != original:
            raise Blocked('worktree topology changed')
        if stopped is not None and stopped() is not True:
            raise Blocked('stop no longer confirmed')
        report['complete'] = not report['unresolved'] and not exclusions
        report['selection_captured'] = True
        (stage / 'legacy-manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    with tempfile.TemporaryDirectory(dir=target.parent, prefix='.legacy-capture-') as tmp:
        ready = Path(tmp) / 'package'
        header = sealed.pack(None, ready, sensitive=sensitive, recipient=recipient, populate=populate)
        if before != _signature(set(roots)):
            raise Blocked('source changed during packaging')
        if preflight(projects)['projects'] != original_discovery:
            raise Blocked('worktree topology changed during packaging')
        if stopped is not None and stopped() is not True:
            raise Blocked('stop no longer confirmed')
        sealed._publish(ready, target)
    return header


def restore(package, target, *, identity_file=None):
    """Validate in private staging before no-replace publication; history inert."""
    target = _path(target)
    if os.path.lexists(target):
        raise Blocked('restore target exists')
    with tempfile.TemporaryDirectory(dir=target.parent, prefix='.legacy-') as tmp:
        stage = Path(tmp) / 'restored'
        sealed.restore(package, stage, identity_file=identity_file)
        m = json.loads((stage / 'legacy-manifest.json').read_text())
        if m.get('format') != VERSION or m.get('selection_captured') is not True:
            raise Blocked('invalid legacy manifest')
        for project in m['projects']:
            for w in project['worktrees']:
                if w['exists']:
                    path = _relative(stage, w['restored_path'])
                    gd, common = gp.locations(path)
                    if gd != (path / '.git').resolve() or gd != common or (gd/'objects/info/alternates').exists():
                        raise Blocked('external Git dependency')
                    gp.git(path, 'fsck', '--full')
        sealed._publish(stage, target)
    return m


def continuation_draft(manifest, task_id, *, new_caller, checkpoint=None):
    """No queue write. checkpoint is caller-reviewed data, never inferred status."""
    _id(task_id)
    if not isinstance(new_caller, str) or not new_caller.strip():
        raise Blocked('new execution identity required')
    sources = []
    for h in manifest['histories']:
        parts = Path(h['restored_path']).parts
        if len(parts) < 4 or parts[0] != 'history':
            raise Blocked('invalid historical evidence reference')
        if _history_reference('/'.join(parts[2:])) in (('tasks', task_id), ('dispatches', task_id)):
            sources.append(h)
    if not sources:
        raise Blocked('task not selected')
    cp = checkpoint or {}
    if new_caller == cp.get('source_caller'):
        raise Blocked('historical caller cannot be reused')
    return dict(state='DRAFT_REQUIRES_CURRENT_USER_REVIEW', source_task=task_id,
                source_caller=cp.get('source_caller', 'unknown'), new_caller=new_caller,
                done=cp.get('done', 'unknown'), remaining=cp.get('remaining', 'unknown'),
                next=cp.get('next', 'read-only review of selected evidence'),
                context=cp.get('context', 'unknown'), evidence=sources,
                restrictions=['no inherited approval', 'production/data/accounts require new approval',
                              'formal MCP submission required; no task has resumed'])
