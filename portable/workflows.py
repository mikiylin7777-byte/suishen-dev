"""Resumable one-question interview; planning is delegated to the current Agent."""
import argparse
import json
from pathlib import Path
import sys
import uuid
from .core import Store, Blocked, PROTOCOL, atomic, canonical, digest, cooperative, lock, safe
from .knowledge import search
from .privacy import sanitize, redact_text

QUESTIONS={'user':'谁会使用这个功能？','problem':'最需要解决什么问题？','flow':'用户从开始到完成会经过哪些步骤？','scope':'首版必须包含什么？','acceptance':'做到什么程度算完成？'}

def load(store, rid):
    row=store.db.execute("SELECT body FROM records WHERE id=? AND kind='requirement'",(rid,)).fetchone()
    if not row:raise Blocked('需求不存在')
    return json.loads(row[0])

def execution_revision(body):
    return digest(canonical({k:v for k,v in body.items() if k!='extras'}).encode())

@cooperative
def start(store, project, idea, known=None):
    if not idea.strip() or (project is not None and not store.db.execute('SELECT id FROM projects WHERE id=?',(project,)).fetchone()):raise Blocked('需要想法及有效项目，未归类可先留空')
    known=known or {}
    if set(known)-set(PROTOCOL['mvp_required']) or any(not isinstance(v,str) for v in known.values()):raise Blocked('已有信息字段无效')
    return store.record('requirement',{'project':project,'idea':redact_text(idea),'answers':sanitize(known),'extras':[],'intent':False,'device':store.device})

@cooperative
def answer(store, rid, field, value, expected=None):
    body=load(store,rid)
    value=sanitize(value)
    if expected is not None and digest(canonical(body).encode())!=expected:raise Blocked('需求已被其他工具更新，请重新读取')
    previous_revision=execution_revision(body)
    if field=='implement':
        if not isinstance(value,bool):raise Blocked('实施意图必须显式布尔值')
        body['intent']=value
        if value:body['device']=store.device
    elif field=='extra':
        if not isinstance(value,str) or not value.strip():raise Blocked('待办不能为空')
        body['extras'].append(value)
    elif field in PROTOCOL['mvp_required'] and isinstance(value,str) and value.strip():body['answers'][field]=value
    else:raise Blocked('回答字段无效')
    with store.db:store.db.execute('UPDATE records SET body=? WHERE id=?',(canonical(body),rid))
    new_revision=execution_revision(body)
    if new_revision!=previous_revision:
        for row in store.db.execute("SELECT id,body FROM records WHERE kind='original_intent'").fetchall():
            original=json.loads(row['body'])
            if original.get('requirement')==rid:
                received=store.get('candidate:engineering:'+original['queue_key'],{}).get('reception',{}).get('state')
                if received=='received':continue
                queued=store.db.execute('SELECT state,reference FROM outbox WHERE id=?',(original['queue_key'],)).fetchone()
                if queued and queued['state']=='pending' and not queued['reference']:
                    with store.db:store.db.execute("UPDATE outbox SET state='withdrawn' WHERE id=?",(original['queue_key'],))
                    continue
                store.put('request-control:'+original['queue_key'],{'action':'cancel' if not body['intent'] else 'scope_review_required','state':'pending','revision':new_revision,'request':row['id'],'requirement':rid})
    return status(store,rid)

def status(store,rid):
    body=load(store,rid);missing=[k for k in PROTOCOL['mvp_required'] if not body['answers'].get(k)]
    from .candidates import pending
    awaiting=pending(store,body['project']) if body['project'] else []
    controls=[store.get('request-control:'+json.loads(r[0])['queue_key'],{}) for r in store.db.execute("SELECT body FROM records WHERE kind='original_intent'") if json.loads(r[0]).get('requirement')==rid]
    adjusting=any(c.get('action')=='scope_review_required' and c.get('state')!='done' for c in controls)
    planning=store.get('planner:'+rid+':'+execution_revision(body),{})
    planning_view={key:planning[key] for key in ('state','task','attempts','last_fault') if key in planning}
    if planning.get('state')=='blocked':planning_view['next']='当前Agent重新engineering_context并planning_resume；保留同一需求与规划任务'
    return {'planning':planning_view,'needs_task_adjustment':adjusting,'pending_reception':awaiting,'id':rid,**body,'revision':digest(canonical(body).encode()),'execution_revision':execution_revision(body),'question':{'field':missing[0],'text':QUESTIONS[missing[0]]} if missing else None,'state':'needs_task_adjustment' if adjusting and body['intent'] else 'awaiting_project_reception' if awaiting else 'awaiting_project' if not body['project'] else 'interview' if missing else 'awaiting_implementation_intent' if not body['intent'] else 'needs_engineer_plan','execution':False,'methods':search(store,body['idea'],body['project'])}

