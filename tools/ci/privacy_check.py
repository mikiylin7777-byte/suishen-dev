#!/usr/bin/env python3
"""合改动时查个人信息：按朋友版打一份到临时目录，逐个文件（含运行包里的模块）找名单里的词，找到就不通过。

名单在 tools/ci/privacy_markers.txt（一行一个词，# 开头是说明）；CI 从基线分支取名单，改动自己删词躲不过这一次。
用法：python3 tools/ci/privacy_check.py [--markers 名单文件]
来由：见 DECISIONS 2026-11-17（原来只在打包时查，注释里写了名字要到打包才发现）。
"""
import argparse,sys,tempfile
from pathlib import Path
SRC=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(SRC))
from portable.core import Blocked
from portable.distribution import build,personal_hits

def markers(path):
    return [l.strip() for l in Path(path).read_text(encoding='utf-8').splitlines() if l.strip() and not l.strip().startswith('#')]

def check(src,words):
    with tempfile.TemporaryDirectory() as d:
        root=Path(d).resolve()/'随身开发'
        build(src,root,'full')
        return personal_hits(root,words,local=False)

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--markers',default=str(SRC/'tools/ci/privacy_markers.txt'));args=a.parse_args()
    words=markers(args.markers)
    if not words:print('个人信息检查：名单是空的，不通过');sys.exit(1)
    try:hits=check(SRC,words)
    except Blocked as e:print('个人信息检查：打包失败：'+str(e));sys.exit(1)
    if hits:
        print('个人信息检查不通过，发出去的文件里有名单上的词（代码注释写「见 DECISIONS 日期」，不写人名）：')
        for h in hits:print('  '+h)
        sys.exit(1)
    print(f'个人信息检查通过：{len(words)} 个词，发出去的文件里都没有')
