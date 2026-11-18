#!/usr/bin/env python3
"""Dedicated dual-channel bridge, compatible with the original bridge envelopes."""
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
if len(sys.argv)>1:sys.path.insert(0,str(Path(sys.argv[1])/'工具/portable.pyz'))
from portable.core import Store,canonical
from portable.bots import event,notification,private_config
from portable.automation import state

def pending(store,cfg,channel):
    result=[]
    if state(store)['paused']:return result
    for key,item in state(store)['events'].items():
        for target in cfg.get('owners',{}).get(channel,{}):
            nid=key+':'+target
            if store.get('bot-notification:'+channel+':'+nid):continue
            # All outbound notifications use a local fixed label, never raw task output.
            labels={'completed':'已完成','approval':'需要批准','blocked':'需要关注','scope_changed':'范围变化'}
            if item['kind'] not in labels:continue
            text=labels[item['kind']]+'；请在本机查看关联指令。'
            if item['kind']=='approval':
                row=store.db.execute('SELECT binding,expires,consumed FROM approvals WHERE id=?',(item['reference'],)).fetchone()
                if not row or row['consumed'] is not None or row['expires']<=time.time():continue
                from portable.privacy import sanitize
                binding=sanitize(json.loads(row['binding']))
                text='需要批准 '+item['reference']+'\n'+canonical({k:binding.get(k) for k in ('task','action','target','scope','version')})+'\n回复：批准 '+item['reference']+'；不代表已执行外部动作。'
            result.append({'id':nid,'channel':channel,'target':target,'kind':item['kind'],
                'text':text})
    return result

def main():
    store=None
    try:
        req=json.load(sys.stdin);cfg=private_config(sys.argv[2]);store=Store(sys.argv[1])
        operation=req['operation'];value=req['value']
        if operation=='pending':result=pending(store,cfg,value['channel'])
        elif operation=='connection':
            channel=value['channel'];status=value['status']
            if channel not in ('feishu','wecom') or status not in ('connecting','authenticated','disconnected','reconnecting','connection_error'):raise ValueError('invalid connection observation')
            key='bot-connection:'+channel
            if store.get(key,{}).get('status')!=status:store.put(key,{'status':status,'at':time.time()})
            result={'recorded':True}
        elif operation=='notification':result=notification(store,cfg,value)
        elif operation=='event':result=event(store,cfg,value)
        else:raise ValueError('unsupported')
        print(canonical(result));return 0
    except Exception:
        print('{"status":"BLOCKED","reason":"机器人桥接未确认"}',file=sys.stderr);return 2
    finally:
        if store:store.db.close()

if __name__=='__main__':raise SystemExit(main())
