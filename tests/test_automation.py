import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from portable.core import Store, Blocked, canonical, digest
from portable.automation import Reconciler, link, state, save, notice, deliver, pause
from portable.supervisor import Runner, service, incremental, restore_incremental
from portable.backup_git import backup as git_backup, git
from portable.bots import event, issue_approval, setup_private, diagnose, notification
from portable.migration import enroll

class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.s=Store(self.base/'app');self.addCleanup(self.s.db.close)
        home=patch.dict(os.environ,{'PORTABLE_HOME':str(self.base/'home')});home.start();self.addCleanup(home.stop)  # 跑后台每日任务会清 ~/.upgrade-*，必须用临时家目录
        self.project=self.base/'project';self.project.mkdir();(self.project/'a.txt').write_text('one')
        self.pid=self.s.register('fixture',self.project)
        self.binding={'epoch':'epoch','project':self.pid,'workspace':str(self.project),'device':self.s.device}
        self.s.put('readiness',self.binding);self.s.put('execution','active')
        self.body={'device':self.s.device,'binding_epoch':'epoch','workspace':str(self.project),
            'verification_commands':['python check.py'],'mvp':{'acceptance':'check'}}
        with self.s.db:self.s.db.execute('INSERT INTO outbox VALUES (?,?,?,?)',('request',canonical(self.body),'submitted','task'))
        self.ready=patch('portable.automation.require_readiness',return_value=self.binding);self.ready.start();self.addCleanup(self.ready.stop)
        self.initial_link()
        self.view={'task_id':'task','workspace':str(self.project),'status':'awaiting_acceptance','run_id':'run','verify':{'passed':False,'status':'failed','failed_index':1}}
        self.calls=[];self.instructions={}
    def initial_link(self):link(self.s,'request','owner')
    def call(self,op,args):
        self.calls.append((op,args))
        if op=='get_task':return self.view.copy()
        if op=='lookup_request':return {'found':False}
        if op=='get_task_result':return {'task_id':'task','run_id':'run','exit_code':0,'execution_status':'done','verification':{'commands':[{'cmd':'python check.py','exit_code':0}]}}
        return {'ok':True,'task_id':'task'}
    def instruction(self,op,args):
        key='i'+str(len(self.instructions));self.instructions[key]={'operation':op,'arguments':args}
        with self.s.db:self.s.db.execute('INSERT INTO outbox VALUES (?,?,?,?)',(key,canonical(self.instructions[key]),'pending','task'))
        return {'instruction_id':key,'operation':op}
    def lifecycle(self,op,args,key):
        self.calls.append((op,args))
        with self.s.db:self.s.db.execute("UPDATE outbox SET state='confirmed' WHERE id=?",(key,))
        return {'ok':True}
    def tick(self):
        with patch('portable.automation.Team.new_instruction',side_effect=self.instruction),patch('portable.automation.Team.lifecycle',side_effect=self.lifecycle):
            return Reconciler(self.s,self.call).tick()

