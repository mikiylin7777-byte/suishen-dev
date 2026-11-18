from .core import cooperative
import json
import os
from pathlib import Path
import stat
import time
import uuid
from .core import Blocked, PROTOCOL, canonical

def private_config(path):
    path=Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077 or path.stat().st_uid!=os.getuid():
        raise Blocked('机器人设置必须为本人所有的600私密文件')
    value=json.loads(path.read_text())
    if not value.get('owners'):
        raise Blocked('未设置本人身份白名单')
    return value

@cooperative
def event(store,config,value):
    from .privacy import sanitize
    channel=value['channel'];identity=value['identity'];mid=value['id'];body=sanitize(value['body'])
    owner=config.get('owners',{}).get(channel,{}).get(identity)
    if channel not in ('feishu','wecom') or not owner or not mid:
        raise Blocked('机器人身份未绑定')
    from .automation import state
    if state(store)['paused']:raise Blocked('运行器已停写，机器人请求未接收')
    with store.db:
        cur=store.db.execute('INSERT OR IGNORE INTO messages VALUES (?,?)',(channel,mid))
        if not cur.rowcount:return {'duplicate':True}
        store.db.execute('INSERT INTO records VALUES (?,?,?,?)',(str(uuid.uuid4()),'bot_received',canonical({'channel':channel,'id':mid,'owner':owner,'body':body}),time.time()))
        if isinstance(body,str) and body.strip().startswith('批准 '):
            aid=body.strip().split(' ',1)[1].strip()
            approval=store.db.execute('SELECT binding FROM approvals WHERE id=?',(aid,)).fetchone()
            if not approval:raise Blocked('批准编号不存在')
            body={'command':'approve','id':aid,'binding':json.loads(approval[0])}
        if isinstance(body,dict) and body.get('command')=='approve':
            binding=body['binding']
            context=store.get('bot-approval:'+body['id'])
            if context and context!=approval_context(store):raise Blocked('暂停或授权条件已变化，批准失效')
            if store.get('bot-target:'+body['id'])!=approval_target(store,binding):raise Blocked('任务轮次或版本已变化，批准失效')
            if binding.get('owner')!=owner:raise Blocked('跨渠道批准人不匹配')
            cur=store.db.execute('UPDATE approvals SET consumed=? WHERE id=? AND binding=? AND expires>? AND consumed IS NULL',(time.time(),body['id'],canonical(binding),time.time()))
            if cur.rowcount!=1:raise Blocked('批准已过期/消费或绑定不符')
            store.put('approval-decision:'+body['id'],{'state':'pending','binding':binding,'device':store.device})
            return {'approved':True,'external_action_executed':False,'reply':'批准已绑定原任务、动作、目标、范围与版本，已一次消费；尚未执行外部动作。'}
        store.db.execute('INSERT INTO records VALUES (?,?,?,?)',(str(uuid.uuid4()),'inbox',canonical({'channel':channel,'owner':owner,'body':body,'execution':False}),time.time()))
    if isinstance(body,str):
        text=body.strip()
        if text in ('项目','查看项目','下一步'):
            from .tools import project_choices
            return {'received':True,'execution':False,'reply':canonical(project_choices(store))}
        if text.startswith('选择项目 '):
            from .tools import project_choices
            selected=text.split(' ',1)[1].strip()
            result=project_choices(store,selected)
            rid=store.get('bot-requirement:'+owner)
            if rid:
                from .workflows import status,link_project
                current=status(store,rid)
                link_project(store,rid,selected,current['revision'])
            return {'received':True,'execution':False,'reply':canonical(result)}
        if text.startswith(('取消任务 ','继续任务 ')):
            from . import workflows
            reference=text.split(' ',1)[1].strip();request=None
            for record in store.db.execute("SELECT id,body FROM records WHERE kind='original_intent' ORDER BY created DESC"):
                original=json.loads(record['body']);queued=store.db.execute('SELECT reference FROM outbox WHERE id=?',(original['queue_key'],)).fetchone()
                if record['id']==reference or (queued and queued[0]==reference):request=record['id'];break
            if not request:raise Blocked('此任务不是本产品当前身份的原请求')
            if text.startswith('取消任务 '):
                result=workflows.request_cancel(store,request)
                return {'received':True,'execution':False,'reply':'取消意图已持久化，后台正按原任务对账；未确认停稳前不会声称已停止。'}
            original,_=workflows.original_request(store,request);current=workflows.status(store,original['requirement']);workflows.answer(store,original['requirement'],'implement',True,current['revision'])
            return {'received':True,'execution':False,'reply':'继续意图已保存；后台将重新工程审阅当前MVP和验收后，经原任务或正式新轮次继续。'}
        if text.startswith('查询任务 '):
            from .team import Team,transport
            task=text.split(' ',1)[1].strip()
            result=Team(store,transport(store.get('team_config',{}))).action('get_task',task)
            # Return state metadata, never raw logs/spec/session content.
            return {'received':True,'execution':False,'reply':canonical({k:result.get(k) for k in ('task_id','status','run_id')})}
        from . import workflows
        key='bot-requirement:'+owner
        rid=store.get(key)
        if not rid or text.startswith(('新需求 ','请实施：','请实施:')):
            known={}
            fields={'用户':'user','问题':'problem','核心流程':'flow','首版范围':'scope','验收':'acceptance'}
            for line in text.replace('请实施：','',1).replace('请实施:','',1).splitlines():
                parts=line.replace('：',':',1).split(':',1)
                if len(parts)==2 and parts[0].strip() in fields:known[fields[parts[0].strip()]]=parts[1].strip()
            rid=workflows.start(store,store.get('selected_project'),text,known)
            store.put(key,rid)
            if text.startswith(('请实施：','请实施:')):workflows.answer(store,rid,'implement',True)
        else:
            current=workflows.status(store,rid)
            if current['question']:workflows.answer(store,rid,current['question']['field'],text,current['revision'])
            elif text in ('实施','请实施','确认实施'):workflows.answer(store,rid,'implement',True,current['revision'])
            else:workflows.answer(store,rid,'extra',text,current['revision'])
        current=workflows.status(store,rid)
        question=current['question']
        reply=question['text'] if question else '需求已保存；请回复“实施”确认首版意图。' if not current['intent'] else '实施意图已保存，当前未派工；工程Agent可从待审需求读取并审阅真实项目，原请求提交后由后台持续跟进。'
        if not current['project']:reply='需求已保存；先发送“选择项目 项目ID”关联项目，再继续访谈。'
        return {'received':True,'execution':False,'requirement':rid,'reply':reply}
    return {'received':True,'execution':False}