@cooperative
def plan_request(store,rid,purpose='planning'):
    s=status(store,rid)
    if s['state']!='needs_engineer_plan':raise Blocked('先补齐关键问题并明确实施意图')
    result={'requirement':rid,'revision':s['revision'],'project':s['project'],'mvp':s['answers'],'backlog':s['extras'],'purpose':purpose,'methods':s['methods'],'agent_action':'inspect_registered_repo_and_review_verification','instructions':'当前Agent调用engineering_context读取真实repo，再显式engineering_review形成受审原请求，request_submit经既有战队提交。用户无需写shell；素材和验收文字不是验证授权。','execution':False}
    result['reference']=store.record('agent_plan_request',result)
    return result

@cooperative
def reviewed_mvp(store,rid,review_id):
    from .team import require_readiness
    from .verification import resolve
    s=status(store,rid)
    if s['state']!='needs_engineer_plan':raise Blocked('需求尚不可规划')
    binding=require_readiness(store,project=s['project'])
    if binding['project']!=s['project']:raise Blocked('当前工程绑定属于另一项目')
    mvp=dict(s['answers']);resolve(store,mvp,review_id,binding)
    mvp['verification_review']=review_id
    return {'mvp':mvp,'project':s['project'],'dispatch_ready':True,'execution':False}

@cooperative
def project_overview(store,project,expected=None,manual=None):
    if not store.db.execute('SELECT id FROM projects WHERE id=?',(project,)).fetchone():raise Blocked('项目未登记')
    path=safe(store.root,'知识库/项目/'+project+'.md')
    with lock(store.root/'数据/vault.lock'):
        old=path.read_text() if path.exists() else ''
        rev=digest(old.encode())
        rows=[]
        for r in store.db.execute('SELECT id,kind,body FROM records ORDER BY created DESC'):
            b=sanitize(json.loads(r['body']))
            if b.get('project')==project or project in b.get('projects',[]):rows.append({'id':r['id'],'kind':r['kind'],'value':b})
        body={'project':project,'goal':next((r['value'].get('idea') for r in rows if r['kind']=='requirement'),''),'tasks':[r for r in rows if r['kind']=='task_reference'],'backlog':[x for r in rows if r['kind']=='requirement' for x in r['value'].get('extras',[])],'recent_deliveries':[r for r in rows if r['kind'] in ('checkpoint','delivery')][:3],'verification':[r for r in rows if r['kind']=='verification_review'][:3],'entry':'打开随身开发 → 查看项目 / 新建需求 / 素材与方法 / 跨工具续接'}
        begin,end='<!-- AUTO BEGIN -->','<!-- AUTO END -->'
        if expected!=rev:
            conflict=store.root/'任务记录'/('project-conflict-'+str(uuid.uuid4())+'.json')
            atomic(conflict,canonical({'current':old,'proposed':body,'manual':manual,'expected':expected}))
            return {'conflict':True,'revision':rev,'preserved':str(conflict)}
        if begin in old or end in old:
            if old.count(begin)!=1 or old.count(end)!=1 or old.index(begin)>old.index(end):raise Blocked('项目自动区标记异常')
            before,rest=old.split(begin);_,after=rest.split(end)
        else:before=old+'\n';after='\n## 手写区\n'
        if manual is not None:after='\n## 手写区\n'+manual
        lines=['## 当前目标',body['goal'] or '尚未记录（通过随身开发提需求后自动填写；当前进度见上方进度卡）','', '## 当前任务']
        lines.extend('- '+str(r['value'].get('task','待关联任务')) for r in body['tasks'])
        if not body['tasks']:lines.append('暂无已关联任务（不代表原项目没有其他任务）')
        lines+=['','## 待办']+[('- '+x) for x in body['backlog']]
        if not body['backlog']:lines.append('暂无额外待办')
        for title,key in [('最近交付','recent_deliveries'),('验证','verification')]:
            lines+=['','## '+title]
            for r in body[key]:lines.append('- '+str(r['value'].get('goal') or r['value'].get('summary') or '已记录工程验证')+'（记录 '+r['id']+'）')
            if not body[key]:lines.append('暂无记录')
        lines+=['','## 使用入口','打开随身开发 → 查看项目 / 新建需求 / 素材与方法 / 跨工具续接。','项目ID：'+project]
        atomic(path,before+begin+'\n'+'\n'.join(lines)+'\n'+end+after)
        return {'conflict':False,'revision':digest(path.read_bytes()),'path':str(path),'overview':body}

