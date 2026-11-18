"""Product-owned reconciler. Team remains the only task scheduler/authority."""
import argparse
import json
import time
from .core import Blocked, Store, canonical, cooperative, digest
from .team import Team, require_readiness, transport

KEY = 'automation:v1'

class Halt(Blocked):
    pass

def state(store):
    return store.get(KEY, {'schema':1,'links':{},'events':{},'paused':False})

def save(store, value):
    store.put(KEY, value)

def notice(value, kind, reference, observation):
    key=digest(canonical([kind,reference,observation]).encode())
    value['events'].setdefault(key, {'kind':kind,'reference':reference,'observation':observation,'delivery':{}})
    return key

@cooperative
def link(store, request, owner):
    row=store.db.execute('SELECT * FROM outbox WHERE id=?',(request,)).fetchone()
    if not row or not owner:raise Blocked('缺原请求或本人授权')
    body=json.loads(row['body'])
    binding=require_readiness(store,body.get('workspace'),project=body.get('project'))
    if body.get('operation') or body.get('device')!=store.device or body.get('binding_epoch')!=binding['epoch']:
        raise Blocked('只绑定当前设备原始实施请求')
    if body.get('workspace')!=binding['workspace'] or not body.get('verification_commands'):
        raise Blocked('请求范围或验收不完整')
    value=state(store)
    if request in value['links']:return {'linked':request,'existing':True}
    value['links'][request]={'owner':owner,'device':store.device,'epoch':binding['epoch'],
        'project':binding['project'],'workspace':binding['workspace'],'request_sha256':digest(row['body'].encode()),
        'stalls':0,'stopped':False,'fresh_verify':False}
    save(store,value)
    store.put('runner:tasks',{})
    return {'linked':request}

@cooperative
def pause(store, paused=True, by='', reason=''):
    """暂停或恢复后台。暂停时记下谁、什么时候、为什么（2026-11-16：关机曾被误记成暂停，备份停了两天没人知道）。"""
    if not paused:
        from .runtime_identity import require_local_runtime
        require_local_runtime(store)
    value=state(store);value['paused']=paused;save(store,value)
    import time as _t
    store.put('runner:pause-reason',{'by':by or '未写明','at':_t.time(),'reason':reason or '未写明'} if paused else {})
    # Invalidates all outstanding robot approvals; resuming does not revive them.
    with store.db:store.db.execute('UPDATE approvals SET expires=0 WHERE consumed IS NULL')
    if not paused:
        store.put('runner:stopped',{})
        store.put('runner:shutdown_requested',{})
    return {'paused':paused,'writes_stopped':bool(paused and store.get('runner:stopped')),'stop_requested':paused}