@cooperative
def notification(store,config,value):
    if value.get('kind') not in PROTOCOL['notification_kinds']:
        raise Blocked('仅允许完成、批准、无法解决或范围变化通知')
    channel=value['channel'];target=value['target'];nid=value['id']
    if target not in config.get('owners',{}).get(channel,{}) or not nid:
        raise Blocked('通知目标不在本人白名单')
    phase=value.get('phase','prepare')
    key='bot-notification:'+channel+':'+nid
    from .core import digest
    fingerprint=digest(canonical({k:value.get(k) for k in ('kind','target','text')}).encode())
    previous=store.get(key,{})
    if phase in ('ack','status'):
        if not previous or previous.get('fingerprint')!=fingerprint:raise Blocked('通知回执绑定不符')
        if phase=='ack':
            previous['state']='acked';store.put(key,previous)
        return {'state':previous['state'],'send_allowed':False}
    if phase!='prepare':raise Blocked('未知通知阶段')
    # Durable send-before-network. Uncertain sends are never automatically replayed.
    with store.db:
        cur=store.db.execute('INSERT OR IGNORE INTO messages VALUES (?,?)',('notification:'+channel,nid))
    if not cur.rowcount:raise Blocked('通知已发送或结果未知，禁止重放')
    store.put(key,{'fingerprint':fingerprint,'state':'unknown'})
    return {'send_allowed':True}