def register(subparser):
    p=subparser.add_parser('workflow');s=p.add_subparsers(dest='workflow_action',required=True)
    a=s.add_parser('binding');a.add_argument('project');a.add_argument('--workspace');a.add_argument('--task');a.add_argument('--run');a.add_argument('--epoch')
    a=s.add_parser('start');a.add_argument('project');a.add_argument('idea')
    a=s.add_parser('status');a.add_argument('id')
    a=s.add_parser('answer');a.add_argument('id');a.add_argument('field');a.add_argument('value',help='JSON value, e.g. true or a quoted string');a.add_argument('--expected')
    a=s.add_parser('plan');a.add_argument('id')
    a=s.add_parser('inspect');a.add_argument('id');a.add_argument('--file',action='append',default=[]);a.add_argument('--workspace');a.add_argument('--task');a.add_argument('--run')
    a=s.add_parser('review');a.add_argument('id');a.add_argument('--inspection',required=True);a.add_argument('--revision',required=True);a.add_argument('--plan-file',required=True);a.add_argument('--commands',required=True);a.add_argument('--confirm',action='store_true')
    a=s.add_parser('submit');a.add_argument('id')
    for action in ('request-status','cancel'):
        a=s.add_parser(action);a.add_argument('id')
    a=s.add_parser('adjust');a.add_argument('id');a.add_argument('--inspection',required=True);a.add_argument('--revision',required=True);a.add_argument('--plan-file',required=True);a.add_argument('--commands',required=True);a.add_argument('--confirm',action='store_true')
    return p

def run(store,args):
    if args.workflow_action=='binding':
        from .project_binding import query
        return query(store,args.project,args.workspace,args.task,args.run,args.epoch)
    if args.workflow_action=='request-status':return request_status(store,args.id)
    if args.workflow_action=='cancel':return request_cancel(store,args.id)
    if args.workflow_action=='adjust':return request_adjust(store,args.id,args.inspection,args.revision,Path(args.plan_file).read_text(),json.loads(Path(args.commands).read_text()),args.confirm,'local-cli:'+store.device)
    if args.workflow_action=='inspect':return engineering_context(store,args.id,'local-cli:'+store.device,args.file,workspace=args.workspace,task=args.task,run=args.run)
    if args.workflow_action=='review':return engineering_review(store,args.id,args.inspection,args.revision,Path(args.plan_file).read_text(),json.loads(Path(args.commands).read_text()),args.confirm,'local-cli:'+store.device)
    if args.workflow_action=='submit':return submit_request(store,args.id)
    if args.workflow_action=='start':return {'id':start(store,args.project,args.idea)}
    if args.workflow_action=='status':return status(store,args.id)
    if args.workflow_action=='answer':return answer(store,args.id,args.field,json.loads(args.value),args.expected)
    return plan_request(store,args.id)

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);register(p.add_subparsers(required=True));args=p.parse_args(argv)
    store=None
    try:
        from .project_binding import ReadOnlyStore
        store=ReadOnlyStore(args.root) if args.workflow_action=='binding' else Store(args.root)
        print(canonical(run(store,args)));return 0
    except (Blocked,ValueError) as e:print(canonical({'status':'BLOCKED','reason':str(e)}));return 2
    finally:
        if store is not None:store.db.close()


