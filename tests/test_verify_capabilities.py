import os
from pathlib import Path
import importlib.util
import unittest
from unittest.mock import patch

def load():
    path=Path(__file__).resolve().parents[1]/'scripts/verify_capabilities.py'
    spec=importlib.util.spec_from_file_location('verify_capabilities_under_test', path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class ClientLoadRequirement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod=load()

    def test_not_installed_does_not_count_as_loaded(self):
        checks={'codex':{'status':'not_installed','loaded':False},'claude':{'status':'not_installed','loaded':False}}
        self.assertEqual(self.mod.loaded_clients(checks), [])

    def test_one_loaded_client_is_enough(self):
        checks={'codex':{'status':'load_failed','loaded':False},'claude':{'status':'loaded','loaded':True}}
        self.assertEqual(self.mod.loaded_clients(checks), ['claude'])

    def test_skip_is_explicit(self):
        self.assertTrue(self.mod.skip_client_load(['--skip-client-checks']))
        self.assertFalse(self.mod.skip_client_load([]))
        with patch.dict(os.environ, {'PORTABLE_SKIP_CLIENT_CHECKS':'1'}):
            self.assertTrue(self.mod.skip_client_load([]))
        with patch.dict(os.environ, {'PORTABLE_SKIP_CLIENT_CHECKS':''}):
            self.assertFalse(self.mod.skip_client_load([]))

    def test_path_gains_local_bin_when_that_directory_exists(self):
        local=str(Path.home()/'.local'/'bin')
        with patch.dict(os.environ, {'PATH':'/usr/bin:/bin'}):
            self.mod.augment_client_path()
            parts=os.environ['PATH'].split(os.pathsep)
            self.assertIn('/usr/bin', parts)
            if Path(local).is_dir():
                self.assertIn(local, parts)
                self.assertLess(parts.index(local), parts.index('/usr/bin'))