def approval_context(store):
    from .automation import state
    from .core import digest
    return digest(canonical({'device':store.device,'host':store.get('host_binding'),
        'execution':store.get('execution'),
        'team_config':store.get('team_config',{}),
        'paused':state(store)['paused']}).encode())

@cooperative
def issue_approval(store,binding):
    """Robot approvals additionally freeze local authorization/pause conditions."""
    from .automation import state
    if state(store)['paused']:raise Blocked('已暂停，不创建可用批准')
    snapshot=approval_target(store,binding)
    aid=store.approval(binding)
    store.put('bot-target:'+aid,snapshot)
    store.put('bot-approval:'+aid,approval_context(store))
    from .automation import notice,save
    value=state(store);notice(value,'approval',aid,{'task':binding['task']});save(store,value)
    return aid


def approval_target(store,binding,call=None):
    from .team import transport
    call=call or transport(store.get('team_config',{}))
    value=call('get_task',{'task_id':binding['task']})
    if value.get('task_id')!=binding['task'] or not value.get('run_id') or value.get('status') in ('cancelled','interrupted') or binding['version']!=value['run_id']:raise Blocked('批准必须绑定当前任务run版本')
    result={k:value.get(k) for k in ('task_id','run_id','updated','status','verify','workspace')}
    for row in store.db.execute('SELECT * FROM outbox WHERE reference=?',(binding['task'],)):
        request=json.loads(row['body']);rid=request.get('mvp',{}).get('requirement_id')
        if rid:
            from .team import require_readiness,request_scope_valid
            scope_binding=require_readiness(store,request.get('workspace'),project=request.get('project'))
            request_scope_valid(store,row,request,scope_binding)
            result['project_binding']={k:scope_binding[k] for k in ('project','epoch','workspace','fingerprint')}
            from .workflows import status
            scope=status(store,rid)
            if not scope['intent']:raise Blocked('需求已撤回，旧范围不能批准')
            result['requirement_revision']=scope['execution_revision'];break
    return result

@cooperative
def approval_claim(store,aid,identity):
    # One durable claim before handing authority to the original action consumer.
    # A crash is unknown, not a second approval. No deployment/data API here.
    decision=store.get('approval-decision:'+aid)
    if not decision or decision['state']!='pending' or decision['device']!=store.device:raise Blocked('没有可用的一次性批准决定')
    row=store.db.execute('SELECT expires FROM approvals WHERE id=?',(aid,)).fetchone()
    if not row or row[0]<=time.time() or store.get('bot-approval:'+aid)!=approval_context(store) or store.get('bot-target:'+aid)!=approval_target(store,decision['binding']):raise Blocked('批准已过期或任务条件变化')
    decision.update(state='claimed',claimed_by=identity,claimed_at=time.time());store.put('approval-decision:'+aid,decision)
    return {'approval':aid,'binding':decision['binding'],'claimant':identity,'single_use':True,'external_action_executed':False}

@cooperative
def approval_execute_checkpoint(store,aid,identity):
    # Concrete safe consumer; exact checkpoint payload is part of approved scope.
    decision=store.get('approval-decision:'+aid,{})
    binding=decision.get('binding',{})
    if binding.get('action')!='record_checkpoint' or not isinstance(binding.get('scope'),dict):raise Blocked('此入口仅执行批准的本地检查点；其他动作须原动作消费者及其安全门禁')
    from .core import PROTOCOL
    if not set(PROTOCOL['checkpoint_required'])<=set(binding['scope']):raise Blocked('批准的检查点不完整')
    claim=approval_claim(store,aid,identity)
    record=store.record('checkpoint',binding['scope'])
    value=store.get('approval-decision:'+aid);value.update(state='executed',record=record);store.put('approval-decision:'+aid,value)
    return {**claim,'record':record,'local_action_executed':True}


