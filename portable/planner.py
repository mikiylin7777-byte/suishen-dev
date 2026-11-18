"""Wake engineering review through the sole official team, using durable references.

No direct model/provider call, local engineering executor, or undocumented UI API.
Only requirements with explicit implementation intent enter this consumer.
"""
import argparse
import json
from pathlib import Path
import shlex
import sys
from .core import Blocked,Store,canonical,cooperative,digest
from . import workflows
from .team import require_readiness,transport
from .automation import notice,state,save


class PlanFault(Blocked):pass

def schema_instruction(requirement_revision,repo_sha256):
    example={'requirement_revision':requirement_revision,'repo_sha256':repo_sha256,'plan':'1. 检查已有实现。2. 在工程候选实现本次首版。3. 运行对应验收。','verification_commands':['python3 -m unittest discover -s tests'],'review_confirmed':True}
    return '最终答复必须是单个纯JSON对象，无代码围栏。精确类型：requirement_revision:string；repo_sha256:string；plan:非空string（文字步骤，绝对不能数组或对象）；verification_commands:非空array，每项非空string且是实际可运行的候选相对命令；review_confirmed:boolean且为true。禁止cd/测试原项目、禁止使用示例命令代替实际工程验收。结构示例（其中命令仅示意，必须按本项目真实内容审阅替换）：'+canonical(example)


def verification_command(store,rid,sha):
    package=str(Path(__file__).parent.parent)
    code='import sys;sys.path.insert(0,'+repr(package)+');from portable.planner import main;sys.exit(main())'
    return shlex.join([sys.executable,'-I','-B','-c',code,'--root',str(store.root),'planner','verify',rid,'--sha',sha])


def validate_result(value,requirement,repo):
    if not isinstance(value,dict):raise PlanFault('SCHEMA_OBJECT_REQUIRED: 最终结果必须是JSON对象')
    if value.get('requirement_revision')!=requirement['revision'] or value.get('repo_sha256')!=repo['sha256']:raise PlanFault('BOUND_VERSION_MISMATCH: requirement_revision/repo_sha256必须原样对应当前审阅')
    if not isinstance(value.get('plan'),str) or not value['plan'].strip():raise PlanFault('PLAN_STRING_REQUIRED: plan必须是非空字符串，不能是数组/对象')
    if value.get('review_confirmed') is not True:raise PlanFault('REVIEW_CONFIRMATION_REQUIRED: review_confirmed必须为boolean true')
    if not isinstance(value.get('verification_commands'),list) or not value['verification_commands'] or any(not isinstance(c,str) or not c.strip() for c in value['verification_commands']):raise PlanFault('COMMAND_ARRAY_REQUIRED: verification_commands须为非空字符串数组')
    from .verification import candidate_commands_checked
    try:candidate_commands_checked(value['verification_commands'],repo['workspace'])
    except Blocked:raise PlanFault('CANDIDATE_COMMAND_REQUIRED: 验收必须验证候选代码，不能指向原项目')
    return value

