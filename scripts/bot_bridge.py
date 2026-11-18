#!/usr/bin/env python3
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
if len(sys.argv)>1:
    sys.path.insert(0,str(Path(sys.argv[1])/'工具/portable.pyz'))
from portable.core import Blocked, Store, canonical
from portable.bots import event,notification,private_config
try:
    request=json.load(sys.stdin)
    cfg=private_config(sys.argv[2])
    s=Store(sys.argv[1])
    result=(notification if request.get('operation')=='notification' else event)(s,cfg,request['value'])
    print(canonical(result))
except Exception:
    print('{"status":"BLOCKED","reason":"私密机器人请求校验失败"}',file=sys.stderr)
    sys.exit(2)