class Automation(Fixture):
    def test_failure_continue_reverify_accept_without_new_task(self):
        self.tick();self.tick() # reverify, reconcile
        self.tick();self.tick() # continue, reconcile
        self.view['verify']['passed']=True
        self.tick();self.tick() # fresh reverify, reconcile
        self.tick() # accept
        self.assertEqual([op for op,_ in self.calls if op in ('reverify_task','continue_task','accept_task')],
            ['reverify_task','reverify_task','continue_task','continue_task','reverify_task','reverify_task','accept_task'])
        self.assertNotIn('create_task',[op for op,_ in self.calls])
    def test_three_failed_rounds_stop_and_unchanged_quiet(self):
        for _ in range(12):self.tick()
        value=state(self.s);self.assertTrue(value['links']['request']['stopped'])
        self.assertEqual(value['links']['request']['stalls'],3)
        count=len(self.calls);self.tick();self.assertEqual(len(self.calls),count)
        self.assertEqual(len(state(self.s)['events']),1)
    def test_binding_change_blocks_and_preserves_handwritten(self):
        home=self.s.root/'知识库/首页.md';home.write_text(home.read_text()+'handwritten')
        self.binding['epoch']='new';self.tick()
        self.assertTrue(state(self.s)['links']['request']['stopped']);self.assertIn('handwritten',home.read_text())
        self.assertEqual(self.calls,[])
    def test_pending_lookup_before_submit_and_unknown_never_create(self):
        with self.s.db:self.s.db.execute("UPDATE outbox SET state='pending',reference=NULL WHERE id='request'")
        with patch('portable.automation.Team.submit',return_value={}) as submit:self.tick();submit.assert_called_once()
        self.assertEqual(self.calls[0],('lookup_request',{'idempotency_key':'request'}))
        with self.s.db:self.s.db.execute("UPDATE outbox SET state='unknown' WHERE id='request'")
        with patch('portable.team.require_readiness',return_value=self.binding):self.tick()
        self.assertNotIn('create_task',[op for op,_ in self.calls]);self.assertFalse(state(self.s)['links']['request']['stopped'])
    def test_incomplete_acceptance_cannot_accept(self):
        value=state(self.s);value['links']['request'].update(fresh_verify=True,verified_run='run');save(self.s,value)
        self.view['verify']['passed']=True
        def call(op,args):
            if op=='get_task_result':return {'run_id':'run','verification':{'commands':[]}}
            return self.call(op,args)
        Reconciler(self.s,call).tick()
        self.assertTrue(state(self.s)['links']['request']['stopped']);self.assertFalse(self.instructions)
    def test_notifications_uncertain_query_only_and_crash_dedupe(self):
        value=state(self.s);notice(value,'completed','task',{'status':'completed'});save(self.s,value)
        sent=[]
        deliver(self.s,'feishu',lambda *args:sent.append(args),lambda key:False)
        deliver(self.s,'feishu',lambda *args:sent.append(args),lambda key:False)
        self.assertEqual(len(sent),1)
        deliver(self.s,'feishu',lambda *args:sent.append(args),lambda key:True)
        self.assertEqual(next(iter(state(self.s)['events'].values()))['delivery']['feishu'],'acked')
    def test_invalid_current_authorization_stops_and_notifies_without_network(self):
        with patch('portable.automation.require_readiness',side_effect=Blocked('changed')):
            self.tick()
        value=state(self.s)
        self.assertTrue(value['links']['request']['stopped'])
        self.assertEqual(next(iter(value['events'].values()))['kind'],'scope_changed')
        self.assertEqual(self.calls,[])

    def test_pause_prevents_team_calls(self):
        pause(self.s);self.tick();self.assertEqual(self.calls,[])