# Context-only engineering bridge. No shell execution tool and no new scheduler.
def repo_version(store,project):
    import subprocess
    row=store.db.execute('SELECT path FROM paths WHERE project=? AND device=?',(project,store.device)).fetchone()
    if not row:raise Blocked('项目没有当前设备的真实repo')
    root=Path(row['path']).resolve()
    def git(*args):
        p=subprocess.run(['git','-C',str(root),*args],capture_output=True,timeout=15)
        if p.returncode:raise Blocked('真实repo版本无法核实')
        return p.stdout
    if Path(git('rev-parse','--show-toplevel').decode().strip()).resolve()!=root:raise Blocked('登记目录不是repo根')
    head=git('rev-parse','HEAD').decode().strip()
    paths=sorted(set(git('ls-files','-c','-o','--exclude-standard','-z').decode().rstrip('\x00').split('\x00'))- {''})
    if len(paths)>10000:raise Blocked('repo文件范围过大，请缩小工程边界')
    files={};total=0
    import os,stat
    gitlinks={line.split(b'\t',1)[1].decode():line.split()[1].decode() for line in git('ls-files','--stage','-z').split(b'\0') if line.startswith(b'160000 ')}
    for rel in paths:
        p=root/rel
        if p.is_symlink():
            files[rel]={'symlink':os.readlink(p)}
            continue
        if rel in gitlinks:
            if not p.is_dir() or not (p/'.git').exists():files[rel]={'gitlink':gitlinks[rel],'state':'uninitialized'}
            else:files[rel]={'gitlink':gitlinks[rel],'submodule':repo_fingerprint(p)}
            continue
        p=safe(root,rel)
        if not p.exists():files[rel]='deleted';continue
        if not p.is_file():raise Blocked('repo包含未知特殊文件')
        total+=p.stat().st_size
        import hashlib
        h=hashlib.sha256()
        with p.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
        files[rel]=h.hexdigest()
    return {'workspace':str(root),'head':head,'files':files,'sha256':digest(canonical({'head':head,'files':files}).encode())}

def repo_fingerprint(root):
    # Reuse the exact path-safe/versioned scanner for initialized submodules.
    class View:
        device='submodule'
        class DB:
            def execute(self,*_):return self
            def fetchone(self):return {'path':str(root)}
        db=DB()
    return repo_version(View(),'submodule')


def prepare_engineering_context(store,rid,origin,files=None,*,workspace=None,task=None,run=None,call=None):
    s=status(store,rid)
    from .project_binding import require
    entry=require(store,s['project'],workspace,None if s.get('needs_task_adjustment') else task,None if s.get('needs_task_adjustment') else run,call=call)
    binding={'project':s['project'],'epoch':entry['readiness_epoch']}
    if s['state'] not in ('needs_engineer_plan','needs_task_adjustment') or not s['project'] or binding['project']!=s['project']:raise Blocked('需求、实施意图与工程绑定尚未就绪')
    repo=repo_version(store,s['project']);selected={};candidate_context=None
    if s.get('needs_task_adjustment'):
        for record in store.db.execute("SELECT id,body FROM records WHERE kind='original_intent' ORDER BY created DESC"):
            original=json.loads(record['body']);control=store.get('request-control:'+original['queue_key'],{})
            if original.get('requirement')==rid and control.get('state')=='pending':
                candidate=store.get('candidate:engineering:'+original['queue_key'],{})
                if candidate.get('workspace'):
                    entry=require(store,s['project'],candidate['workspace'],task,run,call=call,candidate_request=original['queue_key'])
                    candidate_context={'request':record['id'],'candidate_request':original['queue_key'],**repo_fingerprint(Path(candidate['workspace']))};break
    read_repo=candidate_context or (repo_fingerprint(Path(workspace)) if workspace is not None else repo)
    # Gate the final directory before opening any selected file.
    entry=require(store,s['project'],read_repo['workspace'],task=task or entry['task'].get('task'),run=run if run is not None else entry['task'].get('run'),call=call,candidate_request=(candidate_context or {}).get('candidate_request'))
    for rel in files or []:
        from .migration import SECRET_NAME
        if SECRET_NAME.search(rel):raise Blocked('本机运行凭据不进入模型工程上下文，请使用合成dev配置或明确provider')
        if rel not in read_repo['files'] or read_repo['files'][rel]=='deleted':raise Blocked('只能读取已核实repo内文件')
        p=safe(Path(read_repo['workspace']),rel)
        if p.stat().st_size>200000 or len(selected)>=20:raise Blocked('工程上下文读取范围过大')
        try:selected[rel]=redact_text(p.read_text())
        except UnicodeError:raise Blocked('所选文件不是可读文本')
    body={'entry_binding':entry,'entry_workspace':read_repo['workspace'],'candidate_context':candidate_context,'requirement':rid,'revision':s['revision'],'execution_revision':s['execution_revision'],'project':s['project'],'epoch':binding['epoch'],'repo':repo,'origin':origin,'mvp':s['answers'],'methods':s['methods'],'read_files':list(selected)}
    return body,selected