@cooperative
def resume_supervision(store,request,confirmed,call):
    """Fresh scope review for a retained task; never blindly clear stopped flags."""
    if confirmed is not True:raise Blocked('恢复监督须当前工程Agent明确重新审阅')
    value=state(store);linked=value['links'].get(request)
    row=store.db.execute('SELECT * FROM outbox WHERE id=?',(request,)).fetchone()
    if not row or not linked or linked.get('completed_at') or linked.get('stalls',0)>=3 or not row['reference']:raise Blocked('此请求不可按绑定恢复；须处理实际故障或未提交状态')
    body=json.loads(row['body']);binding=require_readiness(store,body['workspace'],project=body['project'])
    if body['device']!=store.device or digest(row['body'].encode())!=linked['request_sha256']:raise Blocked('原请求设备或内容变化')
    rid=body.get('mvp',{}).get('requirement_id')
    if rid:
        from .workflows import status
        current=status(store,rid);expected=store.get('request-amendment:'+request,{}).get('revision') or body['mvp']['requirement_revision']
        if not current['intent'] or current['execution_revision']!=expected:raise Blocked('需求已撤回或修订，须正式request_adjust，不能恢复旧范围')
    if store.get('request-control:'+request,{}).get('state')=='pending':raise Blocked('原请求仍有待处理范围指令')
    view=call('get_task',{'task_id':row['reference']});candidate=store.get('candidate:engineering:'+request,{})
    if view.get('task_id')!=row['reference'] or view.get('caller')!='portable:'+store.device or view.get('workspace')!=candidate.get('workspace',binding['workspace']) or not view.get('run_id') or view.get('status') not in ('running','queued','awaiting_acceptance','interrupted'):raise Blocked('原任务身份/工作区/状态不可恢复')
    # Resolve uncertain prior operations; supersede only proved unsent old-scope intents.
    for old in store.db.execute('SELECT * FROM outbox WHERE reference=?',(row['reference'],)).fetchall():
        old_body=json.loads(old['body'])
        if not old_body.get('operation') or old_body.get('binding_epoch')==binding['epoch']:continue
        if old_body.get('device')!=store.device or old_body.get('arguments',{}).get('task_id')!=row['reference']:raise Blocked('旧生命周期身份无法核实，保留现场')
        if old['state'] in ('pending','sending','unknown','submitted'):
            if old_body['operation']=='cancel_task':raise Blocked('先对账原取消指令，不能用恢复覆盖取消决定')
            if old['state']=='pending':
                with store.db:store.db.execute("UPDATE outbox SET state='superseded' WHERE id=?",(old['id'],))
            else:
                _,confirmed_old=Team(store,call).resolve_instruction(old)
                if not confirmed_old:raise Blocked('旧生命周期结果仍未知，不创建新指令')
        if linked.get('instruction',{}).get('instruction_id')==old['id']:linked.pop('instruction')
    from .verification import review
    from .team import scope_review_inputs
    scoped_mvp,scoped_commands=scope_review_inputs(store,request,body)
    reviewed=review(store,scoped_mvp,scoped_commands,project=body['project'])
    proof={'device':store.device,'project':binding['project'],'epoch':binding['epoch'],'old_epoch':body['binding_epoch'],'workspace':binding['workspace'],'fingerprint':binding['fingerprint'],'authority':binding.get('authority'),'request_sha256':digest(row['body'].encode()),'task':row['reference'],'observed_run':view['run_id'],'review':reviewed}
    store.put('supervision-reauthorization:'+request,proof)
    linked.update(epoch=binding['epoch'],stopped=False,fresh_verify=False,uncertain=False)
    linked.pop('verified_run',None);save(store,value)
    return {'request':request,'task':row['reference'],'supervision_resumed':True,'new_task_created':False,'review':reviewed}


