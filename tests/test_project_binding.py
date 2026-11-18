"""SD32: real isolated Git, synthetic existing grants and read-only task service."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from portable.core import Store, Blocked, canonical
from portable import team, workflows
from portable import project_binding as pb

class Binding(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name).resolve()
        self.s=Store(self.base/'app');self.repo=self.base/'repo';self.other=self.base/'repo-extra'
        for p in (self.repo,self.other):
            p.mkdir();self.git(p,'init','-q');self.git(p,'-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','fixture')
        self.s.register('fixture',self.repo,'demo')
        self.server=self.base/'server.py';self.server.write_text('# synthetic service')
        self.s.put('team_config',{'caller':'portable:'+self.s.device,'server':str(self.server)})
        self.s.put('execution','active')
        self.binding=dict(project='demo',workspace=str(self.repo),device=self.s.device,scope='new_tasks_only',epoch='epoch',fingerprint=team.readiness_fingerprint(self.s))
        self.s.put('readiness',self.binding);self.s.put('readiness_projects',{'demo':self.binding})
    def git(self,p,*args):return subprocess.check_output(['git','-C',str(p),*args],stderr=subprocess.DEVNULL).decode()
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def snapshot(self):
        tables=[r[0] for r in self.s.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        database={t:sorted(repr(tuple(r)) for r in self.s.db.execute('SELECT * FROM '+t)) for t in tables}
        files={str(p.relative_to(self.base)):p.read_bytes() for p in self.base.rglob('*') if p.is_file() and not p.is_symlink() and p.name not in ('state.sqlite','state.sqlite-wal','state.sqlite-shm')}
        return database,files
    def query(self,**kw):return pb.query(self.s,'demo',**kw)
    def task(self):
        key='fixture-request'
        body=dict(project='demo',workspace=str(self.repo),device=self.s.device,binding_epoch='epoch')
        with self.s.db:self.s.db.execute('INSERT INTO outbox (id,body,state,reference) VALUES (?,?,?,?)',(key,canonical(body),'submitted','task'))
        return key
    def service(self,op,args):
        self.assertEqual(op,'get_task');self.assertEqual(args,{'task_id':'task'})
        return dict(task_id='task',caller='portable:'+self.s.device,workspace=str(self.repo),run_id='run')
    def test_readonly_and_normal_registered_repo(self):
        before=self.snapshot()
        for _ in range(2):
            r=self.query();self.assertEqual(r['status'],'READY');self.assertEqual(r['task']['state'],'unknown');self.assertFalse(r['coverage']['arbitrary_tool_writes']);self.assertFalse(r['coverage']['path_replacement_atomic'])
        self.assertEqual(before,self.snapshot())
    def test_wrong_repo_prefix_clone_symlink_and_unknown(self):
        clone=self.base/'clone';subprocess.check_call(['git','clone','-q',str(self.repo),str(clone)])
        link=self.base/'link';link.symlink_to(self.other)
        for p in (self.other,clone,link,self.base/'missing'):
            with self.subTest(p=p):
                before=self.snapshot();self.assertEqual(self.query(workspace=p)['status'],'BLOCKED');self.assertEqual(before,self.snapshot())
    def test_worktree_requires_existing_candidate_and_task_service(self):
        wt=self.base/'candidate';self.git(self.repo,'worktree','add','-qb','candidate',str(wt))
        self.assertEqual(self.query(workspace=wt)['status'],'BLOCKED')
        key=self.task();self.s.put('candidate:engineering:'+key,{'workspace':str(wt)})
        def call(op,args):return {**self.service(op,args),'workspace':str(wt)}
        self.assertEqual(self.query(workspace=wt,task='task',run='run',call=call)['status'],'READY')
        self.assertEqual(self.query(workspace=wt,task='task',run='run')['status'],'BLOCKED')
    def test_stale_worktree_registration_does_not_block_own_repo(self):
        # 见 DECISIONS 2026-11-14：仓库里登记着已经删掉的工作目录时，以前一碰就报错，连项目自己的仓库也拦
        gone=self.base/'gone-wt';self.git(self.repo,'worktree','add','-qb','gone',str(gone))
        import shutil;shutil.rmtree(gone)  # 只剩登记
        self.assertIn('prunable',self.git(self.repo,'worktree','list','--porcelain'))
        before=self.snapshot()
        self.assertEqual(self.query()['status'],'READY')  # 项目自己的仓库放行
        clone=self.base/'clone';subprocess.check_call(['git','clone','-q',str(self.repo),str(clone)])
        for p in (self.other,clone,self.repo/'sub'):  # 别的仓库、克隆、子目录仍然拦
            if p==self.repo/'sub':p.mkdir()
            with self.subTest(p=p):self.assertEqual(self.query(workspace=p)['status'],'BLOCKED')
        self.assertEqual(before[0],self.snapshot()[0])  # 只读：数据库没变
        self.assertIn('prunable',self.git(self.repo,'worktree','list','--porcelain'))  # 也不替用户清登记

    def test_not_bound_project_message_is_plain(self):
        self.s.register('fixture2',self.other,'other')
        r=pb.query(self.s,'other')
        self.assertEqual(r['status'],'BLOCKED')
        self.assertTrue(any('不在「可开工」状态' in x and 'demo' in x for x in r['reasons']),r['reasons'])

    def test_task_run_epoch_and_source_fail_closed(self):
        self.task()
        self.assertEqual(self.query(task='task',run='run',call=self.service)['status'],'READY')
        for kw in ({'task':'wrong','run':'run'},{'task':'task','run':'wrong'},{'task':'task','run':'run','epoch':'old'}):
            before=self.snapshot();self.assertEqual(self.query(call=self.service,**kw)['status'],'BLOCKED');self.assertEqual(before,self.snapshot())
        self.assertEqual(self.query(task='task',run='run')['task']['state'],'unknown')
    def test_registry_conflict_and_pause(self):
        self.s.put('registry',[dict(id='demo',path=str(self.other))]);self.assertEqual(self.query()['status'],'BLOCKED')
        self.s.put('registry',[]);self.s.put('automation:v1',{'paused':True});self.assertEqual(self.query()['status'],'BLOCKED')
    def test_legacy_query_does_not_promote(self):
        self.s.put('readiness_projects',{});before=self.snapshot();self.assertEqual(self.query()['status'],'READY');self.assertEqual(before,self.snapshot())
    def test_engineering_entry_blocks_before_record(self):
        rid=workflows.start(self.s,'demo','fixture',dict.fromkeys(('user','problem','flow','scope','acceptance'),'fixture'));workflows.answer(self.s,rid,'implement',True)
        before=self.snapshot()
        with self.assertRaises(Blocked):workflows.engineering_context(self.s,rid,'fixture',workspace=self.other)
        self.assertEqual(before,self.snapshot())
        with self.assertRaises(Blocked):workflows.engineering_context(self.s,rid,'fixture',task='task',run='run')
        self.assertEqual(before,self.snapshot())
        self.assertIn('entry_binding',workflows.engineering_context(self.s,rid,'fixture'))
    def test_actual_cli(self):
        r=subprocess.run([sys.executable,'-m','portable.workflows','--root',str(self.s.root),'workflow','binding','demo'],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr);self.assertEqual(json.loads(r.stdout)['schema'],'portable.project-binding/v1')
    def test_strong_atomic_requirement_rejected(self):
        with self.assertRaises(Blocked):pb.require(self.s,'demo',strong_atomic=True)

    def test_no_team_readonly_and_unknown_project(self):
        self.s.put('readiness',{});self.s.put('readiness_projects',{});self.s.put('execution','unbound')
        before=self.snapshot();self.assertEqual(self.query()['status'],'READY')
        self.assertEqual(pb.query(self.s,'unknown')['status'],'BLOCKED');self.assertEqual(before,self.snapshot())
        with self.assertRaises(Blocked):pb.require(self.s,'demo')
    def test_write_entries_reject_before_queue_or_link(self):
        rid=workflows.start(self.s,'demo','fixture',dict.fromkeys(('user','problem','flow','scope','acceptance'),'fixture'));workflows.answer(self.s,rid,'implement',True)
        inspected=workflows.engineering_context(self.s,rid,'fixture')
        reviewed=workflows.engineering_review(self.s,rid,inspected['inspection'],inspected['revision'],'fixture plan',['test -d .git'],True,'fixture')
        self.s.put('registry',[dict(id='demo',path=str(self.other))]);before=self.snapshot()
        with self.assertRaises(Blocked):workflows.submit_request(self.s,reviewed['request'],lambda *_:self.fail('must not call transport'))
        self.assertEqual(before,self.snapshot())
    def test_verified_task_engineering_entry_and_service_mismatch(self):
        self.task()
        rid=workflows.start(self.s,'demo','fixture',dict.fromkeys(('user','problem','flow','scope','acceptance'),'fixture'));workflows.answer(self.s,rid,'implement',True)
        r=workflows.engineering_context(self.s,rid,'fixture',task='task',run='run',call=self.service)
        self.assertEqual(r['entry_binding']['task']['state'],'verified')
        reviewed=workflows.engineering_review(self.s,rid,r['inspection'],r['revision'],'fixture',['test -d .git'],True,'fixture',call=self.service)
        self.assertIn('request',reviewed)
        for changed in ({'caller':'other'},{'workspace':str(self.other)},{'task_id':'other'},{'run_id':None}):
            def call(op,args):return {**self.service(op,args),**changed}
            before=self.snapshot();self.assertEqual(self.query(task='task',run='run',call=call)['status'],'BLOCKED');self.assertEqual(before,self.snapshot())
    def test_path_replacement_and_stale_original_epoch(self):
        self.task()
        initial=self.query();self.assertFalse(initial['coverage']['path_replacement_atomic'])
        moved=self.base/'moved';self.repo.rename(moved);self.repo.symlink_to(self.other)
        before=self.snapshot();self.assertEqual(self.query()['status'],'BLOCKED');self.assertEqual(before,self.snapshot())
        self.repo.unlink();moved.rename(self.repo)
        newer={**self.binding,'epoch':'new'};self.s.put('readiness',newer);self.s.put('readiness_projects',{'demo':newer})
        self.assertEqual(self.query(task='task',run='run',call=self.service)['status'],'BLOCKED')
    def test_forged_planner_reference_and_invalid_registry_shape(self):
        from portable.core import digest
        rid=workflows.start(self.s,'demo','fixture',dict.fromkeys(('user','problem','flow','scope','acceptance'),'fixture'))
        revision='fixture-revision';sha='fixture-sha'
        request='portable-plan:'+digest(canonical([self.s.device,rid,revision,sha,'epoch']).encode())
        self.s.put('planner:'+rid+':'+revision,dict(task='task',project='demo',request=request,workspace=str(self.repo),repo_sha256=sha,binding_epoch='epoch'))
        self.assertEqual(self.query(task='task',run='run',call=self.service)['status'],'BLOCKED')
        self.s.put('registry',{'unknown':'shape'});self.assertEqual(self.query()['status'],'BLOCKED')
    def test_cli_inspect_unknown_task_blocks_without_side_effects(self):
        rid=workflows.start(self.s,'demo','fixture',dict.fromkeys(('user','problem','flow','scope','acceptance'),'fixture'));workflows.answer(self.s,rid,'implement',True)
        self.task();before=self.snapshot()
        r=subprocess.run([sys.executable,'-m','portable.workflows','--root',str(self.s.root),'workflow','inspect',rid,'--task','task','--run','run'],capture_output=True,text=True)
        self.assertEqual(r.returncode,2,r.stdout+r.stderr);self.assertEqual(json.loads(r.stdout)['status'],'BLOCKED');self.assertEqual(before,self.snapshot())

    def test_configured_service_full_engineering_chain(self):
        from unittest.mock import patch
        from tests.test_multiproject import Executor
        self.task();backend=Executor(self.s);self.addCleanup(backend.close)
        def call(op,args):
            if op=='get_task' and args['task_id']=='task':return self.service(op,args)
            return backend(op,args)
        with patch('portable.team.transport',return_value=call):
            rid=workflows.start(self.s,'demo','fixture',dict.fromkeys(('user','problem','flow','scope','acceptance'),'fixture'));workflows.answer(self.s,rid,'implement',True)
            inspected=workflows.engineering_context(self.s,rid,'fixture',task='task',run='run')
            reviewed=workflows.engineering_review(self.s,rid,inspected['inspection'],inspected['revision'],'fixture plan',['test -f delivered.txt'],True,'fixture')
            result=workflows.submit_request(self.s,reviewed['request'])
            self.assertIn(result['team']['task_id'],backend.tasks)
            self.assertTrue(any(op=='create_task' for op,_ in backend.calls))

    def test_cli_binding_never_initializes_or_rebinds_store(self):
        for module in ('portable','portable.workflows'):
            self.s.put('host_binding','foreign-host');before=self.snapshot()
            for _ in range(2):
                r=subprocess.run([sys.executable,'-m',module,'--root',str(self.s.root),'workflow','binding','demo'],capture_output=True,text=True)
                self.assertIn('BLOCKED',r.stdout+r.stderr)
            self.assertEqual(before,self.snapshot())
        missing=self.base/'not-initialized'
        subprocess.run([sys.executable,'-m','portable.workflows','--root',str(missing),'workflow','binding','demo'],capture_output=True,text=True)
        self.assertFalse(missing.exists())

    def test_cli_configured_read_service_and_repeated_queries(self):
        # Synthetic SDK-process boundary, actual team.transport + CLI subprocess.
        # It accepts only get_task: no lifecycle tool can pass this fixture.
        view=self.service('get_task',{'task_id':'task'})
        bridge=self.base/'synthetic-sdk-python'
        bridge.write_text('#!'+sys.executable+'\nimport json,sys\nrequest=json.load(sys.stdin)\nassert request["tool"]=="get_task"\nassert request["arguments"]=={"task_id":"task"}\nprint(json.dumps({"ok":True,"result":'+repr(view)+'}))\n')
        bridge.chmod(0o700)
        self.s.put('team_config',{'python':str(bridge),'caller':'portable:'+self.s.device,'server':str(self.server)})
        self.binding['fingerprint']=team.readiness_fingerprint(self.s)
        self.s.put('readiness',self.binding);self.s.put('readiness_projects',{'demo':self.binding});self.task()
        rid=workflows.start(self.s,'demo','fixture',dict.fromkeys(('user','problem','flow','scope','acceptance'),'fixture'));workflows.answer(self.s,rid,'implement',True)
        before=self.snapshot()
        for module in ('portable','portable.workflows'):
            for _ in range(2):
                r=subprocess.run([sys.executable,'-m',module,'--root',str(self.s.root),'workflow','binding','demo','--task','task','--run','run'],capture_output=True,text=True)
                self.assertEqual(r.returncode,0,r.stderr+r.stdout);self.assertEqual(json.loads(r.stdout)['task']['state'],'verified')
        self.assertEqual(before,self.snapshot())
        r=subprocess.run([sys.executable,'-m','portable.workflows','--root',str(self.s.root),'workflow','inspect',rid,'--task','task','--run','run'],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr);inspection=json.loads(r.stdout)
        plan=self.base/'plan.txt';plan.write_text('fixture plan');commands=self.base/'commands.json';commands.write_text('["test -f delivered.txt"]')
        r=subprocess.run([sys.executable,'-m','portable.workflows','--root',str(self.s.root),'workflow','review',rid,'--inspection',inspection['inspection'],'--revision',inspection['revision'],'--plan-file',str(plan),'--commands',str(commands),'--confirm'],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr);self.assertIn('request',json.loads(r.stdout))

    def test_readonly_reauthorization_resolve_branch(self):
        from portable.core import digest
        from portable.verification import review
        key=self.task();mvp=dict.fromkeys(('user','problem','flow','scope','acceptance'),'fixture')
        reviewed=review(self.s,mvp,['test -d .git'])
        row=self.s.db.execute('SELECT * FROM outbox WHERE id=?',(key,)).fetchone()
        body=json.loads(row['body']);body.update(binding_epoch='old',mvp=mvp,verification_commands=['test -d .git'])
        with self.s.db:self.s.db.execute('UPDATE outbox SET body=? WHERE id=?',(canonical(body),key))
        proof=dict(device=self.s.device,project='demo',epoch='epoch',workspace=str(self.repo),fingerprint=self.binding['fingerprint'],authority=None,request_sha256=digest(canonical(body).encode()),task='task',old_epoch='old',review=reviewed)
        self.s.put('supervision-reauthorization:'+key,proof);before=self.snapshot()
        self.assertEqual(self.query(task='task',run='run',call=self.service)['status'],'READY');self.assertEqual(before,self.snapshot())
        proof['review']='unknown';self.s.put('supervision-reauthorization:'+key,proof);before=self.snapshot()
        self.assertEqual(self.query(task='task',run='run',call=self.service)['status'],'BLOCKED');self.assertEqual(before,self.snapshot())

    def test_context_gate_rejection_is_readonly_and_same_instruction_retryable(self):
        from portable.context_server import Context,enroll
        enroll(self.s,'fixture');ctx=Context(self.s,'fixture')
        rid=workflows.start(self.s,'demo','fixture',dict.fromkeys(('user','problem','flow','scope','acceptance'),'fixture'));workflows.answer(self.s,rid,'implement',True)
        args={'id':rid,'files':[],'instruction_id':'retry-same'}
        self.s.put('registry',[dict(id='demo',path=str(self.other))]);before=self.snapshot()
        with self.assertRaises(Blocked):ctx.call('engineering_context',args)
        self.assertEqual(before,self.snapshot())
        self.s.put('registry',[])
        result=ctx.call('engineering_context',args);self.assertIn('inspection',result)
        self.assertEqual(result,ctx.call('engineering_context',args))
        with self.assertRaises(Blocked):ctx.call('engineering_context',{**args,'files':['different.md']})

    def test_context_execution_unknown_and_old_pending_never_replay(self):
        from portable.context_server import Context,enroll
        from portable.core import digest
        from unittest.mock import patch
        identity=enroll(self.s,'fixture')['identity'];ctx=Context(self.s,'fixture')
        rid=workflows.start(self.s,'demo','fixture',dict.fromkeys(('user','problem','flow','scope','acceptance'),'fixture'));workflows.answer(self.s,rid,'implement',True)
        args={'id':rid,'files':[],'instruction_id':'unknown'};calls=[]
        def uncertain(*_):
            calls.append(True);self.s.record('fixture_effect',{'fixture':True});raise Blocked('execution unknown')
        with patch.object(ctx,'_call',side_effect=uncertain):
            with self.assertRaises(Blocked):ctx.call('engineering_context',args)
            with self.assertRaises(Blocked):ctx.call('engineering_context',args)
            with self.assertRaises(Blocked):ctx.call('engineering_context',{**args,'files':['changed']})
        self.assertEqual(calls,[True])
        key='context_instruction:'+digest((identity+':unknown').encode());pending=self.s.get(key)
        self.assertEqual(pending['state'],'pending')
        before=self.snapshot()
        with self.assertRaises(Blocked):ctx.call('engineering_context',args)
        self.assertEqual(before,self.snapshot());self.assertEqual(pending,self.s.get(key))