@cooperative
def engineering_context(store,rid,origin,files=None,*,workspace=None,task=None,run=None,call=None):
    body,selected=prepare_engineering_context(store,rid,origin,files,workspace=workspace,task=task,run=run,call=call)
    entry=body['entry_binding']
    inspection=store.record('engineering_inspection',body)
    return {'entry_binding':entry,'inspection':inspection,**body,'content':selected,'next':'当前Agent审阅真实repo后调用engineering_review；不要求用户编写命令'}

@cooperative
def engineering_review(store,rid,inspection,revision,plan,commands,confirmed,origin,*,call=None):
    from .team import require_readiness
    from .verification import review
    import time
    if confirmed is not True or not isinstance(plan,str) or not plan.strip():raise Blocked('需要当前Agent明确完成工程规划与验证审阅')
    row=store.db.execute("SELECT body FROM records WHERE id=? AND kind='engineering_inspection'",(inspection,)).fetchone()
    if not row:raise Blocked('缺真实repo检查记录；素材不是工程审阅')
    inspected=json.loads(row[0]);s=status(store,rid)
    from .project_binding import require
    entry_task=inspected.get('entry_binding',{}).get('task',{})
    entry=require(store,s['project'],workspace=inspected.get('entry_workspace'),epoch=inspected.get('epoch'),task=entry_task.get('task'),run=entry_task.get('run'),call=call,candidate_request=(inspected.get('candidate_context') or {}).get('candidate_request'))
    binding=require_readiness(store,project=s['project'])
    slot='original-intent:'+digest(canonical([store.device,rid,s['execution_revision'],binding['epoch'],binding['project']]).encode())
    previous=store.get(slot)
    if inspected.get('execution_revision')!=s['execution_revision'] or not s['intent'] or inspected.get('requirement')!=rid or inspected.get('revision')!=revision:raise Blocked('需求授权/版本已变化，旧审阅只作历史')
    if previous:
        record=store.db.execute("SELECT body FROM records WHERE id=? AND kind='original_intent'",(previous['request'],)).fetchone()
        if not record:raise Blocked('原请求槽位损坏，禁止重复派工')
        return {'request':previous['request'],**json.loads(record[0]),'reused_existing':True,'execution':False,'next':'沿同一原请求查询/续办；调整须走request_adjust而非另建任务'}
    current=repo_version(store,s['project'])
    if s['state']!='needs_engineer_plan' or any(inspected.get(k)!=v for k,v in {'requirement':rid,'revision':revision,'project':s['project'],'epoch':binding['epoch']}.items()) or inspected['repo']['sha256']!=current['sha256']:raise Blocked('需求、repo版本或readiness已变化，重新规划')
    if binding['project']!=s['project']:raise Blocked('目标绑定不匹配')
    mvp={**s['answers'],'engineering_plan':redact_text(plan),'repo_version':current['sha256'],'requirement_id':rid,'requirement_revision':s['execution_revision']}
    # One logical slot is committed with review, queue and original intent. Different
    # inspectors/tool identities cannot create a second task for the same revision.
    with store.db:
        review_id=review(store,mvp,commands);mvp['verification_review']=review_id
        key=store.queue('implement',mvp,review_id)
        body={'entry_binding':entry,'entry_workspace':inspected.get('entry_workspace'),'requirement':rid,'revision':revision,'execution_revision':s['execution_revision'],'project':s['project'],'repo_version':current['sha256'],'epoch':binding['epoch'],'review':review_id,'queue_key':key,'origin_agent':origin,'inspection':inspection,'status':'awaiting_team_submission'}
        request=str(uuid.uuid5(uuid.NAMESPACE_URL,slot))
        store.db.execute('INSERT INTO records VALUES (?,?,?,?)',(request,'original_intent',canonical(body),time.time()))
        store.put(slot,{'request':request})
    return {'request':request,**body,'execution':False,'next':'战队/续办器使用request_submit继续同一持久原请求'}