class Reconciler:
    def __init__(self, store, call):
        self.store=store;self.team=Team(store,call);self.call=call

    @cooperative
    def tick(self):
        store=self.store;value=state(store)
        if value['paused']:return {'paused':True}
        for key, linked in value['links'].items():
            control=store.get('request-control:'+key,{})
            if linked['stopped'] and control.get('state')!='pending':continue
            try:
                linked['uncertain']=False
                try:binding=require_readiness(store,linked['workspace'],project=linked['project'])
                except Blocked:raise Halt('绑定变化') from None
                if any(linked[k]!=v for k,v in {'device':store.device,'epoch':binding['epoch'],
                    'project':binding['project'],'workspace':binding['workspace']}.items()):
                    raise Halt('绑定变化')
                row=store.db.execute('SELECT * FROM outbox WHERE id=?',(key,)).fetchone()
                if not row or digest(row['body'].encode())!=linked['request_sha256']:raise Halt('原请求变化')
                body=json.loads(row['body'])
                from .team import request_scope_valid
                try:request_scope_valid(store,row,body,binding)
                except Blocked:raise Halt('绑定变化') from None
                requirement=body.get('mvp',{}).get('requirement_id')
                amendment=store.get('request-amendment:'+key,{})
                if requirement:
                    from .workflows import status as requirement_status
                    scope=requirement_status(store,requirement)
                    expected_revision=amendment.get('revision') or body['mvp'].get('requirement_revision')
                    if not scope.get('intent') or scope['execution_revision']!=expected_revision or control.get('state')=='pending':
                        if not control:
                            control={'action':'cancel' if not scope.get('intent') else 'scope_review_required','state':'pending','revision':scope['execution_revision'],'requirement':requirement}
                            store.put('request-control:'+key,control)
                        self.control_scope(key,linked,row,control,value)
                        continue
                # Pending approved requests ALWAYS query exact original ID before submit.
                if row['state']=='pending':
                    if body.get('mvp',{}).get('repo_version') or body.get('source_version'):
                        from .workflows import repo_version
                        if repo_version(store,linked['project'])['sha256']!=(body['mvp'].get('repo_version') or body['source_version']):raise Halt('原请求变化')
                    found=self.call('lookup_request',{'idempotency_key':key})
                    if found.get('found') is True and found.get('task_id'):
                        with store.db:store.db.execute("UPDATE outbox SET reference=?,state='submitted' WHERE id=?",(found['task_id'],key))
                    elif found.get('found') is False:
                        self.team.submit(key,linked['workspace'])
                    else:raise Blocked('原请求查询不确定')
                    continue
                if row['state'] in ('unknown','sending'):
                    self.team.submit(key,linked['workspace']) # query only, never resubmit unknown
                    continue
                task=row['reference']
                if not task:raise Halt('缺权威任务引用')
                view=self.call('get_task',{'task_id':task})
                if view.get('task_id')!=task or view.get('caller','portable:'+store.device)!='portable:'+store.device:raise Halt('任务身份不符')
                linked['task']=task
                observation={k:view.get(k) for k in ('status','run_id')}
                observation['verify']={k:(view.get('verify') or {}).get(k) for k in ('passed','commands_total','commands_passed','failed_index','ran_at')}
                linked['observation']=observation
                candidate=store.get('candidate:engineering:'+key,{})
                if view.get('workspace')!=candidate.get('workspace',linked['workspace']):raise Halt('任务目标变化')
                if view.get('status')=='completed':
                    notice(value,'completed',task,{'status':'completed'});linked['completed_at']=time.time();linked['stopped']=True
                    from .candidates import receive
                    reception=receive(store,key)
                    if reception.get('state')=='awaiting_external_owner':
                        # Open the PR right away so the owner never has to move code by hand.
                        from .candidates import publish
                        try:reception={**reception,'publication':publish(store,key)}
                        except Exception as error:reception={**reception,'publication':{'state':'failed','reason':str(error)[:300]}}
                    linked['reception']=reception
                    record_delivery(store,key,{'project':linked['project'],'task':task,'run':view.get('run_id'),'summary':'原首版需求已通过受审验证与战队验收；'+('自管代码已接收' if reception.get('state')=='received' else ('已自动开 PR：'+reception['publication']['pr_url'] if (reception.get('publication') or {}).get('pr_url') else '代码待现有主窗口接收')),'reception':reception,'evidence':{'task':task,'run':view.get('run_id')}})
                    from .workflows import project_overview
                    page=store.root/'知识库/项目'/(linked['project']+'.md')
                    project_overview(store,linked['project'],digest(page.read_bytes()) if page.exists() else digest(b''))
                    continue
                blocking=(view.get('blocking') or {}).get('code')
                if blocking and blocking not in ('QUEUED','VERIFY_FAILED','NO_VERIFY','INTERRUPTED'):raise Halt('任务需人工处理')
                if view.get('pending_action') in ('approve','approval','scope_changed'):raise Halt('任务需人工处理')
                if view.get('status') in ('running','queued'):continue
                if view.get('status') not in ('awaiting_acceptance','interrupted'):raise Halt('任务需人工处理')
                # Recover a crash between Team.new_instruction and saving its local reference.
                if not linked.get('instruction'):
                    for instruction_candidate in store.db.execute('SELECT * FROM outbox WHERE reference=?',(task,)).fetchall():
                        pending=json.loads(instruction_candidate['body'])
                        if instruction_candidate['state'] in ('pending','sending','submitted','unknown') and pending.get('operation') in ('continue_task','reverify_task','accept_task'):
                            if pending.get('device')!=store.device or pending.get('binding_epoch')!=binding['epoch']:raise Halt('绑定变化')
                            linked['instruction']={'instruction_id':instruction_candidate['id'],'operation':pending['operation'],'arguments':pending['arguments']}
                            save(store,value);break
                # Reconcile the SAME durable lifecycle instruction after timeout/reboot.
                if linked.get('instruction'):
                    instruction=linked['instruction']
                    result=self.team.lifecycle(instruction['operation'],instruction['arguments'],instruction['instruction_id'])
                    r=store.db.execute('SELECT state FROM outbox WHERE id=?',(instruction['instruction_id'],)).fetchone()
                    if r[0] not in ('confirmed','terminal'):continue
                    if instruction['operation']=='reverify_task':
                        linked['fresh_verify']=True
                        linked['verified_run']=view.get('run_id')
                    linked.pop('instruction');save(store,value)
                    continue
                verify=view.get('verify') or {}
                if linked.get('verified_run') and linked['verified_run']!=view.get('run_id'):linked['fresh_verify']=False
                if not linked['fresh_verify']:
                    operation='reverify_task';args={'task_id':task}
                elif verify.get('passed') is True and linked.get('verified_run')==view.get('run_id'):
                    # Match all explicit acceptance commands against authoritative evidence.
                    result=self.call('get_task_result',{'task_id':task,'include_log_tail':False,'evidence_limit':10})
                    evidence=result.get('verification',{}).get('commands',[])
                    if isinstance(evidence,dict):evidence=evidence.get('items',[])
                    from pathlib import Path
                    if any(not Path(path).is_file() or digest(Path(path).read_bytes())!=sha for path,sha in candidate.get('verification_harnesses',{}).items()):raise Halt('受审验证harness已变化')
                    expected=amendment.get('commands') or candidate.get('verification_commands',body['verification_commands'])
                    if type(result.get('exit_code')) is not int or result.get('exit_code')!=0 or not view.get('run_id') or result.get('execution_status')!='done' or result.get('task',{}).get('run_id',result.get('run_id'))!=view.get('run_id') or not evidence or len(evidence)!=len(expected) or any(e.get('cmd')!=cmd or e.get('exit_code')!=0 for e,cmd in zip(evidence,expected)):
                        raise Halt('验收证据不完整或版本不符')
                    operation='accept_task';args={'task_id':task}
                else:
                    # Count completed failed verification rounds, never polling ticks.
                    fault=digest(canonical([verify.get('status'),verify.get('failed_index')]).encode())
                    linked['stalls']=linked['stalls']+1 if fault==linked.get('fault') else 1
                    linked['fault']=fault
                    if linked['stalls']>=3:raise Halt('三轮同故障无有效进展')
                    operation='continue_task'
                    args={'task_id':task,'spec':'继续原任务，仅修复原验收未通过项；遵守原范围及所有批准门禁，不部署、不操作真实数据。'}
                    linked['fresh_verify']=False
                instruction=self.team.new_instruction(operation,args)
                linked['instruction']={**instruction,'arguments':args}
                save(store,value) # intent reference committed BEFORE network
                self.team.lifecycle(operation,args,instruction['instruction_id'])
            except Exception as exc:
                # Details from external systems may contain secrets; emit metadata only.
                linked['stopped']=isinstance(exc,Halt)
                linked['uncertain']=not linked['stopped']
                notice(value,'scope_changed' if isinstance(exc,Halt) and str(exc) in ('绑定变化','任务目标变化','原请求变化') else 'blocked',key,{'reason':'需要关注：绑定、查询或验收未确认'})
        save(store,value)
        self.summary(value)
        return {'observed':len(value['links']),'events':len(value['events']),'uncertain':sum(bool(v.get('uncertain')) for v in value['links'].values())}

    def control_scope(self,key,linked,row,control,value):
        s=self.store;linked['stopped']=True
        if control.get('action')=='scope_review_required':
            notice(value,'scope_changed',key,{'reason':'需求已修订，旧范围已停自动复验/验收/接收；等待对原任务审阅调整'})
            return
        task=row['reference']
        if not task:
            if row['state'] in ('pending','withdrawn'):
                with s.db:s.db.execute("UPDATE outbox SET state='withdrawn' WHERE id=?",(key,))
                control.update(state='done',stopped=True,not_submitted=True);s.put('request-control:'+key,control);return
            found=self.call('lookup_request',{'idempotency_key':key})
            if found.get('found') is not True:return
            task=found['task_id']
            with s.db:s.db.execute("UPDATE outbox SET reference=?,state='submitted' WHERE id=?",(task,key))
        view=self.call('get_task',{'task_id':task})
        candidate=s.get('candidate:engineering:'+key,{})
        if view.get('task_id')!=task or view.get('caller','portable:'+s.device)!='portable:'+s.device or view.get('workspace')!=candidate.get('workspace',linked['workspace']):raise Halt('任务目标变化')
        if control.get('new_round_request') and control.get('source_task')!=row['reference']:
            return self.reviewed_new_round(key,linked,row,control,view,candidate,value)
        operation='cancel_task' if control['action']=='cancel' else 'continue_task'
        if operation=='continue_task':
            settled=self.call('task_settled',{'task_id':task})
            if not settled.get('stopped'):return
            if view.get('status') not in ('awaiting_acceptance','interrupted'):
                return self.reviewed_new_round(key,linked,row,control,view,candidate,value)
            from .workflows import repo_fingerprint
            from pathlib import Path
            if not control.get('instruction') and repo_fingerprint(Path(candidate['workspace']))['sha256']!=control['candidate_sha256']:
                control.update(action='scope_review_required',state='pending');s.put('request-control:'+key,control);return
        if not control.get('instruction'):
            for prior in s.db.execute('SELECT * FROM outbox WHERE reference=?',(task,)).fetchall():
                original=json.loads(prior['body'])
                if original.get('operation')==operation and prior['state'] in ('pending','sending','unknown','submitted'):
                    control['instruction']=prior['id'];break
        arguments={'task_id':task} if operation=='cancel_task' else control['arguments']
        if not control.get('instruction'):
            control['instruction']=self.team.new_instruction(operation,arguments)['instruction_id'];s.put('request-control:'+key,control)
        try:self.team.lifecycle(operation,arguments,control['instruction'])
        except Exception:
            current=s.db.execute('SELECT state FROM outbox WHERE id=?',(control['instruction'],)).fetchone()
            if current and current[0] in ('superseded','rejected'):control.pop('instruction',None);s.put('request-control:'+key,control)
            raise
        if operation=='cancel_task':
            settled=self.call('task_settled',{'task_id':task})
            if not settled.get('stopped'):return
            control.update(state='done',stopped=True,status=settled.get('status'));s.put('request-control:'+key,control)
            notice(value,'scope_changed',key,{'reason':'原请求已撤回；正式任务已对账停稳，旧批准与自动接收不继续'})
        else:
            instruction=s.db.execute('SELECT state FROM outbox WHERE id=?',(control['instruction'],)).fetchone()
            if instruction[0] not in ('confirmed','terminal'):return
            updated=self.call('get_task',{'task_id':task})
            with s.db:
                s.put('request-amendment:'+key,{'revision':control['revision'],'review':control['review'],'commands':control['commands'],'run_id':updated.get('run_id'),'mvp':control['mvp']})
                control.update(state='done',continued_run=updated.get('run_id'));s.put('request-control:'+key,control)
                linked.update(stopped=False,fresh_verify=False);linked.pop('instruction',None);linked.pop('verified_run',None)
                save(s,value)

    def reviewed_new_round(self,key,linked,row,control,view,candidate,value):
        s=self.store
        if not control.get('new_round_request') and view.get('status') not in ('completed','cancelled'):
            control.update(state='unsupported_terminal',reason='正式后端当前状态不支持安全新轮次');s.put('request-control:'+key,control);return
        from .workflows import repo_fingerprint
        from pathlib import Path
        request_key=control.get('new_round_request') or 'round:'+digest(canonical([s.device,key,control['revision'],control['review']]).encode())
        found=self.call('lookup_request',{'idempotency_key':request_key})
        if found.get('found') is True:task=found['task_id']
        elif found.get('found') is False:
            if repo_fingerprint(Path(candidate['workspace']))['sha256']!=control['candidate_sha256']:
                control.update(action='scope_review_required',state='pending');s.put('request-control:'+key,control);return
            checkpoint=self.call('export_checkpoint',{'task_id':row['reference']})['checkpoint']
            control['new_round_request']=request_key;control['source_task']=row['reference'];s.put('request-control:'+key,control)
            created=self.call('import_checkpoint',{'checkpoint':checkpoint,'title':control['mvp']['problem'][:100],'spec':control['arguments']['spec'],'workspace':candidate['workspace'],'idempotency_key':request_key,'acceptance_criteria':control['commands'],'review_confirmed':True})
            task=created['task_id']
        else:return
        current=self.call('get_task',{'task_id':task})
        if current.get('task_id')!=task or current.get('caller','portable:'+s.device)!='portable:'+s.device or current.get('workspace')!=candidate['workspace']:raise Halt('新轮次身份/目标不符')
        with s.db:
            rounds=s.get('request-rounds:'+key,[])
            if not any(item.get('task')==task for item in rounds):rounds.append({'source_task':control.get('source_task',row['reference']),'task':task,'revision':control['revision'],'review':control['review'],'idempotency_key':request_key})
            s.put('request-rounds:'+key,rounds)
            s.db.execute("UPDATE outbox SET reference=?,state='submitted' WHERE id=?",(task,key))
            s.put('request-amendment:'+key,{'revision':control['revision'],'review':control['review'],'commands':control['commands'],'run_id':current.get('run_id'),'mvp':control['mvp']})
            control.update(state='done',new_task=task);s.put('request-control:'+key,control)
            linked.update(stopped=False,fresh_verify=False,task=task);linked.pop('instruction',None);linked.pop('verified_run',None)
            save(s,value)

    def summary(self,value):
        home=self.store.root/'知识库/首页.md'
        # Only safe metadata; no raw logs, messages, specs or remotely supplied text.
        projects=self.store.db.execute('SELECT id FROM projects ORDER BY id').fetchall()
        lines=['项目：'+', '.join(p['id'] for p in projects)]
        for ref,item in sorted(value['links'].items()):
            status=item.get('observation',{}).get('status')
            status=status if status in ('running','queued','completed','awaiting_acceptance','interrupted') else '待对账'
            received=item.get('reception',{}).get('state')
            suffix='；代码已接收' if received=='received' else '；待本机私密配置审阅接收' if received=='awaiting_private_reception' else '；待现有主窗口接收' if received=='awaiting_external_owner' else ''
            lines.append('任务 '+digest(ref.encode())[:12]+'：'+status+suffix)
        completed=sorted((v.get('completed_at',0),k) for k,v in value['links'].items() if v.get('completed_at'))
        if completed:lines.append('最近交付：'+digest(completed[-1][1].encode())[:12])
        content='\n'.join(lines)
        text=home.read_text()
        if text.count('<!-- AUTO BEGIN -->')==1 and text.count('<!-- AUTO END -->')==1:
            existing=text.split('<!-- AUTO BEGIN -->',1)[1].split('<!-- AUTO END -->',1)[0].strip('\n')
            if existing==content:return
        self.store.overview(digest(home.read_bytes()),content)

