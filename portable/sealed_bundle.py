"""Trusted, normalized trees in portable containers. No Git/database normalization."""
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

MAGIC = b'PORTABLE-SEALED/1\n'
MAX_BYTES = 8 * 1024**3
MAX_ENTRIES = 100000
MAX_MANIFEST = 16 * 1024**2


class BundleError(ValueError):
    """Operation failed; messages intentionally omit source names and tool output."""


def _fail():
    raise BundleError('Container validation or operation failed') from None


def _parents(path):
    p = Path(os.path.abspath(path))
    for ancestor in [p.parent, *p.parent.parents]:
        if ancestor.is_symlink() or not ancestor.is_dir():
            _fail()
    return p


def _name(name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        _fail()
    p = PurePosixPath(name)
    if p.is_absolute() or any(x in ('', '.', '..') for x in name.split('/')):
        _fail()
    return p


def _link(name, target):
    if not isinstance(target, str) or not target or target.startswith('/') or '\\' in target or '\x00' in target:
        _fail()
    parts = list(PurePosixPath(name).parent.parts)
    for item in target.split('/'):
        if item == '..':
            if not parts:
                _fail()
            parts.pop()
        elif item not in ('', '.'):
            parts.append(item)


def _stability(root):
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for p in [Path(directory), *(Path(directory) / n for n in dirs + files)]:
            s = p.lstat()
            result.append((str(p), s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns))
    return sorted(result)


def _digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _tree(root, selection=None):
    if root.is_symlink() or not root.is_dir():
        _fail()
    selected = None if selection is None else set(selection)
    if selected is not None:
        for n in selected:
            _name(n)
    result = []
    total = 0
    def visit(directory):
        nonlocal total
        for p in sorted(directory.iterdir()):
            n = p.relative_to(root).as_posix()
            s = p.lstat()
            if stat.S_ISDIR(s.st_mode):
                kind = 'dir'
            elif stat.S_ISREG(s.st_mode):
                kind = 'file'
            elif stat.S_ISLNK(s.st_mode):
                kind = 'link'
            else:
                _fail()
            include = selected is None or any(n == x or n.startswith(x + '/') or x.startswith(n + '/') for x in selected)
            if include:
                e = dict(path=n, kind=kind, mode=stat.S_IMODE(s.st_mode))
                if kind == 'file':
                    total += s.st_size
                    if total > MAX_BYTES:
                        _fail()
                    e.update(size=s.st_size, sha256=_digest(p))
                if kind == 'link':
                    e['link'] = os.readlink(p)
                    _link(n, e['link'])
                    if not p.resolve().is_relative_to(root.resolve()):
                        _fail()
                result.append(e)
                if len(result) > MAX_ENTRIES or total > MAX_BYTES:
                    _fail()
            if kind == 'dir':
                visit(p)
    visit(root)
    if selected is not None and not selected.issubset({e['path'] for e in result}):
        _fail()
    return result


def _age(args, source, target):
    if args in (['-p'], ['-d']) and not os.isatty(0):
        raise BundleError('Personal TTY required for password operation')
    binary = shutil.which('age')
    if not binary:
        raise BundleError('age is required; no plaintext fallback')
    with open(source, 'rb') as src, open(target, 'xb') as dst:
        os.chmod(target, 0o600)
        r = subprocess.run([binary, *args], stdin=src, stdout=dst, stderr=subprocess.DEVNULL)
    if r.returncode:
        raise BundleError('age operation failed (key, TTY, input or storage)')


def _private(*, directory=None):
    d = tempfile.TemporaryDirectory(prefix='.sealed-', dir=directory)
    os.chmod(d.name, 0o700)
    return d


def _publish(source, target):
    """Atomic no-replace rename. Never unlink or clean a public target path.

    Both paths must be on the same filesystem. Unsupported platforms/filesystems
    fail closed; exists()+rename() is never an acceptable fallback.
    """
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        rename = getattr(library, 'renamex_np', None)
        if rename is not None:
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(os.fsencode(source), os.fsencode(target), 0x00000004)  # RENAME_EXCL
        else:
            result = -1
    elif sys.platform.startswith('linux'):
        rename = getattr(library, 'renameat2', None)
        if rename is not None:
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                               ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)  # RENAME_NOREPLACE
        else:
            result = -1
    else:
        result = -1
    if result != 0:
        raise BundleError('Atomic no-replace publication failed or unsupported') from None


