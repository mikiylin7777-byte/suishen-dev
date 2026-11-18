"""Public context-only MCP stdio server. No execution scheduler or shell tool."""
import argparse
import json
from pathlib import Path
import sys
from .core import Store, Blocked, PROTOCOL, canonical, cooperative, digest
from . import knowledge, workflows
from .privacy import sanitize

# Wire schema is derived from the one product protocol, never a second product contract.
OPERATIONS={
 'legacy_selections':{},'legacy_preflight':{'selection':'string'},'legacy_capture':{'selection':'string','sensitive':'boolean','recipient':'string','public_history':'boolean'},'legacy_restore':{'package':'string','identity':'string'},'legacy_draft':{'restored':'string','task':'string','checkpoint':'object'},
 'planning_resume':{'id':'string','inspection':'string','revision':'string','confirmed':'boolean'},
 'engineering_context':{'id':'string','files':'array','workspace':'string','task':'string','run':'string'},
 'engineering_review':{'id':'string','inspection':'string','revision':'string','plan':'string','commands':'array','confirmed':'boolean'},
 'request_submit':{'request':'string'},
 'material_associate':{'reading':'string','project':'string','favorite':'boolean'},
 'material_requirement':{'reading':'string','project':'string'},
 'requirement_project':{'id':'string','project':'string','expected':'string'},
 'assets':{},'asset_retention':{},'asset_restore':{'asset':'string'},
 'session_archive_search':{'query':'string','source':'string'},'session_archive_get':{'archive':'string'},
 'sessions':{},'session_read':{'selection':'string'},'session_commit':{'batch':'string','summary':'string'},
 'request_resume_supervision':{'request':'string','confirmed':'boolean'},
 'request_status':{'request':'string'},'request_cancel':{'request':'string'},'request_continue':{'request':'string'},'request_adjust':{'request':'string','inspection':'string','revision':'string','plan':'string','commands':'array','confirmed':'boolean'},
 'project_bind':{'project':'string'},'project_unbind':{'project':'string'},
 'project_create':{'name':'string','project':'string'},'project_register':{'name':'string','path':'string','project':'string'},'requirement_create_project':{'id':'string','name':'string','project':'string','expected':'string'},
 'candidate_receive_ack':{'request':'string'},
 'pending_requirements':{},'approval_decisions':{},'approval_claim':{'approval':'string'},'approval_checkpoint':{'approval':'string'},
 'projects':{},'search':{'query':'string','project':'string'},
 'requirement_start':{'project':'string','idea':'string','known':'object'},
 'requirement_status':{'id':'string'},'requirement_answer':{'id':'string','field':'string','value':'any','expected':'string'},
 'plan':{'id':'string'},'troubleshoot':{'project':'string','query':'string'},
 'source_text':{'source':'string','project':'string','text':'string','favorite':'boolean'},
 'source_link':{'url':'string','project':'string','favorite':'boolean'},
 'task_update':{'reference':'string','summary':'string'},
 'source_file':{'path':'string','project':'string','favorite':'boolean'},
 'refine':{'reading':'string','summary':'string','method':'string','conditions':'string','uncertainties':'string','vision':'object'},
 'checkpoint':{'project':'string',**{k:'any' for k in PROTOCOL['checkpoint_required']}},
 'checkpoints':{'project':'string'},'preferences':{},
 'task_reference':{'task':'string','project':'string','origin_device':'string','origin_caller':'string'},
 'document':{'project':'string','expected':'string','manual':'string'},
 'note':{'project':'string','summary':'string'},
}
WRITES=set(OPERATIONS)-{'legacy_selections','legacy_preflight','request_status','approval_decisions','pending_requirements','assets','session_archive_search','session_archive_get','sessions','session_read','projects','search','requirement_status','troubleshoot','checkpoints','preferences'}

