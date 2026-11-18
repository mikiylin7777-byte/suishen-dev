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
