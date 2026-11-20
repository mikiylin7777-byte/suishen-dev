#!/usr/bin/env python3
"""Real installed CLI and SDK capability checks; no accounts or live messages."""
import json
import os
from pathlib import Path
import sys
import tempfile
sys.dont_write_bytecode = True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from portable.core import Store
from portable.onboarding import check_clients,check_sdk
from scripts.sdk_cache import sdk_cache_dir

sdk_override = os.environ.get('PORTABLE_SDK_PYTHON')
sdk = Path(sdk_override) if sdk_override else sdk_cache_dir() / 'integration-sdk/bin/python'
if not sdk.is_file():raise SystemExit('BLOCKED: select PORTABLE_SDK_PYTHON from a locally rebuilt SDK environment')
with tempfile.TemporaryDirectory(prefix='portable-capabilities-') as tmp:
    store=Store(Path(tmp).resolve()/'app')
    try:
        result=check_clients(store,'verification',run_clients=True);result['sdk']=check_sdk(store,'verification-sdk',sdk)
        print(json.dumps(result,ensure_ascii=False))
        assert result['stdio'] and result['sdk'].get('sdk_stdio'), 'actual SDK stdio failed'
        for name,value in result['client_checks'].items():
            if value['status']!='not_installed':assert value['loaded'],name+' configuration load failed'
    finally:store.db.close()