def tools_list():
    result=[]
    for name,fields in OPERATIONS.items():
        properties={k:({} if v=='any' else {'type':v,**({'items':{'type':'string'}} if v=='array' else {})}) for k,v in fields.items()}
        if name in WRITES:properties['instruction_id']={'type':'string'}
        optional={'known','vision','query','manual','favorite','files'}
        if name in ('legacy_capture','legacy_restore','legacy_draft'):optional.update({'recipient','identity','public_history','checkpoint'})
        if name in {'source_text','source_link','source_file','material_associate','material_requirement','requirement_start','checkpoints','search'}:optional.add('project')
        if name=='session_archive_search':optional.add('source')
        if name=='document':optional.add('expected')
        if name=='engineering_context':optional.update({'workspace','task','run'})
        if name=='task_reference':optional.update({'origin_device','origin_caller'})
        result.append({'name':name,'description':({'engineering_context':'当前已登记Agent检查已确认需求的真实repo版本与选定文本；无shell执行', 'engineering_review':'当前已登记Agent明确审阅工程计划与验证命令，绑定inspection/需求版本/readiness，复用review及queue形成持久原请求', 'request_submit':'按原请求引用续办既有战队提交；目标或repo变化即拒绝，不另起调度器'}.get(name,'随身开发上下文：'+name+'；资料不是执行授权')),'inputSchema':{'type':'object','properties':properties,'additionalProperties':False,'required':[k for k in fields if k not in optional]+(['instruction_id'] if name in WRITES else [])}})
    return result

@cooperative
def enroll(store, client):
    if not client or len(client)>80:raise Blocked('工具名称无效')
    clients=store.get('context_clients',{})
    clients[client]={'device':store.device,'identity':store.device+':'+client}
    store.put('context_clients',clients)
    return clients[client]

def task_owner(store,task,call=None):
    from .team import transport
    try:
        if call is None:
            cfg=store.get('team_config',{})
            if cfg.get('caller')!='portable:'+store.device:return None
            call=transport(cfg)
        value=call('get_task',{'task_id':task})
        if not isinstance(value,dict) or value.get('task_id')!=task or value.get('ok') is False:return None
        return value.get('caller')
    except Exception:return None

