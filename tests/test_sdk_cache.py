import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import sdk_cache


class SDKCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        sandbox = Path(temp.name).resolve()
        self.checkout = sandbox / "checkout"
        scripts = self.checkout / "scripts"
        scripts.mkdir(parents=True)
        self.lock = scripts / "mcp-sdk.lock.txt"
        self.lock.write_text("mcp==1.0\n")
        self.cache = sandbox / "cache"
        source_file = patch.object(sdk_cache, "__file__", str(scripts / "sdk_cache.py"))
        environment = patch.dict(os.environ, {"PORTABLE_TEST_CACHE_DIR": str(self.cache)}, clear=True)
        runtime = patch.object(sdk_cache.subprocess, "check_output", return_value=b"fixture-runtime")
        for context in (source_file, environment, runtime):
            context.start()
            self.addCleanup(context.stop)

    def test_override_keys_cache_by_lock_and_interpreter(self):
        with patch.object(sdk_cache.shutil, "which", return_value="/fixture/python3.13") as which:
            first = sdk_cache.sdk_cache_dir()
            self.assertEqual(first.parent, self.cache)
            self.assertEqual(first, sdk_cache.sdk_cache_dir())
            self.lock.write_text("mcp==2.0\n")
            after_lock = sdk_cache.sdk_cache_dir()
            self.assertNotEqual(first, after_lock)
            os.environ["PORTABLE_TEST_PYTHON"] = "/fixture/python3.14"
            which.return_value = "/fixture/python3.14"
            self.assertNotEqual(after_lock, sdk_cache.sdk_cache_dir())
            which.assert_called_with("/fixture/python3.14")

    def test_same_interpreter_path_with_different_runtime_gets_new_cache(self):
        with patch.object(sdk_cache.shutil, "which", return_value="/fixture/python3"):
            first = sdk_cache.sdk_cache_dir()
            sdk_cache.subprocess.check_output.return_value = b"fixture-runtime-different-architecture"
            self.assertNotEqual(first, sdk_cache.sdk_cache_dir())

    def test_relative_override_is_rejected(self):
        with patch.dict(os.environ, {"PORTABLE_TEST_CACHE_DIR": "cache"}):
            with self.assertRaisesRegex(SystemExit, "absolute"):
                sdk_cache.sdk_cache_dir()

    def test_symlink_into_checkout_is_rejected(self):
        self.cache.symlink_to(self.checkout, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, "outside"):
            sdk_cache.sdk_cache_dir()