class Planner:
    def __init__(self,store,call=None):self.store=store;self.call=call or transport(store.get('team_config',{}))
    @cooperative
    def tick(self):
        s=self.store
        if state(s)['paused']:return {'paused':True}
        observed=0
        self.cancel_obsolete_plans()
        for row in s.db.execute("SELECT id FROM records WHERE kind='requirement' ORDER BY created").fetchall():
            requirement=workflows.status(s,row['id'])
            if requirement.get('device')!=s.device or requirement['state'] not in ('needs_engineer_plan','needs_task_adjustment'):continue
            try:binding=require_readiness(s,project=requirement['project'])
            except Blocked:continue
            originals=[json.loads(r['body']) for r in s.db.execute("SELECT body FROM records WHERE kind='original_intent'") if json.loads(r['body']).get('requirement')==row['id'] and json.loads(r['body']).get('execution_revision',json.loads(r['body']).get('revision'))==requirement['execution_revision']]
            if originals:continue
            key='planner:'+row['id']+':'+requirement['execution_revision'];ref=s.get(key,{})
            if ref.get('state') in ('done','blocked'):continue
            view=None
            try:
                repo=workflows.repo_version(s,requirement['project']);adjust_request=None;adjust_candidate=None
                if requirement['state']=='needs_task_adjustment':
                    for record in s.db.execute("SELECT id,body FROM records WHERE kind='original_intent' ORDER BY created DESC"):
                        original=json.loads(record['body']);control=s.get('request-control:'+original['queue_key'],{})
                        if original.get('requirement')==row['id'] and control.get('state')=='pending' and control.get('action')=='scope_review_required':
                            _,queued=workflows.original_request(s,record['id'])
                            if not queued['reference']:break
                            if not self.call('task_settled',{'task_id':queued['reference']}).get('stopped'):break
                            adjust_request=record['id'];adjust_candidate=s.get('candidate:engineering:'+original['queue_key']);break
                    if not adjust_request:continue
                if not ref:
                    inspection=workflows.engineering_context(s,row['id'],'official-team-planner',[],call=self.call)
                    command=verification_command(s,row['id'],repo['sha256'])
                    request='portable-plan:'+digest(canonical([s.device,row['id'],requirement['execution_revision'],repo['sha256'],binding['epoch']]).encode())
                    from .candidates import prepare
                    candidate_context=inspection.get('candidate_context')
                    candidate=prepare(s,request,requirement['project'],repo['sha256'],'planning',source_override=adjust_candidate['workspace'] if adjust_candidate else None,expected_override=candidate_context['sha256'] if candidate_context else None)
                    ref={'binding_epoch':binding['epoch'],'project':binding['project'],'device':s.device,'adjust_request':adjust_request,'inspection_revision':inspection['revision'],'workspace':candidate['workspace'],'request':request,'inspection':inspection['inspection'],'repo_sha256':repo['sha256'],'command':command,'state':'prepared','attempts':0}
                    s.put(key,ref)
                expected_request='portable-plan:'+digest(canonical([s.device,row['id'],requirement['execution_revision'],ref['repo_sha256'],binding['epoch']]).encode())
                if ref['request']!=expected_request or ref.get('binding_epoch',binding['epoch'])!=binding['epoch']:raise Blocked('规划项目绑定变化，旧任务只读')
                if ref['repo_sha256']!=repo['sha256']:raise Blocked('规划期间repo变化，需新审阅')
                if not ref.get('task'):
                    found=self.call('lookup_request',{'idempotency_key':ref['request']})
                    if found.get('found') is True:ref['task']=found['task_id']
                    elif found.get('found') is False and ref['state'] in ('prepared','sending'):
                        ref['state']='sending';s.put(key,ref)
                        spec=schema_instruction(ref['inspection_revision'],repo['sha256'])+'\n只读工程规划，禁止实现、提交、修改业务repo、外发、付费或生产动作。只读取本工作目录内脱敏的项目规则及必要文件；不回读原repo、凭据或原始会话。仅使用已配置战队执行器和既有模型费用范围。根据以下需求返回一个纯JSON最终答复（不得代码围栏），含 requirement_revision、repo_sha256、plan、verification_commands（具体可运行验收命令）、review_confirmed:true。字段中的版本值必须原样使用。无额外功能。需求数据：\n'+canonical({'mvp':requirement['answers'],'requirement_revision':ref['inspection_revision'],'repo_sha256':repo['sha256'],'source_code_snapshot':ref['workspace'],'planning_workspace_only':ref['workspace']})
                        result=self.call('create_task',{'title':'只读工程审阅：'+requirement['answers']['problem'][:70],'spec':spec,'workspace':ref['workspace'],'idempotency_key':ref['request'],'acceptance_criteria':[ref['command']]})
                        ref['task']=result['task_id'];ref['state']='submitted'
                    else:raise Blocked('规划提交未知，继续查询同一原请求，不重复启动')
                    s.put(key,ref)
                if ref.get('repair'):
                    repair=ref['repair'];found=self.call('lookup_request',{'idempotency_key':repair['key']})
                    if found.get('found') is True:
                        if found.get('state')!='completed':continue
                        ref.pop('repair');ref.pop('verified',None);s.put(key,ref)
                    elif found.get('found') is False:
                        self.call('continue_task',repair['arguments']);continue
                    else:continue
                view=self.call('get_task',{'task_id':ref['task']})
                if view.get('status') in ('running','queued'):continue
                result=self.call('get_task_result',{'task_id':ref['task'],'include_log_tail':False,'max_result_bytes':65536})
                if result.get('execution_status')!='done' or type(result.get('exit_code')) is not int or result.get('exit_code')!=0 or result.get('run_id')!=view.get('run_id'):raise PlanFault('只读规划执行未成功')
                if not result.get('final_answer') and result.get('result_compatibility',{}).get('status')=='NATIVE_RESULT_UNVERIFIED':
                    v=state(s);notice(v,'blocked',ref['task'],{'reason':'正式执行已结束，但本轮最终答复归属证据不足；未冒用旧会话，待兼容证据复核，不计工程返修轮次'});save(s,v)
                    continue
                answer=result.get('final_answer') or {}
                if answer.get('truncated'):raise PlanFault('规划答复不完整')
                try:plan=validate_result(json.loads(answer.get('text','')),{**requirement,'revision':ref['inspection_revision']},repo)
                except (ValueError,TypeError):raise PlanFault('JSON_PARSE_ERROR: 返回完整纯JSON对象，不要代码围栏或前后说明')
                # Reconcile each same official lifecycle key before admission.
                verify_key=ref['request']+':verify:'+view['run_id']
                if ref.get('verified_run')!=view['run_id']:ref.pop('verified',None)
                if not ref.get('verified'):
                    found=self.call('lookup_request',{'idempotency_key':verify_key})
                    if found.get('found') is True:
                        if found.get('state')!='completed':continue
                        ref['verified']=True;ref['verified_run']=view['run_id'];s.put(key,ref)
                    elif found.get('found') is False:
                        self.call('reverify_task',{'task_id':ref['task'],'idempotency_key':verify_key});continue
                    else:continue
                evidence=result.get('verification',{}).get('commands',[])
                if isinstance(evidence,dict):evidence=evidence.get('items',[])
                if len(evidence)!=1 or evidence[0].get('cmd')!=ref['command'] or evidence[0].get('exit_code')!=0:raise PlanFault('规划只读验证未通过')
                accept_key=ref['request']+':accept:'+view['run_id']
                accepted=self.call('accept_task',{'task_id':ref['task'],'idempotency_key':accept_key})
                if ref.get('adjust_request'):
                    original=workflows.request_adjust(s,ref['adjust_request'],ref['inspection'],ref['inspection_revision'],plan['plan'],plan['verification_commands'],True,'official-team:'+ref['task'],call=self.call)
                    ref.update(state='done',original=ref['adjust_request'],engineering_task=original['task'])
                else:
                    original=workflows.engineering_review(s,row['id'],ref['inspection'],ref['inspection_revision'],plan['plan'],plan['verification_commands'],True,'official-team:'+ref['task'],call=self.call)
                    submitted=workflows.submit_request(s,original['request'],self.call)
                    ref.update(state='done',original=original['request'],engineering_task=submitted['team']['task_id'])
                s.put(key,ref);observed+=1
            except Exception as exc:
                # Transport loss is uncertainty, not a completed failed round.
                # Keep the same key; official lookup decides before re-admission.
                if not isinstance(exc,PlanFault) or not view or view.get('status') in ('running','queued'):
                    s.put(key,ref);continue
                failed_run=view.get('run_id')
                if failed_run==ref.get('failed_run'):
                    s.put(key,ref);continue
                ref['last_fault']=str(exc)
                ref['failed_run']=failed_run
                ref['attempts']=ref.get('attempts',0)+1
                if ref['attempts']>=3:
                    ref['state']='blocked';v=state(s);notice(v,'blocked',row['id'],{'reason':'工程规划三轮未取得可验证进展；保留原请求等待复核','error':ref['last_fault'],'resume':'当前Agent重新engineering_context后planning_resume；不另建需求'});save(s,v)
                s.put(key,ref)
                if ref['attempts']<3 and ref.get('task'):
                    repair_key=ref['request']+':repair:'+str(ref['attempts'])
                    arguments={'task_id':ref['task'],'idempotency_key':repair_key,'spec':'仅修复原只读规划答复；不修改业务代码、不扩大范围。实际失败分类：'+ref['last_fault']+'\n'+schema_instruction(ref['inspection_revision'],ref['repo_sha256'])}
                    ref['repair']={'key':repair_key,'arguments':arguments};s.put(key,ref)
                    try:self.call('continue_task',arguments)
                    except Exception:pass
        return {'planned':observed}


    def cancel_obsolete_plans(self):
        """Withdraw only this device's durable planning tasks via formal lifecycle."""
        from .team import Team
        s=self.store;team=Team(s,self.call)
        for item in s.db.execute("SELECT key,value FROM meta WHERE key LIKE 'planner:%'").fetchall():
            try:
                ref=json.loads(item['value'])
                if not ref.get('task') or ref.get('state') in ('done','withdrawn'):continue
                rid=item['key'].split(':')[1];current=workflows.status(s,rid)
                if current.get('device')!=s.device:continue
                if current['intent'] and item['key'].endswith(':'+current['execution_revision']):continue
                if not ref.get('withdrawal'):
                    ref['withdrawal']=team.new_instruction('cancel_task',{'task_id':ref['task']})['instruction_id'];s.put(item['key'],ref)
                team.lifecycle('cancel_task',{'task_id':ref['task']},ref['withdrawal'])
                if self.call('task_settled',{'task_id':ref['task']}).get('stopped'):
                    ref['state']='withdrawn';s.put(item['key'],ref)
            except Exception:
                continue # network/unknown admission remains durable for next tick


