import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from portable import sealed_bundle as b


class SealedBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / '.git').mkdir()
        (self.source / '.git/history-fixture').write_bytes(b'history\x00fixture')
        (self.source / 'untracked').write_bytes(b'untracked')
        (self.source / 'private-original').write_bytes(bytes(range(256)))
        (self.source / 'private-original').chmod(0o640)
        (self.source / 'empty').mkdir()
        (self.source / 'link').symlink_to('private-original')
        self.package = self.base / 'container'

    def roundtrip(self, sensitive=False):
        expected = b._tree(self.source)
        key = self.base / 'identity'
        args = {}
        if sensitive:
            if not shutil.which('age') or not shutil.which('age-keygen'):
                self.skipTest('REAL age unavailable')
            with open(key, 'wb') as f:
                os.chmod(key, 0o600)
                subprocess.run(['age-keygen'], stdout=f, stderr=subprocess.DEVNULL, check=True)
            recipient = subprocess.check_output(['age-keygen', '-y', str(key)], stderr=subprocess.DEVNULL).decode().strip()
            args = dict(sensitive=True, recipient=recipient)
        b.pack(self.source, self.package, **args)
        self.assertTrue(self.package.is_file())
        self.assertEqual(b.inspect(self.package)['encrypted'], sensitive)
        if sensitive:
            self.assertNotIn(b'private-original', self.package.read_bytes())
            self.assertNotIn(b'history\x00fixture', self.package.read_bytes())
        shutil.rmtree(self.source)  # synthetic original only
        restored = self.base / 'restored'
        b.restore(self.package, restored, **({'identity_file': key} if sensitive else {}))
        self.assertEqual(expected, b._tree(restored))

    def test_plain_roundtrip(self):
        self.roundtrip()

    def test_real_age_roundtrip(self):
        self.roundtrip(True)

    def test_no_age_no_publish(self):
        with patch.object(b.shutil, 'which', return_value=None):
            with self.assertRaises(b.BundleError):
                b.pack(self.source, self.package, sensitive=True, recipient='fixture')
        self.assertFalse(self.package.exists())
        self.assertTrue(self.source.exists())

    def test_existing_targets(self):
        b.pack(self.source, self.package)
        before = self.package.read_bytes()
        with self.assertRaises(b.BundleError):
            b.pack(self.source, self.package)
        with self.assertRaises(b.BundleError):
            b.restore(self.package, self.source)
        self.assertEqual(before, self.package.read_bytes())

    def test_corruption_truncation_version(self):
        b.pack(self.source, self.package)
        original = self.package.read_bytes()
        for data in (original[:-10], original[:-1] + b'X', original.replace(b'"version": 1', b'"version": 2', 1)):
            self.package.write_bytes(data)
            with self.assertRaises(b.BundleError):
                b.restore(self.package, self.base / 'out')
            self.assertFalse((self.base / 'out').exists())

    def test_source_change(self):
        real = b._tree
        calls = 0
        def changing(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.source / 'untracked').write_bytes(b'changed')
            return real(*args)
        with patch.object(b, '_tree', side_effect=changing):
            with self.assertRaises(b.BundleError):
                b.pack(self.source, self.package)
        self.assertFalse(self.package.exists())

    def test_storage_and_interrupt(self):
        for error in (OSError('simulated no space'), KeyboardInterrupt()):
            with patch.object(b.shutil, 'copyfileobj', side_effect=error):
                with self.assertRaises((b.BundleError, KeyboardInterrupt)):
                    b.pack(self.source, self.package)
            self.assertFalse(self.package.exists())
            self.assertTrue((self.source / 'private-original').exists())

    def test_selection_populate(self):
        seen = []
        def populate(root):
            seen.append(root.stat().st_mode & 0o777)
            (root / 'fixture').write_bytes(b'prepared')
        b.pack(None, self.package, populate=populate, selection=['fixture'])
        b.restore(self.package, self.base / 'out')
        self.assertEqual(seen, [0o700])
        self.assertEqual((self.base / 'out/fixture').read_bytes(), b'prepared')

    def test_parent_symlink_and_source_escape(self):
        (self.base / 'alias').symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(b.BundleError):
            b.pack(self.source, self.base / 'alias/package')
        (self.source / 'evil').symlink_to('../outside')
        with self.assertRaises(b.BundleError):
            b.pack(self.source, self.package)

    def malicious(self, entries, members):
        archive = self.base / 'evil.tar'
        with tarfile.open(archive, 'w') as tar:
            data = json.dumps(dict(version=1, entries=entries)).encode()
            info = tarfile.TarInfo('manifest.json')
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            for name, kind, link in members:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.linkname = link
                tar.addfile(info)
        data = archive.read_bytes()
        h = dict(version=1, encrypted=False, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.package.write_bytes(b.MAGIC + json.dumps(h).encode() + b'\n' + data)
        with self.assertRaises(b.BundleError):
            b.restore(self.package, self.base / 'out')
        self.assertFalse((self.base / 'out').exists())

    def test_malicious_archives(self):
        for name in ('../escape', '/absolute', 'a/../../escape'):
            self.malicious([dict(path=name, kind='dir', mode=0o700)], [])
        e = dict(path='x', kind='link', mode=0o777, link='../escape')
        self.malicious([e], [('tree/x', tarfile.SYMTYPE, '../escape')])
        e = dict(path='x', kind='file', mode=0o600, size=0, sha256=hashlib.sha256(b'').hexdigest())
        self.malicious([e], [('tree/x', tarfile.LNKTYPE, 'outside')])
        self.malicious([e], [('tree/x', tarfile.REGTYPE, ''), ('tree/x', tarfile.REGTYPE, '')])
        self.malicious([dict(path='x', kind='file', mode=0o600, size=b.MAX_BYTES + 1)], [])
        self.malicious([dict(path='x', kind='dir', mode=0o700)], [('tree/x', tarfile.FIFOTYPE, '')])
        self.malicious([dict(path='a', kind='link', mode=0o777, link='.'), dict(path='b', kind='link', mode=0o777, link='a/..')], [('tree/a', tarfile.SYMTYPE, '.'), ('tree/b', tarfile.SYMTYPE, 'a/..')])

    def test_real_bad_key(self):
        if not shutil.which('age'):
            self.skipTest('REAL age unavailable')
        keys = []
        for i in range(2):
            key = self.base / ('key' + str(i))
            with open(key, 'wb') as f:
                os.chmod(key, 0o600)
                subprocess.run(['age-keygen'], stdout=f, stderr=subprocess.DEVNULL, check=True)
            keys.append(key)
        recipient = subprocess.check_output(['age-keygen', '-y', str(keys[0])], stderr=subprocess.DEVNULL).decode().strip()
        b.pack(self.source, self.package, sensitive=True, recipient=recipient)
        with self.assertRaises(b.BundleError) as error:
            b.restore(self.package, self.base / 'out', identity_file=keys[1])
        self.assertNotIn('private-original', str(error.exception))
        self.assertFalse((self.base / 'out').exists())

    def test_publish_failure_cleans_only_new_file(self):
        original = b.shutil.copyfileobj
        def fail_publish(src, dst, *args, **kwargs):
            if getattr(src, 'name', '').endswith('/ready'):
                dst.write(b'incomplete')
                raise OSError('simulated full device')
            return original(src, dst, *args, **kwargs)
        with patch.object(b.shutil, 'copyfileobj', side_effect=fail_publish):
            with self.assertRaises(b.BundleError):
                b.pack(self.source, self.package)
        self.assertFalse(self.package.exists())
        self.assertTrue(self.source.exists())

    def test_restore_interrupt_cleanup(self):
        b.pack(self.source, self.package)
        with patch.object(b.shutil, 'copytree', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                b.restore(self.package, self.base / 'out')
        self.assertFalse((self.base / 'out').exists())
        self.assertTrue(self.package.exists())

    def test_inner_digest_mismatch(self):
        b.pack(self.source, self.package)
        raw = self.package.read_bytes().split(b'\n', 2)[2]
        raw = raw.replace(b'history\x00fixture', b'history\x00CHANGED')
        h = dict(version=1, encrypted=False, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        self.package.write_bytes(b.MAGIC + json.dumps(h).encode() + b'\n' + raw)
        with self.assertRaises(b.BundleError):
            b.restore(self.package, self.base / 'out')

    def test_password_non_tty_rejected(self):
        with patch.object(b.os, 'isatty', return_value=False):
            with self.assertRaises(b.BundleError):
                b.pack(self.source, self.package, sensitive=True)
        self.assertFalse(self.package.exists())

    def test_restore_concurrent_note_and_same_name_payload_preserved(self):
        """Independent reviewer repro: another writer wins publication race."""
        import threading
        b.pack(self.source, self.package)
        original_tree = b._tree(self.source)
        original_package = self.package.read_bytes()
        real_publish = b._publish
        for content_name in ('new-user-note.md', 'private-original'):
            target = self.base / ('out-' + content_name)
            start = threading.Event()
            finished = threading.Event()
            errors = []
            def writer():
                try:
                    if not start.wait(5):
                        raise AssertionError('publish barrier not reached')
                    target.mkdir()
                    (target / content_name).write_bytes(b'independent new content')
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()
            worker = threading.Thread(target=writer)
            worker.start()
            def race(staged, destination):
                self.assertFalse(target.exists())
                self.assertEqual(staged.parent.parent, target.parent)
                self.assertEqual(staged.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(b._tree(staged), original_tree)
                start.set()
                self.assertTrue(finished.wait(5))
                if errors:
                    raise errors[0]
                # Real kernel exclusive rename must reject even a directory.
                real_publish(staged, destination)
            try:
                with patch.object(b, '_publish', side_effect=race):
                    with self.assertRaises(b.BundleError):
                        b.restore(self.package, target)
            finally:
                start.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual((target / content_name).read_bytes(), b'independent new content')
            self.assertEqual(list(target.iterdir()), [target / content_name])
        self.assertEqual(b._tree(self.source), original_tree)
        self.assertEqual(self.package.read_bytes(), original_package)
        self.assertEqual(list(self.base.glob('.sealed-*')), [])

    def test_pack_fsync_replacement_repro_preserves_both_inodes(self):
        """Real rename/create at fsync, not a generic injected write failure."""
        real_fsync = b.os.fsync
        retained = self.base / 'retained-original-container'
        triggered = []
        before = b._tree(self.source)
        def race(fd):
            staged = list(self.base.glob('.sealed-*/container'))
            if staged and staged[0].stat().st_ino == os.fstat(fd).st_ino:
                self.assertFalse(self.package.exists())
                # Reproduce the reviewer's retained inode plus replacement.
                staged[0].rename(retained)
                self.package.write_bytes(b'independent replacement')
                triggered.append(True)
                raise OSError('simulated fsync failure after replacement')
            return real_fsync(fd)
        with patch.object(b.os, 'fsync', side_effect=race):
            with self.assertRaises(b.BundleError):
                b.pack(self.source, self.package)
        self.assertEqual(triggered, [True])
        self.assertEqual(self.package.read_bytes(), b'independent replacement')
        self.assertFalse(b.inspect(retained)['encrypted'])
        b.restore(retained, self.base / 'retained-restored')
        self.assertEqual(b._tree(self.base / 'retained-restored'), before)
        self.assertEqual(b._tree(self.source), before)
        self.assertEqual(list(self.base.glob('.sealed-*')), [])

    def test_pack_concurrent_target_created_at_publication(self):
        real_publish = b._publish
        before = b._tree(self.source)
        def race(staged, target):
            target.write_bytes(b'new concurrent file')
            real_publish(staged, target)
        with patch.object(b, '_publish', side_effect=race):
            with self.assertRaises(b.BundleError):
                b.pack(self.source, self.package)
        self.assertEqual(self.package.read_bytes(), b'new concurrent file')
        self.assertEqual(b._tree(self.source), before)

    def test_no_cleanup_after_publication_even_if_later_error(self):
        # Once the rename is visible, no code may assume ownership of its contents.
        real_publish = b._publish
        b.pack(self.source, self.package)
        original = self.package.read_bytes()
        target = self.base / 'restored'
        def restore_race(staged, destination):
            real_publish(staged, destination)
            (destination / 'new-user-note.md').write_bytes(b'new note')
            (destination / 'private-original').write_bytes(b'concurrent same-name payload')
            raise OSError('simulated failure after public rename')
        with patch.object(b, '_publish', side_effect=restore_race):
            with self.assertRaises(b.BundleError):
                b.restore(self.package, target)
        self.assertEqual((target / 'new-user-note.md').read_bytes(), b'new note')
        self.assertEqual((target / 'private-original').read_bytes(), b'concurrent same-name payload')
        self.assertEqual(self.package.read_bytes(), original)
        new_package = self.base / 'new-container'
        retained = self.base / 'retained-container'
        def pack_race(staged, destination):
            real_publish(staged, destination)
            destination.rename(retained)
            destination.write_bytes(b'replacement after publish')
            raise OSError('simulated failure after public replacement')
        with patch.object(b, '_publish', side_effect=pack_race):
            with self.assertRaises(b.BundleError):
                b.pack(self.source, new_package)
        self.assertEqual(new_package.read_bytes(), b'replacement after publish')
        self.assertEqual(retained.read_bytes(), original)

    def test_native_exclusive_publication_never_replaces_any_target(self):
        # Exercise real Darwin renamex_np on file, empty/nonempty dir and symlink.
        staged_file = self.base / 'staged-file'
        staged_file.write_bytes(b'our file')
        staged_dir = self.base / 'staged-dir'
        staged_dir.mkdir()
        (staged_dir / 'payload').write_bytes(b'our tree')
        targets = [self.base / ('existing-' + str(i)) for i in range(4)]
        targets[0].write_bytes(b'old target')
        targets[1].mkdir()
        targets[2].mkdir()
        (targets[2] / 'note').write_bytes(b'user note')
        targets[3].symlink_to('missing-target')
        inodes = [p.lstat().st_ino for p in targets]
        for source in (staged_file, staged_dir):
            for target in targets:
                with self.assertRaises(b.BundleError):
                    b._publish(source, target)
                self.assertTrue(source.exists())
                self.assertEqual([p.lstat().st_ino for p in targets], inodes)
        self.assertEqual(targets[0].read_bytes(), b'old target')
        self.assertEqual((targets[2] / 'note').read_bytes(), b'user note')
        self.assertEqual(os.readlink(targets[3]), 'missing-target')
        # Positive control: exactly one rename publishes a complete tree/file.
        b._publish(staged_file, self.base / 'published-file')
        b._publish(staged_dir, self.base / 'published-dir')
        self.assertFalse(staged_file.exists())
        self.assertFalse(staged_dir.exists())
        self.assertEqual((self.base / 'published-file').read_bytes(), b'our file')
        self.assertEqual((self.base / 'published-dir/payload').read_bytes(), b'our tree')

    def test_unsupported_atomic_publish_fails_closed(self):
        b.pack(self.source, self.package)
        before = self.package.read_bytes()
        with patch.object(b.sys, 'platform', 'unsupported-test-platform'):
            with self.assertRaises(b.BundleError):
                b.restore(self.package, self.base / 'out')
            with self.assertRaises(b.BundleError):
                b.pack(self.source, self.base / 'new-package')
        self.assertFalse((self.base / 'out').exists())
        self.assertFalse((self.base / 'new-package').exists())
        self.assertEqual(self.package.read_bytes(), before)
        self.assertTrue((self.source / 'private-original').exists())
        self.assertEqual(list(self.base.glob('.sealed-*')), [])
