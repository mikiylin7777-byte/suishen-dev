#!/usr/bin/env python3
"""Coordinator only: atomic repair of the explicitly designated trial package.
Never relax the normal install/update gate or change backend tasks/metadata.
"""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from portable.core import Store,Blocked,canonical,digest,maintenance
from portable.distribution import atomic_upgrade,ISOLATED_RECOVERY_TARGET
p=argparse.ArgumentParser();p.add_argument('target');p.add_argument('candidate');p.add_argument('backup')
p.add_argument('--expected-device',required=True);p.add_argument('--expected-runtime-sha256',required=True)
a=p.parse_args()
try:
    target=Path(a.target).absolute()
    if ISOLATED_RECOVERY_TARGET is None or target!=ISOLATED_RECOVERY_TARGET:raise Blocked('只允许指定隔离实测副本；实际安装禁止恢复修补')
    with maintenance(target):
        s=Store(target)
        try:
            if s.device!=a.expected_device or digest((target/'工具/portable.pyz').read_bytes())!=a.expected_runtime_sha256:
                raise Blocked('目标身份/运行版本已变化，未修补')
        finally:s.db.close()
        print(canonical(atomic_upgrade(target,a.candidate,a.backup,isolated_recovery=True)))
except (Blocked,OSError,ValueError) as e:
    print(canonical({'status':'BLOCKED','reason':str(e)}),file=sys.stderr);sys.exit(2)