@cooperative
def resume_reviewed(store,rid,inspection,revision,confirmed,origin,call=None):
    if confirmed is not True:raise Blocked('须当前Agent明确重新审阅后继续规划')
    current=workflows.status(store,rid);binding=require_readiness(store,project=current['project'])
    key='planner:'+rid+':'+current['execution_revision'];ref=store.get(key,{})
    expected='portable-plan:'+digest(canonical([store.device,rid,current['execution_revision'],ref.get('repo_sha256'),binding['epoch']]).encode())
    if ref and ref.get('request')!=expected:raise Blocked('原规划任务绑定已变更，须按新范围审阅，不复活旧epoch')
    if ref.get('resume_review')==inspection:return {'requirement':rid,'task':ref['task'],'existing':True}
    if current['state'] not in ('needs_engineer_plan','needs_task_adjustment') or ref.get('state')!='blocked' or not ref.get('task'):raise Blocked('仅恢复当前需求三轮停下的原规划任务')
    row=store.db.execute("SELECT body FROM records WHERE id=? AND kind='engineering_inspection'",(inspection,)).fetchone()
    checked=json.loads(row[0]) if row else {}
    repo=workflows.repo_version(store,current['project'])
    if checked.get('requirement')!=rid or checked.get('revision')!=revision or revision!=current['revision'] or checked.get('epoch')!=binding['epoch'] or checked.get('repo',{}).get('sha256')!=repo['sha256'] or ref['repo_sha256']!=repo['sha256']:raise Blocked('新审阅、需求或repo版本不符，未继续旧任务')
    call=call or transport(store.get('team_config',{}));view=call('get_task',{'task_id':ref['task']})
    if view.get('caller','portable:'+store.device)!='portable:'+store.device or not call('task_settled',{'task_id':ref['task']}).get('stopped'):raise Blocked('原规划任务身份或停稳未确认')
    if view.get('status') not in ('awaiting_acceptance','interrupted'):raise Blocked('原规划任务状态不支持正式续接')
    # Reclassify the last owned result with current strict schema; never log raw reply.
    try:
        previous=call('get_task_result',{'task_id':ref['task'],'include_log_tail':False,'max_result_bytes':65536})
        answer=previous.get('final_answer') or {}
        if previous.get('execution_status')=='done' and previous.get('exit_code')==0 and previous.get('run_id')==view.get('run_id') and not answer.get('truncated'):
            try:validate_result(json.loads(answer.get('text','')),{"revision":ref['inspection_revision']},{"sha256":ref['repo_sha256'],"workspace":ref['workspace']})
            except PlanFault as error:ref['last_fault']=str(error)
            except (ValueError,TypeError):ref['last_fault']='JSON_PARSE_ERROR: 上轮不是完整纯JSON对象'
    except Exception:ref.setdefault('last_fault','PREVIOUS_RESULT_UNAVAILABLE: 旧结果暂不可核实，未冒用旧会话')
    import uuid
    instruction=ref['request']+':reviewed-resume:'+uuid.uuid4().hex
    history=ref.setdefault('reviewed_restarts',[]);history.append({'inspection':inspection,'origin':origin,'previous_failed_run':ref.get('failed_run'),'previous_error':ref.get('last_fault'),'previous_attempts':ref.get('attempts')})
    ref.update(state='submitted',attempts=0,resume_review=inspection,inspection=inspection,inspection_revision=revision,repair={'key':instruction,'arguments':{'task_id':ref['task'],'idempotency_key':instruction,'spec':'当前工程Agent已重新审阅；继续同一只读规划，不新建任务/需求。此前失败：'+ref.get('last_fault','unknown')+'\n'+schema_instruction(revision,repo['sha256'])}})
    ref.pop('failed_run',None);ref.pop('verified',None);store.put(key,ref)
    return {'requirement':rid,'task':ref['task'],'reviewed_resume_saved':True,'new_task_created':False,'execution_started':False}


def register(subparser):
    p=subparser.add_parser('planner');p.add_argument('action',choices=['tick','verify','resume']);p.add_argument('requirement',nargs='?');p.add_argument('--sha');p.add_argument('--inspection');p.add_argument('--revision');p.add_argument('--confirm',action='store_true');p.set_defaults(planner_run=run);return p

def run(args):
    s=Store(args.root)
    try:
        if args.action=='tick':return Planner(s).tick()
        if args.action=='resume':return resume_reviewed(s,args.requirement,args.inspection,args.revision,args.confirm,'local-cli:'+s.device)
        current=workflows.status(s,args.requirement)
        if current['state'] not in ('needs_engineer_plan','needs_task_adjustment') or workflows.repo_version(s,current['project'])['sha256']!=args.sha:raise Blocked('需求或repo版本变化，只读规划不能验收')
        return {'unchanged':True}
    finally:s.db.close()

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);register(p.add_subparsers(required=True))
    try:print(canonical(run(p.parse_args(argv))));return 0
    except Exception:print('{"status":"BLOCKED","reason":"工程规划尚未验证"}');return 2

if __name__=='__main__':raise SystemExit(main())