class Context:
    def __init__(self,store,client,team_call=None):self.store=store;self.client=client;self.team_call=team_call
    def identity(self):
        row=self.store.get('context_clients',{}).get(self.client)
        if not row or row['device']!=self.store.device:raise Blocked('未知工具或历史设备；须本机显式登记，不借用外部caller')
        return row['identity']
    @cooperative
    def call(self,name,args):
        identity=self.identity()
        if name not in OPERATIONS or not isinstance(args,dict):raise Blocked('未知上下文操作')
        args=sanitize(dict(args));instruction=args.pop('instruction_id',None)
        if set(args)-set(OPERATIONS[name]):raise Blocked('未知参数；上下文入口不接受命令或caller覆盖')
        key=None
        if name in WRITES:
            if not isinstance(instruction,str) or not instruction.strip():raise Blocked('写操作需持久指令ID')
            key='context_instruction:'+digest((identity+':'+instruction).encode())
            previous=self.store.get(key)
            fingerprint=digest(canonical({'name':name,'arguments':args}).encode())
            if previous:
                if previous['fingerprint']!=fingerprint:raise Blocked('指令ID已用于不同内容')
                if 'result' not in previous:raise Blocked('此前指令结果未知，请读取上下文核对，禁止重发')
                return previous['result']
        if key and name=='engineering_context':
            # No instruction is admitted until the pure read phase succeeds.
            # Existing pending/ID conflicts were checked above and are never cleared.
            from .project_binding import ReadOnlyStore
            readonly=ReadOnlyStore(self.store.root)
            try:
                workflows.prepare_engineering_context(readonly,args['id'],identity,args.get('files'),workspace=args.get('workspace'),task=args.get('task'),run=args.get('run'),call=self.team_call)
            finally:readonly.db.close()
        if key:self.store.put(key,{'fingerprint':fingerprint,'state':'pending'})
        result=sanitize(self._call(name,args,identity))
        if key:self.store.put(key,{'fingerprint':fingerprint,'result':result})
        return result
    def _call(self,name,a,identity):
        s=self.store
        if name=='engineering_context':return workflows.engineering_context(s,a['id'],identity,a.get('files'),workspace=a.get('workspace'),task=a.get('task'),run=a.get('run'),call=self.team_call)
        if name=='engineering_review':return workflows.engineering_review(s,a['id'],a['inspection'],a['revision'],a['plan'],a['commands'],a['confirmed'],identity,call=self.team_call)
        if name=='request_submit':return workflows.submit_request(s,a['request'],self.team_call)
        if name=='material_associate':return knowledge.associate(s,a['reading'],a.get('project'),a.get('favorite'))
        if name=='material_requirement':return workflows.start_from_material(s,a['reading'],a.get('project'))
        if name=='requirement_project':return workflows.link_project(s,a['id'],a['project'],a['expected'])
        if name=='assets':
            from .material_assets import retention
            retention(s)
            return [json.loads(r[0]) for r in s.db.execute("SELECT value FROM meta WHERE key LIKE 'material_asset:%'")]
        if name=='asset_retention':
            from .material_assets import retention
            return retention(s)
        if name=='asset_restore':
            from .material_assets import restore
            return restore(s,a['asset'])
        if name=='session_archive_search':
            from .session_adapters import archive_search
            return archive_search(s,a.get('query',''),a.get('source'))
        if name=='session_archive_get':
            from .session_adapters import archive_get
            return archive_get(s,a['archive'])
        if name=='sessions':
            return [{'selection':r['key'].split(':',1)[1],**json.loads(r['value'])} for r in s.db.execute("SELECT key,value FROM meta WHERE key LIKE 'session_source:%'")]
        if name=='session_read':
            from .session_adapters import read_batch
            return read_batch(s,a['selection'])
        if name=='session_commit':
            from .session_adapters import commit
            return commit(s,a['batch'],a['summary'])
        if name.startswith('legacy_'):
            from . import legacy_cli as lc
            if name=='legacy_selections':return [{'selection':r['key'].split(':')[-1],'projects':[p['id'] for p in json.loads(r['value']).get('projects',[])],'exclusions':json.loads(r['value']).get('exclusions',[])} for r in s.db.execute("SELECT key,value FROM meta WHERE key LIKE 'legacy:selection:%'") if json.loads(r['value']).get('device')==s.device and json.loads(r['value']).get('root')==str(s.root)]
            if name=='legacy_preflight':return lc.preview(lc.selection(s,a['selection'])['projects'])
            if name=='legacy_capture':
                if lc.selection(s,a['selection'])['histories'] and not a['sensitive']:raise Blocked('所选原始历史的明文确认须本人本机菜单；MCP仅可明确age加密携带')
                import uuid
                target=s.root/'数据/迁移候选'/('legacy-'+uuid.uuid4().hex+'.bundle');target.parent.mkdir(parents=True,exist_ok=True)
                return lc.capture(s,a['selection'],target,sensitive=a['sensitive'],recipient=a.get('recipient'),history_plaintext=a.get('public_history',False))
            if name=='legacy_restore':
                import uuid
                target=s.root/'数据/旧资产'/uuid.uuid4().hex;target.parent.mkdir(parents=True,exist_ok=True)
                return lc.restore(s,a['package'],target,a.get('identity'))
            return lc.draft(s,a['restored'],a['task'],a.get('checkpoint'))
        if name=='planning_resume':
            from .planner import resume_reviewed
            return resume_reviewed(s,a['id'],a['inspection'],a['revision'],a['confirmed'],identity,self.team_call)
        if name=='request_resume_supervision':
            from .automation import resume_supervision
            original,_=workflows.original_request(s,a['request'])
            from .team import transport
            return resume_supervision(s,original['queue_key'],a['confirmed'],self.team_call or transport(s.get('team_config',{})))
        if name=='request_status':return workflows.request_status(s,a['request'],self.team_call)
        if name=='request_cancel':return workflows.request_cancel(s,a['request'],self.team_call)
        if name=='request_continue':
            original,_=workflows.original_request(s,a['request']);current=workflows.status(s,original['requirement'])
            result=workflows.answer(s,original['requirement'],'implement',True,current['revision'])
            return {'requirement':result,'next':'重新engineering_context/review后request_adjust；不凭历史批准执行'}
        if name=='request_adjust':return workflows.request_adjust(s,a['request'],a['inspection'],a['revision'],a['plan'],a['commands'],a['confirmed'],identity,call=self.team_call)
        if name=='project_bind':
            from .team import bind_new_tasks,transport
            return bind_new_tasks(s,self.team_call or transport(s.get('team_config',{})),a['project'])
        if name=='project_unbind':
            from .team import revoke_new_tasks
            return revoke_new_tasks(s,a['project'])
        if name=='project_create':
            from .candidates import create_managed
            result=create_managed(s,a['name'],a['project'])
            workflows.project_overview(s,a['project'],digest((s.root/'知识库/项目'/(a['project']+'.md')).read_bytes()))
            return result
        if name=='project_register':return {'project':s.register(a['name'],a['path'],a['project']),'original_modified':False}
        if name=='requirement_create_project':
            current=workflows.status(s,a['id'])
            if current['revision']!=a['expected'] or current['project'] or not current['intent'] or current['question']:raise Blocked('先补齐MVP并明确新建项目的实施意图')
            from .candidates import create_managed
            result=create_managed(s,a['name'],a['project'])
            linked=workflows.link_project(s,a['id'],a['project'],a['expected'])
            workflows.answer(s,a['id'],'implement',True,linked['revision'])
            workflows.project_overview(s,a['project'],digest((s.root/'知识库/项目'/(a['project']+'.md')).read_bytes()))
            if s.get('team_config'):
                from .team import bind_new_tasks,transport
                bind_new_tasks(s,self.team_call or transport(s.get('team_config')),a['project'])
            return {**result,'requirement':workflows.status(s,a['id']),'execution_started':False}
        if name=='candidate_receive_ack':
            from .candidates import acknowledge_external
            return acknowledge_external(s,a['request'])
        if name=='approval_decisions':return [{'approval':r['key'].split(':',1)[1],**json.loads(r['value'])} for r in s.db.execute("SELECT key,value FROM meta WHERE key LIKE 'approval-decision:%'")]
        if name in ('approval_claim','approval_checkpoint'):
            from .bots import approval_claim,approval_execute_checkpoint
            return (approval_claim if name=='approval_claim' else approval_execute_checkpoint)(s,a['approval'],identity)
        if name=='pending_requirements':return [workflows.status(s,r[0]) for r in s.db.execute("SELECT id FROM records WHERE kind='requirement' ORDER BY created DESC LIMIT 100")]
        if name=='projects':return [dict(r) for r in s.db.execute('SELECT id,name FROM projects ORDER BY name')]
        if name in ('search','troubleshoot'):return knowledge.search(s,a.get('query',''),a.get('project'))
        if name=='requirement_start':return {'id':workflows.start(s,a.get('project'),a['idea'],a.get('known'))}
        if name=='requirement_status':return workflows.status(s,a['id'])
        if name=='requirement_answer':
            if not a.get('expected'):raise Blocked('多工具更新需求必须提供读取版本')
            return workflows.answer(s,a['id'],a['field'],a['value'],a['expected'])
        if name=='plan':return workflows.plan_request(s,a['id'])
        if name=='source_text':return knowledge.ingest(s,a['source'],[a['project']] if a.get('project') else [],text=a['text'],favorite=a.get('favorite',False))
        if name=='source_link':return knowledge.ingest(s,a['url'],[a['project']] if a.get('project') else [],network=True,favorite=a.get('favorite',False))
        if name=='source_file':
            allowed=s.get('context_source_files',[])
            if str(Path(a['path']).resolve()) not in allowed:raise Blocked('文件未明确选择；请在本机登记素材文件')
            return knowledge.ingest(s,a['path'],[a['project']] if a.get('project') else [],favorite=a.get('favorite',False))
        if name=='refine':
            if a.get('vision'):a['vision']={**a['vision'],'agent':identity}
            return {'id':knowledge.refine(s,**a)}
        if name=='checkpoint':
            project=a.get('project') or s.get('selected_project')
            if not s.db.execute('SELECT id FROM projects WHERE id=?',(project,)).fetchone():raise Blocked('检查点必须关联已登记项目')
            return {'id':s.record('checkpoint',{**a,'device':s.device,'origin':identity,'project':project})}
        if name=='checkpoints':
            return [{'id':r['id'],**json.loads(r['body'])} for r in s.db.execute("SELECT id,body FROM records WHERE kind='checkpoint' ORDER BY created DESC") if not a.get('project') or json.loads(r['body']).get('project')==a['project']][:20]
        if name=='preferences':
            from .onboarding import preferences
            return preferences(s)
        if name=='task_reference':
            caller=task_owner(s,a['task'],self.team_call)
            local=caller=='portable:'+s.device
            body={**a,'origin_device':s.device if local else a.get('origin_device','external'),'origin_caller':caller or 'unverified','linked_by':identity,'read_only':not local}
            return {'id':s.record('task_reference',body),'read_only':not local,'execution':False}
        if name=='task_update':
            row=s.db.execute("SELECT body FROM records WHERE id=? AND kind='task_reference'",(a['reference'],)).fetchone()
            reference=json.loads(row[0]) if row else {}
            if reference.get('read_only',True) or reference.get('origin_device')!=s.device or task_owner(s,reference.get('task'),self.team_call)!='portable:'+s.device:raise Blocked('历史外部caller或未核实任务只读')
            return {'id':s.record('task_context_update',{**a,'origin':identity,'device':s.device}),'backend_modified':False}
        if name=='document':return workflows.project_overview(s,**a)
        if name=='note':return {'id':s.record('decision',{**a,'origin':identity,'execution':False}),'execution':False}
        raise Blocked('未实现操作')