@cooperative
def submit_request(store,request,call=None):
    from .team import require_readiness,Team,transport
    row=store.db.execute("SELECT body FROM records WHERE id=? AND kind='original_intent'",(request,)).fetchone()
    if not row:raise Blocked('原请求不存在')
    body=json.loads(row[0]);s=status(store,body['requirement'])
    from .project_binding import require
    entry_task=body.get('entry_binding',{}).get('task',{})
    require(store,body['project'],workspace=body.get('entry_workspace'),epoch=body['epoch'],task=entry_task.get('task'),run=entry_task.get('run'),call=call)
    binding=require_readiness(store,project=body['project'])
    if not s['project'] or s['state']!='needs_engineer_plan' or s['execution_revision']!=body.get('execution_revision',body['revision']) or binding['project']!=body['project'] or binding['epoch']!=body['epoch'] or repo_version(store,body['project'])['sha256']!=body['repo_version']:raise Blocked('原请求目标/版本已变化；不得提交过期工程规格')
    from .automation import link
    link(store,body['queue_key'],'local-owner')
    result=Team(store,call or transport(store.get('team_config',{}))).submit(body['queue_key'],binding['workspace'])
    return {'request':request,'queue_key':body['queue_key'],'team':result}

@cooperative
def start_from_material(store,reading,project=None):
    row=store.db.execute("SELECT body FROM records WHERE id=? AND kind='source_reading'",(reading,)).fetchone()
    if not row:raise Blocked('素材不存在')
    value=json.loads(row[0])
    if value['status']!='read':raise Blocked('未读素材不能代替需求输入')
    rid=start(store,project,value.get('text','')[:10000] or '从素材继续需求')
    body=load(store,rid);body['source_reading']=reading
    with store.db:store.db.execute('UPDATE records SET body=? WHERE id=?',(canonical(body),rid))
    return {'id':rid,**status(store,rid)}

@cooperative
def link_project(store,rid,project,expected):
    body=load(store,rid)
    if digest(canonical(body).encode())!=expected:raise Blocked('需求版本已变化')
    if not store.db.execute('SELECT id FROM projects WHERE id=?',(project,)).fetchone():raise Blocked('先登记真实项目，禁止通用库假repo')
    body['project']=project;body['intent']=False
    with store.db:store.db.execute('UPDATE records SET body=? WHERE id=?',(canonical(body),rid))
    return status(store,rid)



def original_request(store,request):
    row=store.db.execute("SELECT body FROM records WHERE id=? AND kind='original_intent'",(request,)).fetchone()
    if not row:raise Blocked('原请求不存在')
    body=json.loads(row[0]);queued=store.db.execute('SELECT * FROM outbox WHERE id=?',(body['queue_key'],)).fetchone()
    if not queued or json.loads(queued['body']).get('device')!=store.device:raise Blocked('历史设备原请求只读，须正式新轮次')
    return body,queued

