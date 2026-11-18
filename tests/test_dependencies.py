import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from portable.core import Store,Blocked,canonical
from portable.distribution import build

SOURCE=Path(__file__).resolve().parents[1]
class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.release=self.root/'release'
        build(SOURCE,self.release);self.s=Store(self.release)
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def test_only_sealed_generated_environments_are_omitted(self):
        from portable.dependencies import seal_environment,generated_environment_dirs
        from portable import migration as m
        folder=self.release/'工具/runtimes/fixture';(folder/'venv/bin').mkdir(parents=True)
        (folder/'venv/bin/python').symlink_to(sys.executable)
        (folder/'private-home').mkdir()
        sealed=seal_environment(folder);(folder/'install.json').write_text(canonical({'state':'failed','environment_sha256':sealed}))
        excluded=generated_environment_dirs(self.release);self.assertIn('工具/runtimes/fixture/venv',excluded)
        media=self.root/'media';media.mkdir();m.enroll(self.s,media)
        snapshot=Path(m.snapshot(self.s,self.release,media));manifest=m.verify(snapshot)
        self.assertIn('工具/runtimes/fixture/venv',manifest['excluded_derived'])
        self.assertFalse(any('/venv/' in name for name in manifest['files']))
        (folder/'venv/unknown-personal-file').write_text('unknown owner content')
        with self.assertRaises(Blocked):generated_environment_dirs(self.release)
    def test_moved_package_never_uses_old_config_or_environment(self):
        self.s.put('team_config',{'python':'/old/venv/bin/python','server':'/old/candidate/run_server.py','caller':'portable:'+self.s.device})
        self.s.db.execute('PRAGMA wal_checkpoint(FULL)')
        import shutil
        moved=self.root/'moved';shutil.copytree(self.release,moved)
        new=Store(moved)
        try:
            self.assertEqual(new.get('team_config'),{})
        finally:new.db.close()