def pack(source, target, *, sensitive=False, recipient=None, selection=None, populate=None):
    """Pack a trusted stable tree. populate(private_path) is trusted Python only."""
    target = _parents(target)
    if os.path.lexists(target):
        _fail()
    if recipient and not sensitive:
        _fail()
    try:
        with _private() as tmp:
            tmp = Path(tmp)
            root = Path(source).absolute() if source is not None else None
            if populate is not None:
                root = tmp / 'tree'
                root.mkdir(mode=0o700)
                populate(root)
            if target == root or root in target.parents:
                _fail()
            stable = _stability(root)
            entries = _tree(root, selection)
            manifest = json.dumps(dict(version=1, entries=entries), sort_keys=True).encode()
            if len(manifest) > MAX_MANIFEST:
                _fail()
            archive = tmp / 'archive'
            with open(archive, 'xb') as out:
                os.chmod(archive, 0o600)
                with tarfile.open(fileobj=out, mode='w', format=tarfile.PAX_FORMAT) as tar:
                    import io
                    info = tarfile.TarInfo('manifest.json')
                    info.size = len(manifest)
                    info.mode = 0o600
                    tar.addfile(info, io.BytesIO(manifest))
                    for e in entries:
                        tar.add(root / e['path'], arcname='tree/' + e['path'], recursive=False)
            if entries != _tree(root, selection) or stable != _stability(root):
                raise BundleError('Source changed during packaging')
            # Validate archived bytes too, detecting a change during the actual read.
            check = tmp / 'check'
            check.mkdir(mode=0o700)
            _extract(archive, check)
            payload = archive
            if sensitive:
                payload = tmp / 'cipher'
                _age(['-r', recipient] if recipient else ['-p'], archive, payload)
            header = dict(version=1, encrypted=bool(sensitive), size=payload.stat().st_size, sha256=_digest(payload))
            ready = tmp / 'ready'
            with open(ready, 'xb') as f:
                os.chmod(ready, 0o600)
                f.write(MAGIC + json.dumps(header, sort_keys=True).encode() + b'\n')
                with open(payload, 'rb') as src:
                    shutil.copyfileobj(src, f)
                f.flush()
                os.fsync(f.fileno())
            # Only a private sibling is writable before atomic publication.
            # Cleanup owns that private directory, never the public target name.
            # Media staging holds only the selected public container or ciphertext;
            # do not require POSIX chmod support from the removable filesystem.
            with tempfile.TemporaryDirectory(prefix='.sealed-', dir=target.parent) as adjacent:
                staged = Path(adjacent) / 'container'
                fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as dst, open(ready, 'rb') as src:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
                _read(staged)
                _publish(staged, target)
            return header
    except BundleError:
        raise
    except (OSError, ValueError, tarfile.TarError, TypeError, KeyError, RuntimeError):
        _fail()


def _read(package, destination=None):
    with open(package, 'rb') as f:
        if f.readline(64) != MAGIC:
            _fail()
        line = f.readline(4097)
        if len(line) > 4096:
            _fail()
        h = json.loads(line)
        if set(h) != {'version', 'encrypted', 'size', 'sha256'} or type(h['version']) is not int or h['version'] != 1 or type(h['encrypted']) is not bool or type(h['size']) is not int or not 0 <= h['size'] <= MAX_BYTES + MAX_MANIFEST + MAX_ENTRIES * 4096:
            _fail()
        digest = hashlib.sha256()
        count = 0
        while block := f.read(1024 * 1024):
            count += len(block)
            if count > h['size']:
                _fail()
            digest.update(block)
            if destination:
                destination.write(block)
        if count != h['size'] or digest.hexdigest() != h['sha256']:
            _fail()
        return h


def inspect(package):
    """Validate outer digest; encrypted content requires restore to validate."""
    try:
        return _read(package)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        _fail()


