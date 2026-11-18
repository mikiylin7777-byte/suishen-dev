#!/usr/bin/env python3
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from portable.distribution import build
from portable.core import Blocked,canonical
p=argparse.ArgumentParser();p.add_argument('target');a=p.parse_args()
try:
    print(canonical(build(Path(__file__).resolve().parents[1],a.target)))
except (Blocked,OSError) as e:
    print(canonical({'status':'BLOCKED','reason':str(e)}),file=sys.stderr);sys.exit(2)
