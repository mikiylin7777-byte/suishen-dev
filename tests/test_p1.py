"""Regression of the three coordinator findings, isolated filesystem/backend only."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from portable.core import Store,Blocked,canonical
from portable import distribution as d
from portable.team import Team,bind_new_tasks

SOURCE=Path(__file__).resolve().parents[1]
CHECKPOINT={k:[] for k in ('goal','decisions','done','remaining','authorization','evidence')}

class P1(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.app=self.root/'app';self.candidate=self.root/'candidate'
        d.build(SOURCE,self.app);d.build(SOURCE,self.candidate)
        self.s=Store(self.app);self.task={'task_id':'t1','run_id':'r1','status':'awaiting_acceptance','updated':'v1'}
        self.calls=[];self.disconnect=False
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def call(self,tool,args):
        self.calls.append((tool,dict(args)))
        if self.disconnect:raise TimeoutError()
        if tool=='__capabilities__':return {'tools':['create_task','get_task','get_task_result','lookup_request','list_tasks']}
        if tool=='list_tasks':return {'tasks':[dict(self.task)],'next_offset':None}
        if tool=='get_task':return dict(self.task)
        if tool=='lookup_request':return {'found':True,'state':'completed','task_id':'t1'}
        return {'ok':True,'task_id':'t1'}
    def activate(self):
        server=self.root/'server.py';server.write_text('# fixture')
        self.s.put('team_config',{'server':str(server),'python':'fixture','caller':'portable:'+self.s.device})
        workspace=self.root/'workspace';workspace.mkdir();self.s.register('demo',workspace,'demo')
        self.task.update(workspace=str(workspace),caller='portable:'+self.s.device)
        bind_new_tasks(self.s,self.call,'demo');self.team=Team(self.s,self.call)
    def test_external_notes_and_checkpoint_after_backup_abort(self):
        real=d.backup
        def inject(*args):
            result=real(*args)
            (self.app/'知识库/new.md').write_text('latest note')
            self.s.record('checkpoint',CHECKPOINT)
            return result
        with patch('portable.distribution.backup',side_effect=inject):
            with self.assertRaises(Blocked):d.atomic_upgrade(self.app,self.candidate,self.root/'backups')
        self.assertEqual((self.app/'知识库/new.md').read_text(),'latest note')
        self.assertEqual(len(list((self.app/'任务记录').glob('*.json'))),1)
        self.assertTrue(list((self.root/'backups').glob('upgrade-*')))
    def test_external_change_just_before_exchange_abort(self):
        real=d.release_verify
        def inject(path):
            result=real(path)
            if Path(path).name.startswith('.upgrade-'):(self.app/'知识库/late.md').write_text('late')
            return result
        with patch('portable.distribution.release_verify',side_effect=inject):
            with self.assertRaises(Blocked):d.atomic_upgrade(self.app,self.candidate,self.root/'backups')
        self.assertEqual((self.app/'知识库/late.md').read_text(),'late')
    def test_old_side_edit_after_exchange_restored_and_frozen(self):
        real=d.exchange_directories;count=0
        def inject(left,right):
            nonlocal count
            real(left,right);count+=1
            if count==1:(right/'知识库/during.md').write_text('external')
        with patch('portable.distribution.exchange_directories',side_effect=inject):
            with self.assertRaises(Blocked):d.atomic_upgrade(self.app,self.candidate,self.root/'backups')
        self.assertEqual((self.app/'知识库/during.md').read_text(),'external')
        with self.assertRaises(Blocked):self.s.record('checkpoint',CHECKPOINT)
        receipt=list((self.root/'backups').glob('upgrade-receipt-*'))[0]
        self.assertEqual(json.loads(receipt.read_text())['phase'],'conflict-after-exchange')
    def test_cooperative_writer_waits_and_writes_current_database(self):
        started=threading.Event();finished=threading.Event();threads=[];errors=[]
        real=d.backup
        def writer():
            started.set()
            try:
                s=Store(self.app);s.record('checkpoint',CHECKPOINT);s.db.close()
            except Exception as e:errors.append(e)
            finally:finished.set()
        def inject(*args):
            result=real(*args);t=threading.Thread(target=writer);threads.append(t);t.start()
            self.assertTrue(started.wait(2));self.assertFalse(finished.wait(.05));return result
        with patch('portable.distribution.backup',side_effect=inject):d.atomic_upgrade(self.app,self.candidate,self.root/'backups')
        for t in threads:t.join(5)
        self.assertTrue(finished.is_set());self.assertEqual(errors,[])
        # Existing Store also follows the active generation after exchange.
        self.s.record('checkpoint',CHECKPOINT)
        check=Store(self.app)
        self.assertEqual(check.db.execute("SELECT count(*) FROM records WHERE kind='checkpoint'").fetchone()[0],2);check.db.close()
    def test_completed_submission_history_does_not_block_upgrade(self):
        self.activate()
        from portable.verification import review
        mvp=dict(user='owner',problem='fixture',flow='local',scope='test',acceptance='test')
        rid=review(self.s,mvp,['python3 -m unittest']);key=self.s.queue('implement',mvp,rid)
        self.team.submit(key,str(self.root/'workspace'))
        self.assertEqual(self.s.db.execute('SELECT state FROM outbox').fetchone()[0],'submitted')
        with patch('portable.team.transport',return_value=self.call):
            with self.assertRaises(Blocked):d.prepare_update(self.s)
            self.task['status']='completed'
            d.prepare_update(self.s)
            d.atomic_upgrade(self.app,self.candidate,self.root/'backups')
        check=Store(self.app);row=check.db.execute('SELECT * FROM outbox WHERE id=?',(key,)).fetchone()
        self.assertEqual(row['state'],'terminal');self.assertEqual(json.loads(row['body'])['terminal_observation']['status'],'completed');check.db.close()
    def test_delivered_project_copy_skipped_by_backup_and_carried_over(self):
        import subprocess
        delivery=self.app/'任务记录/候选交付'/('c'*64);full=delivery/'完整候选';full.mkdir(parents=True)
        subprocess.run(['git','init','-q',str(full)],check=True)
        (full/'.gitignore').write_text('results/\n');(full/'results').mkdir();(full/'results/r.md').write_text('ignored report\n')
        (full/'fixture.mjs').write_text('-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\n')
        (delivery/'候选.patch').write_text('diff\n')
        staging=self.app/'代码/接收事务'/('c'*64+'-'+'d'*32);(staging/'new').mkdir(parents=True);(staging/'new/fixture.mjs').write_text((full/'fixture.mjs').read_text());(staging/'RECEPTION.json').write_text('{}')
        saved=Path(d.backup(self.s,self.root/'backups','daily')['backup'])
        self.assertTrue((saved/'任务记录/候选交付'/('c'*64)/'候选.patch').is_file())
        self.assertFalse((saved/'任务记录/候选交付'/('c'*64)/'完整候选').exists())
        self.s.db.close()
        d.atomic_upgrade(self.app,self.candidate,self.root/'backups')
        self.s=Store(self.app)
        self.assertEqual((self.app/'任务记录/候选交付'/('c'*64)/'完整候选/results/r.md').read_text(),'ignored report\n')
        self.assertTrue((self.app/'代码/接收事务'/('c'*64+'-'+'d'*32)/'new/fixture.mjs').is_file())
        self.assertTrue((saved/'代码/接收事务'/('c'*64+'-'+'d'*32)/'RECEPTION.json').is_file())
    def test_unknown_disconnected_preserved_and_blocks_all_upgrade_paths(self):
        self.activate();key=self.team.new_instruction('reverify_task',{'task_id':'t1'})['instruction_id']
        with self.s.db:self.s.db.execute("UPDATE outbox SET state='unknown' WHERE id=?",(key,))
        self.disconnect=True
        with patch('portable.team.transport',return_value=self.call):
            for operation in (lambda:d.prepare_update(self.s),lambda:d.atomic_upgrade(self.app,self.candidate,self.root/'backups'),lambda:d.update(self.s,self.candidate,self.root/'backups')):
                with self.assertRaises(Blocked):operation()
        self.assertEqual(self.s.db.execute('SELECT state FROM outbox WHERE id=?',(key,)).fetchone()[0],'unknown')
    def test_second_intent_reaches_backend_and_retry_queries_only(self):
        self.activate()
        for operation in ('reverify_task','continue_task','cancel_task'):
            args={'task_id':'t1'}
            first=self.team.new_instruction(operation,args)['instruction_id'];self.team.lifecycle(operation,args,first)
            if operation=='cancel_task':self.task['status']='cancelled'
            self.team.lifecycle(operation,args,first)
            second=self.team.new_instruction(operation,args)['instruction_id'];self.assertNotEqual(first,second)
            self.team.lifecycle(operation,args,second)
            self.assertEqual(sum(t==operation for t,a in self.calls),2)
            if operation=='cancel_task':self.task['status']='cancelled'
            self.team.lifecycle(operation,args,second)
    def test_run_change_and_unknown_never_create_implicit_new_id(self):
        self.activate();args={'task_id':'t1'}
        first=self.team.new_instruction('reverify_task',args)['instruction_id'];self.task['run_id']='r2'
        with self.assertRaises(Blocked):self.team.lifecycle('reverify_task',args,first)
        second=self.team.new_instruction('reverify_task',args)['instruction_id']
        real=self.team.transport
        def uncertain(tool,args):
            if tool=='reverify_task':raise TimeoutError()
            if tool=='lookup_request':return {'found':True,'state':'unknown'}
            return real(tool,args)
        self.team.transport=uncertain
        with self.assertRaises(Blocked):self.team.lifecycle('reverify_task',args,second)
        self.team.lifecycle('reverify_task',args,second)
        with self.assertRaises(Blocked):self.team.new_instruction('reverify_task',args)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)
    def test_active_side_edit_after_exchange_is_not_overwritten(self):
        real=d.exchange_directories
        def inject(left,right):
            real(left,right)
            (left/'知识库/active.md').write_text('new active edit')
            (right/'知识库/old.md').write_text('old side edit')
        with patch('portable.distribution.exchange_directories',side_effect=inject):
            with self.assertRaises(Blocked):d.atomic_upgrade(self.app,self.candidate,self.root/'backups')
        self.assertEqual((self.app/'知识库/active.md').read_text(),'new active edit')
        receipt=json.loads(next((self.root/'backups').glob('upgrade-receipt-*')).read_text())
        self.assertEqual((Path(receipt['previous_package'])/'知识库/old.md').read_text(),'old side edit')
        with self.assertRaises(Blocked):self.s.put('test','blocked')
    def test_upgrade_with_sealed_rebuildable_environment(self):
        self.s.dbpath.chmod(0o600)
        from portable.dependencies import seal_environment
        folder=self.app/'工具/runtimes/fixture';(folder/'venv/bin').mkdir(parents=True)
        (folder/'venv/bin/python').symlink_to('/synthetic/nonexistent-python')
        (folder/'private-home').mkdir()
        sealed=seal_environment(folder)
        (folder/'install.json').write_text(canonical({'state':'ready','environment_sha256':sealed}))
        (self.app/'知识库/keep.md').write_text('owner')
        d.atomic_upgrade(self.app,self.candidate,self.root/'backups')
        self.assertEqual(self.s.dbpath.stat().st_mode & 0o777,0o600)
        self.assertFalse((self.app/'工具/runtimes/fixture/venv').exists())
        self.assertTrue((self.app/'工具/runtimes/fixture/environment-files.json').is_file())
        self.assertEqual((self.app/'知识库/keep.md').read_text(),'owner')
    def test_cli_requires_explicit_durable_id_and_reuses_it(self):
        import contextlib,io
        from portable.__main__ import main
        self.activate();args=self.root/'args.json';args.write_text(canonical({'task_id':'t1'}))
        def cli(*arguments):
            out=io.StringIO()
            with contextlib.redirect_stdout(out),contextlib.redirect_stderr(io.StringIO()),patch('portable.__main__.transport',return_value=self.call):
                code=main(['--root',str(self.app),*arguments])
            return code,json.loads(out.getvalue()) if out.getvalue() else None
        self.assertEqual(cli('lifecycle','reverify_task',str(args))[0],2)
        code,result=cli('new-instruction','reverify_task',str(args));self.assertEqual(code,0)
        key=result['instruction_id']
        for i in range(2):self.assertEqual(cli('lifecycle','reverify_task',str(args),'--instruction-id',key)[0],0)
        self.assertEqual(sum(t=='reverify_task' for t,a in self.calls),1)