def _extract(archive, root):
    with tarfile.open(archive, 'r:') as tar:
        first = tar.next()
        if first is None or first.name != 'manifest.json' or not first.isfile() or first.size > MAX_MANIFEST:
            _fail()
        m = json.load(tar.extractfile(first))
        if set(m) != {'version', 'entries'} or type(m['version']) is not int or m['version'] != 1 or not isinstance(m['entries'], list) or len(m['entries']) > MAX_ENTRIES:
            _fail()
        expected = {}
        total = 0
        for e in m['entries']:
            n = e['path']
            _name(n)
            if n in expected or e['kind'] not in ('dir', 'file', 'link') or type(e['mode']) is not int or not 0 <= e['mode'] <= 0o777:
                _fail()
            fields = {'path', 'kind', 'mode'} | ({'size', 'sha256'} if e['kind'] == 'file' else {'link'} if e['kind'] == 'link' else set())
            if set(e) != fields:
                _fail()
            if e['kind'] == 'file':
                if type(e['size']) is not int or e['size'] < 0:
                    _fail()
                total += e['size']
            if e['kind'] == 'link':
                _link(n, e['link'])
            expected[n] = e
        if total > MAX_BYTES:
            _fail()
        for n in expected:
            for parent in PurePosixPath(n).parents:
                if str(parent) != '.' and (str(parent) not in expected or expected[str(parent)]['kind'] != 'dir'):
                    _fail()
        seen = set()
        for member in tar:
            # tar iterator includes the first member even after next().
            if member is first:
                continue
            if not member.name.startswith('tree/'):
                _fail()
            n = member.name[5:]
            if n not in expected or n in seen:
                _fail()
            seen.add(n)
            e = expected[n]
            p = root / n
            if e['kind'] == 'dir' and member.isdir():
                p.mkdir(mode=0o700)
            elif e['kind'] == 'file' and member.isfile() and member.size == e['size']:
                with open(p, 'xb') as dst:
                    os.chmod(p, 0o600)
                    shutil.copyfileobj(tar.extractfile(member), dst)
                if _digest(p) != e['sha256']:
                    _fail()
            elif e['kind'] == 'link' and member.issym() and member.linkname == e['link']:
                os.symlink(e['link'], p)
            else:
                _fail()
        if seen != set(expected):
            _fail()
        # Require conventional end markers; reject hidden concatenated members.
        end = tar.offset
        with open(archive, 'rb') as raw:
            raw.seek(end)
            padding = 0
            while block := raw.read(1024 * 1024):
                if any(block):
                    _fail()
                padding += len(block)
            if padding < 1024 or padding % 512:
                _fail()
        for n, e in expected.items():
            if e['kind'] == 'link' and not (root / n).resolve().is_relative_to(root.resolve()):
                _fail()
        return m


def restore(package, new_target, *, identity_file=None):
    """Restore into a new local directory; never overwrite an existing target."""
    target = _parents(new_target)
    if os.path.lexists(target):
        _fail()
    try:
        with _private() as tmp:
            tmp = Path(tmp)
            payload = tmp / 'payload'
            with open(payload, 'xb') as f:
                os.chmod(payload, 0o600)
                header = _read(package, f)
            archive = payload
            if header['encrypted']:
                args = ['-d']
                if identity_file is not None:
                    key = Path(identity_file)
                    s = key.lstat()
                    if not stat.S_ISREG(s.st_mode) or stat.S_IMODE(s.st_mode) != 0o600 or s.st_uid != os.getuid():
                        _fail()
                    args += ['-i', str(key)]
                archive = tmp / 'plain'
                _age(args, payload, archive)
            elif identity_file is not None:
                _fail()
            tree = tmp / 'tree'
            tree.mkdir(mode=0o700)
            manifest = _extract(archive, tree)
            with _private(directory=target.parent) as adjacent:
                staged = Path(adjacent) / 'tree'
                shutil.copytree(tree, staged, symlinks=True)
                os.chmod(staged, 0o700)
                for e in sorted(manifest['entries'], key=lambda e: len(e['path']), reverse=True):
                    if e['kind'] != 'link':
                        os.chmod(staged / e['path'], e['mode'])
                if sorted(_tree(staged), key=lambda e: e['path']) != sorted(manifest['entries'], key=lambda e: e['path']):
                    _fail()
                _publish(staged, target)
            return header
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        _fail()