class Backups(Fixture):
    def test_incremental_reuses_objects_and_restores_all_assets(self):
        media=self.base/'media';media.mkdir();enroll(self.s,media)
        a=incremental(self.s,self.project,media,self.pid);self.assertEqual(a['written_objects'],1)
        b=incremental(self.s,self.project,media,self.pid);self.assertEqual(b['written_objects'],0)
        (self.project/'b.txt').write_text('two');c=incremental(self.s,self.project,media,self.pid)
        self.assertEqual(c['written_objects'],1)
        restore_incremental(c['manifest'],self.base/'restored')
        self.assertEqual((self.base/'restored/a.txt').read_text(),'one')
        self.assertEqual((self.base/'restored/b.txt').read_text(),'two')
        with self.assertRaises(Blocked):restore_incremental(c['manifest'],self.base/'restored')
    def test_incremental_secret_link_and_recursive_media_blocked(self):
        media=self.base/'media';media.mkdir();enroll(self.s,media)
        (self.project/'.env').write_text('secret=synthetic-only')
        with self.assertRaises(Blocked):incremental(self.s,self.project,media,self.pid)
        (self.project/'.env').unlink();(self.project/'link').symlink_to(self.project/'a.txt')
        with self.assertRaises(Blocked):incremental(self.s,self.project,media,self.pid)
    def test_thirty_second_coalesce_and_unmounted_quiet(self):
        media=self.base/'media';media.mkdir();enroll(self.s,media)
        self.s.put('runner:config',{'sync':[{'project':self.pid,'medium':str(media)}]})
        now=[100];runner=Runner(self.s,self.call,lambda:now[0])
        runner.tick();now[0]=120;(self.project/'a.txt').write_text('changed');runner.tick()
        now[0]=149;runner.tick();self.assertFalse((media/'portable-incremental').exists())
        now[0]=150;runner.tick();self.assertTrue((media/'portable-incremental').exists())
        media.rename(self.base/'unmounted');before=len(state(self.s)['events']);runner.tick()
        self.assertEqual(len(state(self.s)['events']),before)
    def test_git_allowlist_secret_rejected_before_history_and_local_only(self):
        note=self.s.root/'知识库/keep.md';note.write_text('safe knowledge')
        dest=self.base/'git-backup';result=git_backup(self.s,dest,['知识库/keep.md'])
        self.assertFalse(result['network']);before=result['commit']
        note.write_text('token=FAKE_SYNTHETIC_SECRET')
        with self.assertRaises(Blocked):git_backup(self.s,dest,['知识库/keep.md'])
        self.assertEqual(git(dest,'rev-parse','HEAD').decode().strip(),before)
        self.assertNotIn(b'FAKE_SYNTHETIC_SECRET',git(dest,'log','-p','--all'))
        with self.assertRaises(Blocked):git_backup(self.s,dest,['数据/state.sqlite'])
        with self.assertRaises(Blocked):git_backup(self.s,dest,['知识库/../数据/state.sqlite'])
    def test_json_secret_and_modified_ignore_block_before_git_history(self):
        note=self.s.root/'知识库/keep.md';note.write_text('{"appSecret":"SYNTHETIC_VALUE_ONLY"}')
        destination=self.base/'git-backup'
        with self.assertRaises(Blocked):git_backup(self.s,destination,['知识库/keep.md'])
        self.assertFalse(destination.exists())
        note.write_text('safe');git_backup(self.s,destination,['知识库/keep.md'])
        before=git(destination,'rev-parse','HEAD')
        (destination/'.gitignore').write_text('token=SYNTHETIC_VALUE_ONLY')
        with self.assertRaises(Blocked):git_backup(self.s,destination,['知识库/keep.md'])
        self.assertEqual(git(destination,'rev-parse','HEAD'),before)

    def test_git_unrelated_repo_and_ignored_secret_cannot_enter(self):
        dest=self.base/'git-backup';note=self.s.root/'知识库/keep.md';note.write_text('safe')
        git_backup(self.s,dest,['知识库/keep.md']);(dest/'credentials.txt').write_text('password=FAKE_SYNTHETIC')
        note.write_text('changed');git_backup(self.s,dest,['知识库/keep.md'])
        self.assertNotIn(b'credentials.txt',git(dest,'ls-tree','-r','HEAD'))
        with self.assertRaises(Blocked):git_backup(self.s,self.project,['知识库/keep.md'])
    def test_service_exact_identity_and_stop_required(self):
        directory=self.base/'LaunchAgents';installed=service(self.s.root,directory,'install')
        self.assertFalse(installed['real_launchd']);p=Path(installed['definition'])
        self.assertTrue(service(self.s.root,directory,'status')['installed'])
        with self.assertRaises(Blocked):service(self.s.root,directory,'uninstall')
        pause(self.s);Runner(self.s).tick();service(self.s.root,directory,'uninstall');self.assertFalse(p.exists())
        p.write_bytes(b'unknown service')
        with self.assertRaises(Exception):service(self.s.root,directory,'install')
    def test_task_poll_throttled_independent_of_media_tick(self):
        now=[100];runner=Runner(self.s,self.call,lambda:now[0])
        with patch('portable.supervisor.Reconciler.tick',return_value={'uncertain':0}) as reconcile:
            runner.tick();now[0]=102;runner.tick();now[0]=129;runner.tick()
            self.assertEqual(reconcile.call_count,1)
            now[0]=130;runner.tick();self.assertEqual(reconcile.call_count,2)
        now[0]=160
        with patch('portable.supervisor.Reconciler.tick',side_effect=OSError('offline')) as reconcile:
            runner.tick();now[0]=190;runner.tick();self.assertEqual(reconcile.call_count,1)
            now[0]=220;runner.tick();self.assertEqual(reconcile.call_count,2)

    def test_daily_weekly_once_and_backup_failure_observable(self):
        self.s.put('runner:config',{'backup_dir':str(self.base/'backups')})
        runner=Runner(self.s,self.call,lambda:100000)
        with patch('portable.supervisor.backup',return_value={'ok':True}) as b:
            runner.tick();runner.tick();self.assertEqual(b.call_count,2)
        runner.clock=lambda:200000
        with patch('portable.supervisor.backup',side_effect=Blocked('fixture')):runner.tick()
        self.assertTrue(any(e['kind']=='blocked' for e in state(self.s)['events'].values()))

    def test_daily_backup_retries_until_success_and_records_error(self):
        # 2026-11-16：一天 3 次用完就不试了，失败原因也丢了；现在每 30 分钟一直试，失败原因写原文
        self.s.put('runner:config',{'backup_dir':str(self.base/'backups')})
        clock=[1794700000.0];runner=Runner(self.s,self.call,lambda:clock[0])
        with patch('portable.supervisor.backup',side_effect=Blocked('存在活动锁/未知 sidecar：数据/x.lock')) as b:
            for i in range(5):runner.tick();clock[0]+=600   # 50 分钟里 tick 5 次
            self.assertEqual([c.args[2] for c in b.call_args_list].count('daily'),2)  # 第一次立刻试，30 分钟后再试
        self.assertIn('存在活动锁',self.s.get('runner:backup-problems')[0])
        self.assertIn('存在活动锁',self.s.get('runner:backup-last-error')['detail'])
        clock[0]+=1800
        with patch('portable.supervisor.backup',return_value={'ok':True}) as b,patch('portable.distribution.archive_residue',return_value={}):
            runner.tick();self.assertEqual([c.args[2] for c in b.call_args_list].count('daily'),1)
            import datetime;self.assertEqual(self.s.get('runner:daily'),datetime.datetime.fromtimestamp(clock[0]).strftime('%Y-%m-%d'))
            clock[0]+=1800;runner.tick();self.assertEqual([c.args[2] for c in b.call_args_list].count('daily'),1)  # 当天成功后不再跑

    def test_stale_upgrade_pause_self_heals(self):
        from portable import supervisor
        from portable.automation import pause,state
        pause(self.s,by='upgrade',reason='升级换程序前停写')
        now=self.s.get('runner:pause-reason')['at']
        with patch.object(supervisor,'_upgrade_running',return_value=False):
            self.assertFalse(supervisor.resume_stale_upgrade_pause(self.s,now+600))  # 不到 30 分钟不动
            with patch.object(supervisor,'_upgrade_running',return_value=True):
                self.assertFalse(supervisor.resume_stale_upgrade_pause(self.s,now+3600))  # 升级还在跑不动
            self.assertTrue(supervisor.resume_stale_upgrade_pause(self.s,now+3600))
        self.assertFalse(state(self.s)['paused']);self.assertIn('升级',self.s.get('runner:auto-resumed')['reason'])
        pause(self.s,by='runner stop 命令',reason='人停的')
        with patch.object(supervisor,'_upgrade_running',return_value=False):
            self.assertFalse(supervisor.resume_stale_upgrade_pause(self.s,now+99999))  # 人明确停的不动

    def test_overview_synced_once_a_day_when_registry_present(self):
        runner=Runner(self.s,self.call,lambda:100000)
        fp=['a']
        with patch('portable.board.overview',return_value={}) as o,patch('portable.board.weekly_check',return_value=[]) as w,patch('portable.board.board_fingerprint',side_effect=lambda s:fp[0]):
            runner.tick();self.assertEqual(o.call_count,0)  # empty registry: nothing to sync
            self.s.put('registry',[{'id':'x','name':'x'}])
            runner.tick();runner.tick();self.assertEqual(o.call_count,1);self.assertEqual(o.call_args.args[1],True)
            fp[0]='b';runner.tick();self.assertEqual(o.call_count,2)  # a STATUS.md changed: refresh at once
            runner.clock=lambda:200000;runner.tick();self.assertEqual(o.call_count,3)
            self.assertEqual(w.call_count,1)  # weekly check once per ISO week
        with patch('portable.board.overview',return_value={}),patch('portable.board.weekly_check',return_value=['甲 已 20 天没更新进度']),patch('portable.board.board_fingerprint',return_value='b'):
            runner.clock=lambda:100000+8*86400;result=runner.tick()
        self.assertIn('甲 已 20 天没更新进度',(self.s.root/'看板/提醒.md').read_text())

    def test_runner_rewrites_unread_replies_every_minute(self):
        from portable.board import expect_reply, record_reply
        expect_reply(self.s,'thread-1','req-1','甲','继续做报表')
        now=[1000];runner=Runner(self.s,self.call,lambda:now[0])
        self.assertEqual(runner.tick()['等回复'],1)
        text=(self.s.root/'看板/等回复.md').read_text()
        self.assertIn('thread-1',text);self.assertIn('继续做报表',text)
        (self.s.root/'看板/等回复.md').write_text('stale')
        runner.tick()
        self.assertEqual((self.s.root/'看板/等回复.md').read_text(),'stale')
        now[0]=1060;record_reply(self.s,'req-1','对方说做完了');runner.tick()
        self.assertIn('没有还没读回的消息',(self.s.root/'看板/等回复.md').read_text())

    def test_opened_pr_checked_for_merge_every_half_hour(self):
        self.s.put('candidate:engineering:'+'a'*64,{'reception':{'state':'awaiting_external_owner','publication':{'state':'pr_opened'}}})
        self.s.put('candidate:engineering:'+'b'*64,{'reception':{'state':'received','publication':{'state':'merged'}}})
        now=[100000];runner=Runner(self.s,self.call,lambda:now[0])
        with patch('portable.candidates.publish',return_value={}) as pub:
            runner.tick();runner.tick();self.assertEqual([c.args[1] for c in pub.call_args_list],['a'*64])
            now[0]+=1800;runner.tick();self.assertEqual(pub.call_count,2)

