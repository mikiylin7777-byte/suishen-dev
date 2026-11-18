#!/usr/bin/env python3
"""Host acceptance with the actual archive; isolated HOME, launchd simulated."""
import argparse
import os
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('archive',type=Path,help='真实朋友版0.2.31归档ZIP，摘要必须匹配')
a=p.parse_args()
os.environ['PORTABLE_OLD_FRIEND_ARCHIVE']=str(a.archive.resolve(strict=True))
print('真实旧包→当前源码候选；隔离HOME；launchd为审计替身，不代表真实后台启动。')
r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName('tests.test_existing_friend_upgrade'))
sys.exit(not r.wasSuccessful())
