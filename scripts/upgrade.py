#!/usr/bin/env python3
"""Run with reviewed 0.1.3 source; does not need an SDK in its own interpreter."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from portable.core import Blocked,canonical
from portable.distribution import atomic_upgrade
p=argparse.ArgumentParser();p.add_argument('target');p.add_argument('candidate');p.add_argument('backup');a=p.parse_args()
try:print(canonical(atomic_upgrade(a.target,a.candidate,a.backup)))
except (Blocked,OSError,ValueError) as e:
    print(canonical({'status':'BLOCKED','reason':str(e)}),file=sys.stderr);sys.exit(2)