class Bots(Fixture):
    def setUp(self):
        super().setUp()
        mock=patch('portable.team.transport',return_value=self.call);mock.start();self.addCleanup(mock.stop)
    def config(self):return {'enabled':True,'owners':{'feishu':{'f':'owner'},'wecom':{'w':'owner'}},'feishu':{'appId':'fixture','appSecret':'SYNTHETIC'},'wecom':{'botId':'fixture','secret':'SYNTHETIC'}}
    def binding_value(self):return {'task':'task','action':'deploy','target':'fixture','scope':'fixture','version':'run','owner':'owner'}
    def test_cross_channel_approval_once_pause_invalidates_and_inbox_persistent(self):
        cfg=self.config()
        aid=issue_approval(self.s,self.binding_value())
        v={'channel':'wecom','identity':'w','id':'m','body':'批准 '+aid}
        self.assertTrue(event(self.s,cfg,v)['approved'])
        self.assertTrue(event(self.s,cfg,v)['duplicate'])
        with self.assertRaises(Blocked):event(self.s,cfg,{**v,'channel':'feishu','identity':'f','id':'n'})
        aid=issue_approval(self.s,self.binding_value())
        pause(self.s);pause(self.s,False)
        with self.assertRaises(Blocked):event(self.s,cfg,{**v,'id':'p','body':'批准 '+aid})
        self.assertGreater(self.s.db.execute("SELECT count(*) FROM records WHERE kind='bot_received'").fetchone()[0],0)
    def test_wizard_private_no_secret_diagnostic_and_reject_overwrite(self):
        path=self.base/'private.json';setup_private(path,self.config())
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertNotIn('SYNTHETIC',canonical(diagnose(path)))
        with self.assertRaises(Blocked):setup_private(path,self.config())
        path.chmod(0o644)
        with self.assertRaises(Blocked):diagnose(path)
    def test_notification_ack_binding_unknown_not_replayed(self):
        cfg=self.config();v={'channel':'wecom','target':'w','kind':'completed','id':'n','text':'done'}
        notification(self.s,cfg,v)
        self.assertEqual(notification(self.s,cfg,{**v,'phase':'status'})['state'],'unknown')
        with self.assertRaises(Blocked):notification(self.s,cfg,v)
        with self.assertRaises(Blocked):notification(self.s,cfg,{**v,'text':'changed','phase':'ack'})
        self.assertEqual(notification(self.s,cfg,{**v,'phase':'ack'})['state'],'acked')
    def test_ordinary_material_never_executes_and_wrong_owner_rejected(self):
        cfg=self.config();v={'channel':'feishu','identity':'f','id':'m','body':'delete everything'}
        self.assertFalse(event(self.s,cfg,v)['execution'])
        with self.assertRaises(Blocked):event(self.s,cfg,{**v,'identity':'intruder','id':'x'})