def setup_private(path,value):
    """Noninteractive private wizard: caller supplies settings over stdin, never argv."""
    from .core import atomic
    path=Path(path).absolute()
    if path.exists() or path.is_symlink():raise Blocked('私密设置已存在，禁止覆盖')
    for parent in path.parents:
        if parent.is_symlink():raise Blocked('私密设置父目录不能为链接')
    channels=[c for c in ('feishu','wecom') if value.get('owners',{}).get(c)]
    if not channels or any(not identity for c in channels for identity in value['owners'][c]):raise Blocked('至少选择一个渠道并填写本人白名单')
    owners=[set(value['owners'][c].values()) for c in channels]
    if any(v!=owners[0] for v in owners) or not all(owners[0]):raise Blocked('渠道必须绑定同一本人身份')
    required={'feishu':['appId','appSecret'],'wecom':['botId','secret']}
    if any(not value.get(c,{}).get(k) for c in channels for k in required[c]):raise Blocked('缺官方双向应用设置')
    # Exclusive create avoids overwriting a concurrently supplied private file.
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as handle:
        handle.write(canonical(value));handle.flush();os.fsync(handle.fileno())
    return {'created':True,'mode':'600','secret_output':False}


def diagnose(path):
    value=private_config(path)
    observations={}
    root=value.get('root')
    if root:
        import sqlite3
        dbpath=Path(root)/'数据/state.sqlite'
        if dbpath.is_file() and not dbpath.is_symlink():
            db=sqlite3.connect(dbpath.resolve().as_uri()+'?mode=ro',uri=True)
            try:
                for channel in ('feishu','wecom'):
                    row=db.execute('SELECT value FROM meta WHERE key=?',('bot-connection:'+channel,)).fetchone()
                    if row:observations[channel]=json.loads(row[0])
            finally:db.close()
    return {'private_config':True,'enabled':value.get('enabled') is True,
        'channels':{c:{'identity_bound':bool(value.get('owners',{}).get(c)),
            'credentials_present':all(bool(value.get(c,{}).get(k)) for k in keys),
            'transport':'official-sdk-websocket','connection_observation':observations.get(c,{'status':'not_checked'})}
            for c,keys in [('feishu',['appId','appSecret']),('wecom',['botId','secret'])]},'secret_output':False}


def wizard(store,path=None):
    import getpass,sys
    if not sys.stdin.isatty():raise Blocked('机器人设置需要本人私密终端输入；无人值守不读取秘密')
    path=Path(path or store.root/'数据/私密/bots.json')
    if path.exists():path=path.with_name('bots-'+uuid.uuid4().hex+'.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    print('机器人可选；保存为停用状态，不连接网络。')
    owner=input('本机本人称呼：').strip()
    value={'enabled':False,'root':str(store.root),'python':sys.executable,'bridge':str(store.root/'工具/adapters/bots/bridge.py'),'owners':{},'feishu':{},'wecom':{}}
    for channel,label,fields in [('feishu','飞书',['appId','appSecret']),('wecom','企业微信',['botId','secret'])]:
        if input('配置'+label+'？输入 yes，回车跳过：').strip().lower()!='yes':continue
        identity=input(label+'本人身份ID：').strip()
        value['owners'][channel]={identity:owner}
        for field in fields:value[channel][field]=getpass.getpass(label+' '+field+'（隐藏输入）：').strip()
    result=setup_private(path,value)
    store.put('bots_private_path',str(path))
    return result


def register(subparser):
    p=subparser.add_parser('bots-private');p.add_argument('action',choices=['setup','diagnose']);p.add_argument('--config',required=True);p.set_defaults(bots_run=run);return p

def run(args):
    import sys
    return setup_private(args.config,json.load(sys.stdin)) if args.action=='setup' else diagnose(args.config)


def main(argv=None):
    import argparse
    p=argparse.ArgumentParser();register(p.add_subparsers(required=True))
    try:print(canonical(run(p.parse_args(argv))));return 0
    except Exception:print('{"status":"BLOCKED","reason":"私密设置未确认，未输出敏感详情"}');return 2

if __name__=='__main__':raise SystemExit(main())
