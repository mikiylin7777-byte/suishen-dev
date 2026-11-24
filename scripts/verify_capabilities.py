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

SKIP_FLAG='--skip-client-checks'
SKIP_REASON='显式开关：不要求 codex/claude 已加载（CI 或无客户端环境）'

def augment_client_path():
    """开发机上的 claude/codex 常在 ~/.local/bin，调用方 PATH 未必带上。"""
    home=Path.home()
    extras=[home/'.local'/'bin', home/'bin', Path('/opt/homebrew/bin'), Path('/usr/local/bin')]
    current=os.environ.get('PATH','')
    parts=current.split(os.pathsep) if current else []
    prefix=[str(path) for path in extras if path.is_dir() and str(path) not in parts]
    if prefix:os.environ['PATH']=os.pathsep.join(prefix+parts)

def skip_client_load(argv):
    if SKIP_FLAG in argv:return True
    return os.environ.get('PORTABLE_SKIP_CLIENT_CHECKS','').strip().lower() in ('1','true','yes')

def loaded_clients(checks):
    return [name for name,value in checks.items() if value.get('loaded')]

def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    skip=skip_client_load(argv)
    augment_client_path()
    sdk_override = os.environ.get('PORTABLE_SDK_PYTHON')
    sdk = Path(sdk_override) if sdk_override else sdk_cache_dir() / 'integration-sdk/bin/python'
    if not sdk.is_file():raise SystemExit('BLOCKED: select PORTABLE_SDK_PYTHON from a locally rebuilt SDK environment')
    with tempfile.TemporaryDirectory(prefix='portable-capabilities-') as tmp:
        store=Store(Path(tmp).resolve()/'app')
        try:
            result=check_clients(store,'verification',run_clients=True);result['sdk']=check_sdk(store,'verification-sdk',sdk)
            if skip:
                result['client_load']='skipped'
                result['client_load_reason']=SKIP_REASON
            else:
                result['client_load']='required'
            print(json.dumps(result,ensure_ascii=False))
            assert result['stdio'] and result['sdk'].get('sdk_stdio'), 'actual SDK stdio failed'
            if skip:
                print(SKIP_REASON, file=sys.stderr)
                return
            loaded=loaded_clients(result['client_checks'])
            if not loaded:
                raise SystemExit('本机 codex 与 claude 都没有加载成功：'+json.dumps(result['client_checks'],ensure_ascii=False)+'。CI 或无客户端环境请显式传入 --skip-client-checks')
        finally:store.db.close()

if __name__=='__main__':main()
