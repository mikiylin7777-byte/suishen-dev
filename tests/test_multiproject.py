"""Public API regressions; injected transport executes real isolated fixture work/checks."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
sys.dont_write_bytecode=True
from portable.core import Store,Blocked,canonical,digest
from portable import team,workflows,automation,candidates
from portable.context_server import Context,enroll
from portable.planner import Planner
from portable.tools import project_choices

class Executor:
    def __init__(self,store):self.store=store;self.tasks={};self.requests={};self.calls=[];self.pending={}
    def poll(self):
        for key,item in list(self.pending.items()):
            if item['process'].poll() is None:continue
            out,_=item['process'].communicate();evidence=json.loads(out);task=self.tasks[item['task']]
            task['evidence']=evidence;task['verify']={'passed':all(r['exit_code']==0 for r in evidence),'ran_at':str(time.time())}
            self.requests[key]={'task_id':item['task'],'state':'completed'};del self.pending[key]
    def close(self):
        for item in self.pending.values():
            process=item['process']
            if process.poll() is None:process.terminate()
            process.communicate(timeout=10)
    def __call__(self,op,args):
        self.poll()
        self.calls.append((op,dict(args)))
        if op=='__capabilities__':return {'tools':['create_task','get_task','get_task_result','lookup_request','list_tasks','task_settled']}
        if op=='list_tasks':return {'tasks':list(self.tasks.values())}
        if op=='lookup_request':return dict(found=args['idempotency_key'] in self.requests,**self.requests.get(args['idempotency_key'],{}))
        if op=='create_task':
            key=args['idempotency_key']
            if key in self.requests:return {'task_id':self.requests[key]['task_id']}
            task='task-'+str(len(self.tasks)+1);workspace=Path(args['workspace']);planning=args['title'].startswith('只读工程审阅')
            if planning:
                data=json.loads(args['spec'].split('需求数据：\n')[-1])
                answer=canonical({'requirement_revision':data['requirement_revision'],'repo_sha256':data['repo_sha256'],'plan':'在隔离候选写入delivered.txt并检查文件','verification_commands':['test -f delivered.txt'],'review_confirmed':True})
                code="from pathlib import Path;assert Path('README.md').is_file()"
            else:answer='fixture development done';code="from pathlib import Path;Path('delivered.txt').write_text('fixture complete')"
            result=subprocess.run([sys.executable,'-I','-c',code],cwd=workspace,capture_output=True)
            self.tasks[task]={'task_id':task,'caller':'portable:'+self.store.device,'workspace':str(workspace),'status':'awaiting_acceptance','run_id':'run-'+task,'verify':{'passed':False},'commands':args['acceptance_criteria'],'exit_code':result.returncode,'final':answer,'planning':planning}
            self.requests[key]={'task_id':task,'state':'completed'};return {'task_id':task}
        task=self.tasks[args['task_id']]
        if op=='get_task':return json.loads(canonical(task))
        if op=='task_settled':return {'stopped':not any(p['task']==task['task_id'] for p in self.pending.values()),'status':task['status'],'run_id':task['run_id']}
        if op=='get_task_result':return {'task_id':task['task_id'],'run_id':task['run_id'],'execution_status':'done','exit_code':task['exit_code'],'final_answer':{'text':task['final'],'truncated':False},'verification':{'commands':task.get('evidence',[])}}
        if op=='reverify_task':
            key=args['idempotency_key']
            if key not in self.requests:
                code="import json,subprocess,sys;commands=json.loads(sys.argv[1]);print(json.dumps([{'cmd':c,'exit_code':subprocess.run(c,shell=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).returncode} for c in commands]))"
                process=subprocess.Popen([sys.executable,'-I','-c',code,canonical(task['commands'])],cwd=task['workspace'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                self.pending[key]={'process':process,'task':task['task_id']};self.requests[key]={'task_id':task['task_id'],'state':'pending'}
            return {'task_id':task['task_id'],'requested':True}
        elif op=='accept_task':
            if task['exit_code']!=0 or task['verify'].get('passed') is not True:raise AssertionError('cannot accept failed fixture')
            task['status']='completed'
        elif op=='continue_task':
            if args.get('verification_commands'):task['commands']=args['verification_commands']
            result=subprocess.run([sys.executable,'-c',"from pathlib import Path;Path('adjusted.txt').write_text('revised')"],cwd=task['workspace'],capture_output=True)
            task.update(run_id=task['run_id']+'-continued',exit_code=result.returncode,verify={'passed':False})
        elif op=='cancel_task':task['status']='cancelled';return {'task_id':task['task_id'],'cancel_effective':True}
        else:raise AssertionError(op)
        self.requests[args['idempotency_key']]={'task_id':task['task_id'],'state':'completed'}
        return {'task_id':task['task_id'],'requested':True}

class MultiProject(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve();self.s=Store(self.root/'app');self.addCleanup(self.s.db.close)
        self.server=self.root/'server.py';self.server.write_text('# local fixture transport identity')
        self.s.put('team_config',{'python':sys.executable,'server':str(self.server),'caller':'portable:'+self.s.device})
        self.backend=Executor(self.s);self.addCleanup(self.backend.close)
        for project in ('alpha','beta','unbound'):candidates.create_managed(self.s,project,project)
        enroll(self.s,'client');self.ctx=Context(self.s,'client',self.backend)
    def bind(self,project):return team.bind_new_tasks(self.s,self.backend,project)
    def requirement(self,project):
        rid=workflows.start(self.s,project,project+' feature',dict(user='fixture',problem=project+' feature',flow='write then verify',scope='one file',acceptance='delivered.txt exists'))
        workflows.answer(self.s,rid,'implement',True);return rid
    def submit(self,project):
        rid=self.requirement(project);view=self.ctx.call('engineering_context',{'id':rid,'files':[],'instruction_id':'inspect-'+rid})
        reviewed=self.ctx.call('engineering_review',{'id':rid,'inspection':view['inspection'],'revision':view['revision'],'plan':'write delivered.txt in isolated candidate','commands':['test -f delivered.txt'],'confirmed':True,'instruction_id':'review-'+rid})
        result=self.ctx.call('request_submit',{'request':reviewed['request'],'instruction_id':'submit-'+rid})
        return rid,reviewed,result['team']['task_id']
    def finish(self):
        for _ in range(40):
            automation.Reconciler(self.s,self.backend).tick();time.sleep(.02)
    def test_two_projects_view_repeat_bind_and_single_submission_both_receive(self):
        a=self.bind('alpha');ra,pa,ta=self.submit('alpha');project_choices(self.s,'beta')
        self.assertEqual(team.require_readiness(self.s,project='alpha')['epoch'],a['epoch'])
        with self.assertRaises(Blocked):self.ctx.call('engineering_context',{'id':self.requirement('beta'),'files':[],'instruction_id':'beta-unbound'})
        b=self.bind('beta');rb,pb,tb=self.submit('beta');self.assertTrue(self.bind('beta')['existing']);self.assertEqual(self.bind('alpha')['epoch'],a['epoch'])
        project_choices(self.s,'beta');self.ctx.call('request_submit',{'request':pa['request'],'instruction_id':'repeat-alpha'})
        self.finish()
        self.assertEqual(len(self.backend.tasks),2)
        for project,key,task in [('alpha',pa['queue_key'],ta),('beta',pb['queue_key'],tb)]:
            self.assertEqual(self.backend.tasks[task]['status'],'completed');self.assertEqual(automation.state(self.s)['links'][key]['reception']['state'],'received');self.assertTrue((self.s.root/'代码'/project/'delivered.txt').is_file())
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM records WHERE kind='delivery'").fetchone()[0],2)
    def test_two_project_planner_and_scoped_cancel(self):
        self.bind('alpha');self.bind('beta');ra=self.requirement('alpha');rb=self.requirement('beta')
        for _ in range(25):
            Planner(self.s,self.backend).tick();time.sleep(.02)
        self.assertEqual(sum(t['planning'] for t in self.backend.tasks.values()),2)
        records={json.loads(r['body'])['project']:(r['id'],json.loads(r['body'])) for r in self.s.db.execute("SELECT id,body FROM records WHERE kind='original_intent'")}
        self.assertEqual(set(records),{'alpha','beta'})
        alpha,body=records['alpha'];project_choices(self.s,'beta');self.ctx.call('request_cancel',{'request':alpha,'instruction_id':'cancel-alpha'})
        self.finish();task=self.s.db.execute('SELECT reference FROM outbox WHERE id=?',(body['queue_key'],)).fetchone()[0];self.assertEqual(self.backend.tasks[task]['status'],'cancelled')
        beta=records['beta'][1];self.assertEqual(automation.state(self.s)['links'][beta['queue_key']]['reception']['state'],'received')
        self.assertEqual(len(self.backend.tasks),4)
    def test_project_revoke_path_epoch_and_unknown_task_do_not_affect_other_project(self):
        self.bind('alpha');ra,pa,ta=self.submit('alpha');self.bind('beta');rb,pb,tb=self.submit('beta')
        revoked=team.revoke_new_tasks(self.s,'alpha');self.assertEqual(revoked['revoked'],'alpha')
        with self.assertRaises(Blocked):team.Team(self.s,self.backend).new_instruction('reverify_task',{'task_id':ta})
        self.finish();self.assertNotEqual(self.backend.tasks[ta]['status'],'completed');self.assertEqual(self.backend.tasks[tb]['status'],'completed')
        with self.assertRaises(Blocked):team.require_readiness(self.s,project='unbound')
        with self.assertRaises(Blocked):team.require_readiness(self.s,self.s.root/'代码/alpha',project='beta')
        new=self.root/'moved-alpha';shutil.copytree(self.s.root/'代码/alpha',new);self.s.register('alpha',new,'alpha');self.bind('alpha')
        with self.assertRaises(Blocked):team.Team(self.s,self.backend).submit(pa['queue_key'],str(new))
        # A caller-owned but unbound source task cannot borrow beta's selected scope.
        unknown=self.backend('create_task',{'title':'unknown','spec':'fixture','workspace':str(self.s.root/'代码/unbound'),'acceptance_criteria':['test -f delivered.txt'],'idempotency_key':'unknown'})['task_id']
        project_choices(self.s,'beta')
        with self.assertRaises(Blocked):team.Team(self.s,self.backend).new_instruction('cancel_task',{'task_id':unknown})
    def test_global_fingerprint_pause_upgrade_and_restore_fail_closed(self):
        self.bind('alpha');_,pa,ta=self.submit('alpha');self.bind('beta');_,pb,tb=self.submit('beta')
        automation.pause(self.s)
        for project in ('alpha','beta'):
            with self.assertRaises(Blocked):team.require_readiness(self.s,project=project)
        automation.pause(self.s,False);self.server.write_text('# changed actual server bytes')
        self.finish()
        for project in ('alpha','beta'):
            with self.assertRaises(Blocked):team.require_readiness(self.s,project=project)
        self.assertNotEqual(self.backend.tasks[ta]['status'],'completed');self.assertNotEqual(self.backend.tasks[tb]['status'],'completed')
        self.s.db.execute('PRAGMA wal_checkpoint(FULL)');copy=self.root/'copied';shutil.copytree(self.s.root,copy)
        restored=Store(copy);self.addCleanup(restored.db.close);self.assertFalse(restored.get('readiness_projects',{}))
        for project in ('alpha','beta'):
            with self.assertRaises(Blocked):team.require_readiness(restored,project=project)
        self.s.put('execution','update-paused')
        with self.assertRaises(Blocked):team.require_readiness(self.s,project='beta')
    def test_legacy_single_project_promotes_only_proven_binding(self):
        self.bind('alpha');old=self.s.get('readiness');
        with self.s.db:self.s.db.execute("DELETE FROM meta WHERE key='readiness_projects'")
        self.s.put('readiness',{k:v for k,v in old.items() if k!='authority'})
        self.assertEqual(team.require_readiness(self.s,project='alpha')['epoch'],old['epoch'])
        self.assertEqual(set(self.s.get('readiness_projects')),{'alpha'})
        with self.assertRaises(Blocked):team.require_readiness(self.s,project='beta')
        self.bind('beta');self.assertEqual(team.require_readiness(self.s,project='alpha')['epoch'],old['epoch'])
    def test_path_change_only_blocks_that_project_and_explicit_recovery_is_reviewed(self):
        self.bind('alpha');_,pa,ta=self.submit('alpha');self.bind('beta');_,pb,tb=self.submit('beta')
        original=self.s.root/'代码/alpha';moved=self.root/'new-alpha';shutil.copytree(original,moved);self.s.register('alpha',moved,'alpha')
        self.finish();self.assertTrue(automation.state(self.s)['links'][pa['queue_key']]['stopped']);self.assertEqual(self.backend.tasks[tb]['status'],'completed')
        self.s.register('alpha',original,'alpha');self.bind('alpha')
        with self.assertRaises(Blocked):automation.resume_supervision(self.s,pa['queue_key'],False,self.backend)
        resumed=automation.resume_supervision(self.s,pa['queue_key'],True,self.backend);self.assertFalse(resumed['new_task_created'])
        self.finish();self.assertEqual(self.backend.tasks[ta]['status'],'completed');self.assertEqual(len(self.backend.tasks),2)
    def test_new_epoch_requires_fresh_scope_review_before_retained_task_resume(self):
        self.bind('alpha');_,planned,task=self.submit('alpha');self.bind('beta');team.revoke_new_tasks(self.s,'alpha');self.finish()
        self.bind('alpha')
        with self.assertRaises(Blocked):team.Team(self.s,self.backend).new_instruction('reverify_task',{'task_id':task})
        resumed=self.ctx.call('request_resume_supervision',{'request':planned['request'],'confirmed':True,'instruction_id':'reviewed-recovery'})
        self.assertTrue(resumed['supervision_resumed']);self.finish();self.assertEqual(self.backend.tasks[task]['status'],'completed');self.assertEqual(len(self.backend.tasks),1)
    def test_global_caller_and_backend_bytes_revoke_all_current_scopes(self):
        for mode in ('caller','backend'):
            with self.subTest(mode=mode):
                helper=MultiProject();helper.setUp()
                try:
                    executable=helper.root/'backend';executable.write_text('owned backend baseline');cfg=helper.s.get('team_config');cfg['environment']={'AI_TEAM_CODEX_TASK':str(executable)};helper.s.put('team_config',cfg)
                    helper.bind('alpha');_,_,ta=helper.submit('alpha');helper.bind('beta');_,_,tb=helper.submit('beta')
                    if mode=='caller':cfg['caller']='portable:not-this-device';helper.s.put('team_config',cfg)
                    else:executable.write_text('changed backend bytes')
                    previous=len(helper.backend.calls);helper.finish();self.assertEqual(len(helper.backend.calls),previous)
                    for project in ('alpha','beta'):
                        with self.assertRaises(Blocked):team.require_readiness(helper.s,project=project)
                    self.assertNotEqual(helper.backend.tasks[ta]['status'],'completed');self.assertNotEqual(helper.backend.tasks[tb]['status'],'completed')
                finally:helper.doCleanups()
    def test_public_prepare_upgrade_requires_rebinding_and_keeps_legacy_map_inert(self):
        from portable.distribution import prepare_update
        from unittest.mock import patch
        self.bind('alpha');self.submit('alpha');self.bind('beta');self.submit('beta');self.finish()
        with patch('portable.team.transport',return_value=self.backend):prepare_update(self.s)
        self.assertFalse(self.s.get('readiness_projects'));self.assertFalse(self.s.get('readiness'))
        self.bind('beta')
        with self.assertRaises(Blocked):team.require_readiness(self.s,project='beta') # pause still authoritative
        automation.pause(self.s,False);team.require_readiness(self.s,project='beta')
        with self.assertRaises(Blocked):team.require_readiness(self.s,project='alpha')
        self.assertTrue(self.s.get('readiness_disabled:upgrade')['projects'])
    def test_scope_adjustment_routes_to_original_project_while_viewing_another(self):
        self.bind('alpha');rid,planned,ta=self.submit('alpha');self.bind('beta');_,_,tb=self.submit('beta');project_choices(self.s,'beta')
        current=workflows.status(self.s,rid);self.ctx.call('requirement_answer',{'id':rid,'field':'scope','value':'add adjusted.txt','expected':current['revision'],'instruction_id':'change-alpha'})
        inspected=self.ctx.call('engineering_context',{'id':rid,'files':[],'instruction_id':'inspect-adjust-alpha'})
        adjusted=self.ctx.call('request_adjust',{'request':planned['request'],'inspection':inspected['inspection'],'revision':inspected['revision'],'plan':'create adjusted.txt in alpha candidate','commands':['test -f adjusted.txt'],'confirmed':True,'instruction_id':'adjust-alpha'})
        self.assertEqual(adjusted['task'],ta);self.finish()
        self.assertEqual(self.backend.tasks[ta]['status'],'completed');self.assertEqual(self.backend.tasks[tb]['status'],'completed')
        self.assertTrue((self.s.root/'代码/alpha/adjusted.txt').is_file());self.assertFalse((self.s.root/'代码/beta/adjusted.txt').exists())
        self.assertEqual(len(self.backend.tasks),2)
        self.assertEqual({args['task_id'] for op,args in self.backend.calls if op=='continue_task'},{ta})
    def test_unsent_withdrawal_and_mvp_revision_never_submit_stale_scope(self):
        self.bind('alpha');self.bind('beta')
        for field,value in [('implement',False),('scope','changed scope')]:
            rid=self.requirement('alpha');view=self.ctx.call('engineering_context',{'id':rid,'files':[],'instruction_id':'inspect-unsent-'+rid})
            reviewed=self.ctx.call('engineering_review',{'id':rid,'inspection':view['inspection'],'revision':view['revision'],'plan':'scoped fixture','commands':['test -f delivered.txt'],'confirmed':True,'instruction_id':'review-unsent-'+rid})
            current=workflows.status(self.s,rid);workflows.answer(self.s,rid,field,value,current['revision'])
            with self.assertRaises(Blocked):team.Team(self.s,self.backend).submit(reviewed['queue_key'],str(self.s.root/'代码/alpha'))
        self.assertEqual(self.backend.tasks,{})
    def test_binding_another_project_does_not_invalidate_exact_owner_approval(self):
        from portable import bots
        from unittest.mock import patch
        self.bind('alpha');_,_,task=self.submit('alpha')
        binding={'task':task,'action':'record_checkpoint','target':'local-checkpoint','scope':{'goal':'alpha checkpoint','decisions':[],'done':[],'remaining':[],'authorization':'local only','evidence':['fixture']},'version':self.backend.tasks[task]['run_id'],'owner':'owner'}
        with patch('portable.team.transport',return_value=self.backend):
            approval=bots.issue_approval(self.s,binding)
            self.ctx.call('project_bind',{'project':'beta','instruction_id':'bind-beta-ui'})
            cfg={'owners':{'feishu':{'fixture-owner':'owner'}}}
            decision=bots.event(self.s,cfg,{'channel':'feishu','identity':'fixture-owner','id':'one','body':'批准 '+approval})
            self.assertTrue(decision['approved'])
            self.assertTrue(bots.approval_execute_checkpoint(self.s,approval,'fixture-agent')['local_action_executed'])
            with self.assertRaises(Blocked):bots.approval_execute_checkpoint(self.s,approval,'repeat-agent')

    def test_adjustment_candidate_cannot_redirect_to_other_project(self):
        self.bind('alpha');rid,planned,task=self.submit('alpha')
        self.bind('beta');_,other,_=self.submit('beta')
        before=workflows.status(self.s,rid)
        workflows.answer(self.s,rid,'scope','adjust fixture',before['revision'])
        candidate=self.s.get('candidate:engineering:'+planned['queue_key'])
        alien=self.s.get('candidate:engineering:'+other['queue_key'])
        candidate['workspace']=alien['workspace'];self.s.put('candidate:engineering:'+planned['queue_key'],candidate)
        records=list(self.s.db.execute('SELECT id FROM records'))
        with self.assertRaises(Blocked):workflows.engineering_context(self.s,rid,'fixture',['README.md'])
        self.assertEqual([tuple(r) for r in records],[tuple(r) for r in self.s.db.execute('SELECT id FROM records')])

    def test_adjustment_rechecks_candidate_after_inspection_before_writes(self):
        self.bind('alpha');rid,planned,task=self.submit('alpha');self.bind('beta');_,other,_=self.submit('beta')
        current=workflows.status(self.s,rid);workflows.answer(self.s,rid,'scope','adjust fixture',current['revision'])
        inspected=workflows.engineering_context(self.s,rid,'fixture',['README.md'],task=task,run=self.backend.tasks[task]['run_id'],call=self.backend)
        self.assertEqual(inspected['entry_binding']['task']['task'],task)
        self.assertIn('README.md',inspected['content'])
        from portable.project_binding import query
        ready=query(self.s,'alpha',workspace=inspected['entry_workspace'],task=task,run=self.backend.tasks[task]['run_id'],call=self.backend)
        self.assertEqual(ready['status'],'READY')
        candidate=self.s.get('candidate:engineering:'+planned['queue_key']);candidate['workspace']=self.s.get('candidate:engineering:'+other['queue_key'])['workspace']
        self.s.put('candidate:engineering:'+planned['queue_key'],candidate)
        tables=[r[0] for r in self.s.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        def state():return {t:[tuple(r) for r in self.s.db.execute('SELECT * FROM '+t)] for t in tables}
        before=state()
        with self.assertRaises(Blocked):workflows.request_adjust(self.s,planned['request'],inspected['inspection'],inspected['revision'],'fixture plan',['test -f adjusted.txt'],True,'fixture',call=self.backend)
        self.assertEqual(before,state())

    def test_real_planning_export_binding_and_adjustment_source(self):
        from portable.project_binding import query
        self.bind('alpha');rid=self.requirement('alpha')
        def service(op,args):
            value=self.backend(op,args)
            return {**value,'status':'running'} if op=='get_task' and value.get('planning') else value
        def launch(requirement):
            Planner(self.s,service).tick()
            key='planner:'+requirement+':'+workflows.status(self.s,requirement)['execution_revision']
            ref=self.s.get(key);self.assertTrue(ref.get('task'),ref)
            return key,ref
        key,ref=launch(rid)
        self.assertNotEqual(ref['workspace'],str(self.s.root/'代码/alpha'))
        args=dict(workspace=ref['workspace'],task=ref['task'],run=self.backend.tasks[ref['task']]['run_id'],call=service)
        self.assertEqual(query(self.s,'alpha',**args)['status'],'READY')
        inspected=self.ctx.call('engineering_context',{'id':rid,'workspace':ref['workspace'],'task':ref['task'],'run':args['run'],'files':['README.md'],'instruction_id':'inspect-planning-export'})
        self.assertIn('README.md',inspected['content'])
        for field,value in (('device','foreign'),('binding_epoch','old'),('request','untrusted')):
            bad={**ref,field:value};self.s.put(key,bad)
            self.assertEqual(query(self.s,'alpha',**args)['status'],'BLOCKED')
        self.s.put(key,ref)
        self.assertEqual(query(self.s,'alpha',**{**args,'task':'wrong'})['status'],'BLOCKED')
        candidate_key='candidate:planning:'+ref['request'];candidate=self.s.get(candidate_key)
        self.s.put(candidate_key,{**candidate,'source':str(self.s.root/'代码/beta')})
        self.assertEqual(query(self.s,'alpha',**args)['status'],'BLOCKED');self.s.put(candidate_key,candidate)
        # A second real plan derives its code from the existing engineering candidate.
        rid2,planned,task=self.submit('alpha');current=workflows.status(self.s,rid2)
        workflows.answer(self.s,rid2,'scope','adjust fixture',current['revision'])
        key2,ref2=launch(rid2)
        args2=dict(workspace=ref2['workspace'],task=ref2['task'],run=self.backend.tasks[ref2['task']]['run_id'],call=service)
        self.assertEqual(query(self.s,'alpha',**args2)['status'],'READY')
        export=self.s.get('candidate:planning:'+ref2['request'])
        self.assertNotEqual(export['source'],export['registered_source'])
        self.s.put('candidate:planning:'+ref2['request'],{**export,'source':export['registered_source']})
        self.assertEqual(query(self.s,'alpha',**args2)['status'],'BLOCKED')
        self.s.put('candidate:planning:'+ref2['request'],export)
        # 两个都完成才停；按时间等（最多 15 秒），机器忙时 30×0.02 秒不够，CI 上曾因此偶发失败（11-14 run 2532、2572）
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            Planner(self.s,self.backend).tick()
            if self.s.get(key,{}).get('state')=='done' and self.s.get(key2,{}).get('state')=='done':break
            time.sleep(.05)
        self.assertEqual(self.s.get(key)['state'],'done')
        self.assertEqual(self.s.get(key2)['state'],'done')
        self.assertEqual(self.s.get(key2)['engineering_task'],task)