def serve(context, input_stream=sys.stdin, output_stream=sys.stdout):
    for line in input_stream:
        request=None
        try:
            if len(line)>2*1024*1024:raise Blocked('请求过大')
            request=json.loads(line);method=request.get('method');rid=request.get('id')
            if method=='initialize':
                context.identity();result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'portable-context','version':PROTOCOL['version']}}
            elif method=='tools/list':context.identity();result={'tools':tools_list()}
            elif method=='tools/call':
                p=request['params'];value=context.call(p['name'],p.get('arguments',{}));result={'content':[{'type':'text','text':canonical(value)}],'isError':False}
            elif method=='ping':result={}
            elif method.startswith('notifications/'):continue
            else:raise Blocked('不支持的MCP方法')
            response={'jsonrpc':'2.0','id':rid,'result':result}
        except (Blocked,ValueError,KeyError,TypeError,AttributeError) as e:
            response={'jsonrpc':'2.0','id':request.get('id') if isinstance(request,dict) else None,'error':{'code':-32602,'message':str(e)}}
        if isinstance(request,dict) and 'id' not in request:continue
        output_stream.write(canonical(response)+'\n');output_stream.flush()

def register(subparser):
    p=subparser.add_parser('context');p.add_argument('action',choices=['serve','enroll','select-file']);p.add_argument('--client',required=True);p.add_argument('--file');return p

def run(store,args):
    if args.action=='enroll':return enroll(store,args.client)
    if args.action=='select-file':
        Context(store,args.client).identity();path=Path(args.file)
        if path.is_symlink() or not path.is_file():raise Blocked('仅登记普通文件')
        files=store.get('context_source_files',[]);files=sorted(set(files+[str(path.resolve())]));store.put('context_source_files',files);return {'selected':str(path.resolve())}
    serve(Context(store,args.client));return None

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);register(p.add_subparsers(required=True));args=p.parse_args(argv);store=Store(args.root)
    try:
        result=run(store,args)
        if result is not None:print(canonical(result))
        return 0
    except (Blocked,ValueError,OSError) as e:print(canonical({'status':'BLOCKED','reason':str(e)}),file=sys.stderr);return 2
    finally:store.db.close()

if __name__=='__main__':sys.exit(main())