class RealLifecycle(Fixture):
    """Use real Team persistence/guards; only the external transport is synthetic."""
    def initial_link(self):pass # RealLifecycle links only after its public binding handshake.
    def activate(self):
        import sys
        from portable.team import bind_new_tasks
        self.ready.stop()
        server=self.base/'fixture-server.py';server.write_text('# fixture endpoint, never executed')
        self.s.put('team_config',{'python':sys.executable,'server':str(server),'caller':'portable:'+self.s.device})
        def handshake(op,args):
            if op=='__capabilities__':return {'tools':['create_task','get_task','get_task_result','lookup_request','list_tasks']}
            return {'tasks':[]}
        bind_new_tasks(self.s,handshake,self.pid)
        self.binding=self.s.get('readiness')
        self.view['caller']='portable:'+self.s.device
        self.body.update(project=self.pid,binding_epoch=self.binding['epoch'])
        with self.s.db:self.s.db.execute('UPDATE outbox SET body=? WHERE id=?',(canonical(self.body),'request'))
        link(self.s,'request','owner')
    def test_real_durable_lifecycle_reboot_reconciles_without_duplicate_mutation(self):
        self.activate();operations={};lost=[True]
        def call(op,args):
            if op=='lookup_request':return {'found':args['idempotency_key'] in operations,'state':'completed'}
            if op=='reverify_task':
                operations[args['idempotency_key']]=op;self.view['verify']={'passed':True,'status':'passed','ran_at':'fixture'}
                if lost[0]:lost[0]=False;raise TimeoutError('simulated lost response')
            if op=='accept_task':operations[args['idempotency_key']]=op;self.view['status']='completed'
            return self.call(op,args)
        Reconciler(self.s,call).tick()
        row=self.s.db.execute("SELECT * FROM outbox WHERE id!='request'").fetchone()
        self.assertEqual(row['state'],'unknown')
        # New reconciler represents a restart; lookup original instruction, no second verify.
        for _ in range(4):Reconciler(self.s,call).tick()
        self.assertEqual(list(operations.values()).count('reverify_task'),1)
        self.assertEqual(list(operations.values()).count('accept_task'),1)
        self.assertTrue(state(self.s)['links']['request']['stopped'])
        self.assertEqual(self.view['status'],'completed')
    def test_orphan_instruction_recovers_same_id(self):
        self.activate()
        from portable.team import Team
        team=Team(self.s,self.call)
        instruction=team.new_instruction('reverify_task',{'task_id':'task'})
        Reconciler(self.s,self.call).tick()
        self.assertEqual(state(self.s)['links']['request']['instruction']['instruction_id'],instruction['instruction_id'])
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM outbox WHERE id!='request'").fetchone()[0],1)
    def test_daily_real_backup_readback_and_recursive_refusal(self):
        from portable.distribution import backup
        target=self.base/'external';result=backup(self.s,target,'daily')
        copied=Path(result['backup'])
        self.assertEqual((copied/'知识库/首页.md').read_bytes(),(self.s.root/'知识库/首页.md').read_bytes())
        self.assertTrue((copied/'BACKUP.json').is_file())
        with self.assertRaises(Blocked):backup(self.s,self.s.root/'bad','daily')
    def test_foreground_runner_process_acknowledges_stop_then_exits(self):
        import subprocess
        import sys
        import time
        script='from portable.core import Store;from portable.supervisor import Runner;import sys;s=Store(sys.argv[1]);Runner(s,lambda *args: {}).serve(.02)'
        child=subprocess.Popen([sys.executable,'-c',script,str(self.s.root)],cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+5
            while not self.s.get('runner:heartbeat') and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(self.s.get('runner:heartbeat'))
            self.assertFalse(pause(self.s)['writes_stopped'])
            while not self.s.get('runner:stopped') and time.monotonic()<deadline:time.sleep(.02)
            stopped=self.s.get('runner:stopped');self.assertTrue(stopped)
            time.sleep(.06);self.assertEqual(self.s.get('runner:stopped'),stopped)
            child.terminate();stdout,stderr=child.communicate(timeout=5)
            self.assertEqual(child.returncode,0,stderr.decode())
        finally:
            if child.poll() is None:child.kill();child.communicate(timeout=5)

    def test_packaged_module_foreground_tick_and_generated_service_entry(self):
        import plistlib
        import subprocess
        from portable.distribution import build
        source=Path(__file__).resolve().parents[1];installed=self.base/'release'
        build(source,installed)
        value=service(installed,self.base/'agents','install')
        definition=plistlib.loads(Path(value['definition']).read_bytes())
        command=definition['ProgramArguments'];self.assertIn(str(installed/'工具/portable.pyz'),command)
        self.assertTrue(Path(definition['WorkingDirectory']).is_dir())
        result=subprocess.run(command[:-1]+['tick'],cwd=definition['WorkingDirectory'],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertEqual(json.loads(result.stdout),{})
        s=Store(installed)
        try:self.assertTrue(s.get('runner:heartbeat'))
        finally:s.db.close()

    def test_sqlite_incremental_consistency_and_tamper_rejected(self):
        import sqlite3
        db=sqlite3.connect(self.project/'fixture.sqlite');db.execute('CREATE TABLE items(value)');db.execute("INSERT INTO items VALUES ('kept')");db.commit();db.close()
        media=self.base/'media';media.mkdir();enroll(self.s,media)
        result=incremental(self.s,self.project,media,self.pid);restore_incremental(result['manifest'],self.base/'restored')
        db=sqlite3.connect(self.base/'restored/fixture.sqlite');self.assertEqual(db.execute('SELECT value FROM items').fetchone()[0],'kept');db.close()
        manifest=Path(result['manifest']);v=json.loads(manifest.read_text());v['dirs'].append('../escape');manifest.write_text(canonical(v))
        with self.assertRaises(Blocked):restore_incremental(manifest,self.base/'other')

if __name__=='__main__':unittest.main()