def record_delivery(store,request,body):
    import uuid
    rid=str(uuid.uuid5(uuid.NAMESPACE_URL,'portable-delivery:'+store.device+':'+request))
    with store.db:store.db.execute('INSERT OR IGNORE INTO records VALUES (?,?,?,?)',(rid,'delivery',canonical(body),time.time()))
    return rid


@cooperative
def deliver(store, channel, send, lookup):
    """Persistent intent -> send -> ack. Unknown sends are queried, never resent."""
    value=state(store)
    if value['paused']:return value['events']
    for key,event in value['events'].items():
        current=event['delivery'].get(channel)
        if current=='acked':continue
        if current in ('sending','unknown'):
            try:confirmed=lookup(key) is True
            except Exception:confirmed=False
            event['delivery'][channel]='acked' if confirmed else 'unknown'
            continue
        event['delivery'][channel]='sending';save(store,value)
        try:
            ack=send(key,{'kind':event['kind'],'reference':event['reference']})
            event['delivery'][channel]='acked' if ack is True else 'unknown'
        except Exception:event['delivery'][channel]='unknown'
        save(store,value)
    save(store,value)
    return value['events']

def register(subparser):
    p=subparser.add_parser('automation');p.add_argument('action',choices=['tick','link','pause','resume','status','recover'])
    p.add_argument('--confirm-reviewed',action='store_true');p.add_argument('--request');p.add_argument('--owner');p.set_defaults(automation_run=run)
    return p

def run(args):
    store=Store(args.root)
    try:
        if args.action=='recover':return resume_supervision(store,args.request,args.confirm_reviewed,transport(store.get('team_config',{})))
        if args.action=='link':return link(store,args.request,args.owner)
        if args.action in ('pause','resume'):return pause(store,args.action=='pause',by='automation '+args.action+' 命令',reason='用户或 AI 手动执行')
        if args.action=='status':return state(store)
        return Reconciler(store,transport(store.get('team_config',{}))).tick()
    finally:store.db.close()

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);s=p.add_subparsers(required=True);register(s)
    try:print(canonical(run(p.parse_args(argv))));return 0
    except Exception:print(canonical({'status':'BLOCKED','reason':'自动续办未确认，未扩大授权'}));return 2

if __name__=='__main__':raise SystemExit(main())
