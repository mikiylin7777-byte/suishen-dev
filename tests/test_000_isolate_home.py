"""全量测试最先加载：没指定 PORTABLE_HOME 时，统一指到一个临时目录，所有测试和它们开的子进程都不碰真实家目录。
11-14 事故：test_automation 跑后台每日任务时没隔离家目录，对真实 ~/.upgrade-* 执行了「只留最新 2 份」，0.2.44 起会直接删掉。"""
import atexit
import os
import shutil
import tempfile
import unittest

if not os.environ.get('PORTABLE_HOME'):
    _HOME=tempfile.mkdtemp(prefix='portable-test-home-')
    os.environ['PORTABLE_HOME']=_HOME
    atexit.register(shutil.rmtree,_HOME,True)


class IsolatedHomeTest(unittest.TestCase):
    def test_home_is_not_the_real_one(self):
        from pathlib import Path
        from portable.core import user_home
        self.assertNotEqual(user_home().resolve(),Path.home().resolve())
