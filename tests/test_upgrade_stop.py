"""升级前停后台：bootout 后进程晚几秒才退出，不应误判为停止失败。"""
import os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from portable import upgrade_channel
from portable.core import Blocked
from portable.supervisor import identity


class StopWaitTest(unittest.TestCase):
    def setUp(self):
        d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup);self.home=Path(d.name)
        self.root=self.home/'随身开发';self.root.mkdir()
        agents=self.home/'Library/LaunchAgents';agents.mkdir(parents=True)
        (agents/(identity(self.root)+'.plist')).write_text('x')

    def run_stop(self,print_codes):
        calls=iter(print_codes)
        def fake(argv,**kw):
            return SimpleNamespace(returncode=next(calls) if argv[1]=='print' else 0)
        with patch.object(upgrade_channel.Path,'home',return_value=self.home),patch.object(upgrade_channel.sys,'platform','darwin'),\
             patch.object(upgrade_channel.subprocess,'run',side_effect=fake),patch.object(upgrade_channel.time,'sleep') as slept:
            result=upgrade_channel._launch(self.root,'stop')
        return result,slept.call_count

    def test_waits_until_service_really_exits(self):
        result,slept=self.run_stop([0,0,0,113])
        self.assertTrue(result);self.assertEqual(slept,3)

    def test_still_running_after_30_seconds_blocks(self):
        with self.assertRaises(Blocked) as e:self.run_stop([0]*31)
        self.assertIn('本次没有升级',str(e.exception))


if __name__=='__main__':unittest.main()


class RemoteAndZipUpgradeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import hashlib, io, zipfile
        from scripts import package_friend
        cls.pkg_dir = tempfile.TemporaryDirectory()
        cls.zip_path = Path(cls.pkg_dir.name) / 'suishen-dev-new.zip'
        package_friend.package(cls.zip_path, markers=['qzxowner9'], repo='qzxowner9-x/suishen-dev')
        cls.zip_bytes = cls.zip_path.read_bytes()
        cls.zip_sha = hashlib.sha256(cls.zip_bytes).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.pkg_dir.cleanup()

    def setUp(self):
        import zipfile
        from portable.core import VERSION
        self.ver = VERSION
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        self.home = Path(d.name).resolve()
        env = patch.dict(os.environ, {'HOME': str(self.home), 'PORTABLE_HOME': str(self.home)})
        env.start(); self.addCleanup(env.stop)
        fixture = Path(__file__).resolve().parents[1] / 'tests/fixtures/friend-0.2.31.zip'
        with zipfile.ZipFile(fixture) as z:
            z.extractall(self.home)
        self.root = self.home / '随身开发'

    def test_repo_in_manifest_passes_personal_hits_only_in_manifest(self):
        import json, zipfile
        from portable import distribution
        with zipfile.ZipFile(self.zip_path) as z:
            manifest = json.loads(z.read('随身开发/发行.json'))
        self.assertEqual(manifest.get('repo'), 'qzxowner9-x/suishen-dev')
        other = self.home / 'bad'
        other.mkdir()
        (other / '发行.json').write_text('{"repo":"qzxowner9-x/suishen-dev"}')
        (other / 'note.md').write_text('hello qzxowner9')
        self.assertEqual(distribution.personal_hits(other, ['qzxowner9'], local=False, repo='qzxowner9-x/suishen-dev'), ['note.md：qzxowner9'])

    def test_apply_from_local_zip_and_sha256_verification(self):
        import json
        with self.assertRaisesRegex(Blocked, 'SHA256 校验失败'):
            upgrade_channel.apply_target(self.root, str(self.zip_path), sha256='0' * 64, launchctl=lambda a, p: True)
        self.assertEqual(json.loads((self.root / '发行.json').read_text())['version'], '0.2.31')
        out = upgrade_channel.apply_target(self.root, str(self.zip_path), sha256=self.zip_sha, launchctl=lambda a, p: True)
        self.assertEqual(out['自检版本'], self.ver)
        self.assertEqual(out['sha256'], self.zip_sha)

    def test_check_and_apply_from_github_release_remembers_repo(self):
        import io
        release = {
            'tag_name': 'v' + self.ver,
            'body': f'三处修复。\nSHA256: `{self.zip_sha}`',
            'assets': [{'name': f'suishen-dev-{self.ver}.zip', 'browser_download_url': 'https://github.com/qzxowner9-x/suishen-dev/releases/download/v1/pkg.zip'}],
        }
        info = upgrade_channel.check_remote(self.root, 'qzxowner9-x/suishen-dev', fetch_json=lambda slug: release)
        self.assertTrue(info['有新版'])
        self.assertEqual(info['sha256'], self.zip_sha)
        # 记住仓库后，不传 repo 也能直接升级
        out = upgrade_channel.apply_target(
            self.root, launchctl=lambda a, p: True,
            fetch_json=lambda slug: release,
            urlopen=lambda req, timeout=120: io.BytesIO(self.zip_bytes),
        )
        self.assertEqual(out['自检版本'], self.ver)
        self.assertEqual(out['来源'], 'qzxowner9-x/suishen-dev')
        # 再升一次会提示已经是最新版
        with self.assertRaisesRegex(Blocked, '不用升级'):
            upgrade_channel.apply_target(self.root, launchctl=lambda a, p: True, fetch_json=lambda slug: release)

    def test_rejects_unsafe_zip_and_non_https_url(self):
        import zipfile
        bad = self.home / 'evil.zip'
        with zipfile.ZipFile(bad, 'w') as z:
            z.writestr('../escape.txt', 'x')
        with self.assertRaisesRegex(Blocked, '不安全路径'):
            upgrade_channel._extract_zip(bad, self.home / 'out')
        with self.assertRaisesRegex(Blocked, 'https://'):
            upgrade_channel._download_zip('http://example.com/a.zip', self.home / 'a.zip')