@cooperative
def request_cancel(store,request,call=None):
    body,row=original_request(store,request)
    current=status(store,body['requirement'])
    answer(store,body['requirement'],'implement',False,current['revision'])
    key='request-control:'+body['queue_key'];prior=store.get(key,{})
    if prior.get('action')!='cancel':store.put(key,{'action':'cancel','state':'pending','request':request,'requirement':body['requirement'],'revision':status(store,body['requirement'])['execution_revision']})
    return request_status(store,request,call)


def request_status(store,request,call=None):
    from .team import transport
    body,row=original_request(store,request)
    result={'request':request,'original':body,'queue_state':row['state'],'task':row['reference'],'control':store.get('request-control:'+body['queue_key'],{}),'candidate':store.get('candidate:engineering:'+body['queue_key'],{})}
    if row['reference']:
        try:
            call=call or transport(store.get('team_config',{}));result['backend']=call('get_task',{'task_id':row['reference']});result['settled']=call('task_settled',{'task_id':row['reference']})
        except Exception:result['settled']={'state':'unknown','stopped':False}
    return result

@cooperative
def request_adjust(store,request,inspection,revision,plan,commands,confirmed,origin,*,call=None):
    from .team import require_readiness
    from .verification import review,bind_candidate_commands,verified_harnesses
    body,row=original_request(store,request);current=status(store,body['requirement']);binding=require_readiness(store,project=body['project'])
    if not current['intent'] or current['question'] or confirmed is not True or not row['reference']:raise Blocked('调整需当前实施意图、完整新MVP及实际原任务')
    inspected_row=store.db.execute("SELECT body FROM records WHERE id=? AND kind='engineering_inspection'",(inspection,)).fetchone()
    if not inspected_row:raise Blocked('调整缺工程检查')
    inspected=json.loads(inspected_row[0]);source=repo_version(store,body['project']);candidate=store.get('candidate:engineering:'+body['queue_key'],{})
    from .project_binding import require
    entry_task=inspected.get('entry_binding',{}).get('task',{})
    require(store,body['project'],candidate['workspace'],task=row['reference'],run=entry_task.get('run'),epoch=inspected.get('epoch'),call=call,candidate_request=body['queue_key'])
    if (inspected.get('candidate_context') or {}).get('sha256')!=repo_fingerprint(Path(candidate['workspace']))['sha256']:raise Blocked('原任务候选自工程检查后变化，请停稳并重新检查')
    if inspected.get('execution_revision')!=current['execution_revision'] or inspected.get('requirement')!=body['requirement'] or inspected.get('revision')!=revision or inspected.get('epoch')!=binding['epoch'] or inspected['repo']['sha256']!=source['sha256'] or candidate.get('baseline')!=source['sha256']:raise Blocked('调整基线/范围变化，须协调后重新审阅')
    if not isinstance(plan,str) or not plan.strip():raise Blocked('调整需具体工程计划')
    mvp={**current['answers'],'engineering_plan':redact_text(plan),'repo_version':source['sha256'],'requirement_id':body['requirement'],'requirement_revision':current['execution_revision']}
    reviewed=review(store,mvp,commands)
    effective=bind_candidate_commands(commands,source['workspace'],candidate['workspace'],verified_harnesses(store,commands))
    value={'action':'continue','state':'pending','request':request,'requirement':body['requirement'],'revision':current['execution_revision'],'review':reviewed,'mvp':mvp,'commands':effective,'candidate_sha256':repo_fingerprint(Path(candidate['workspace']))['sha256'],'arguments':{'task_id':row['reference'],'spec':'继续同一工程任务，按当前已审新需求及新验收调整；不部署、不操作生产/凭据/外发。\n'+canonical(sanitize(mvp)),'verification_commands':effective},'origin':origin}
    store.put('request-control:'+body['queue_key'],value)
    return {'request':request,'task':row['reference'],'adjustment_saved':True,'state':'waiting_for_settled_then_reviewed_continue_or_new_round','execution_started':False}

if __name__=='__main__':sys.exit(main())
