import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
sys.dont_write_bytecode=True
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from portable.core import Store,Blocked,canonical,digest,VERSION
from portable import privacy
from portable.automation import state,save,pause,Reconciler
from portable.supervisor import Runner,configure_local
from portable.distribution import build,release_verify
from portable.bots import event,setup_private
from test_automation import Fixture

SOURCE=Path(__file__).resolve().parents[1]

class AcceptanceExecution(Fixture):
    def test_failed_unknown_or_old_run_cannot_accept_old_green_verify(self):
        for code,status,run in [(1,'failed','run'),(None,None,'run'),(0,None,'run'),(0,'done','old')]:
            with self.subTest(code=code,status=status,run=run):
                v=state(self.s);v['links']['request'].update(fresh_verify=True,verified_run='run',stopped=False);save(self.s,v)
                self.view['verify']['passed']=True
                def call(op,args):
                    if op=='get_task_result':return {'exit_code':code,'execution_status':status,'run_id':run,'verification':{'commands':[{'cmd':'python check.py','exit_code':0}]}}
                    return self.call(op,args)
                with patch('portable.automation.Team.new_instruction',side_effect=AssertionError('must not accept')):
                    Reconciler(self.s,call).tick()
                self.assertTrue(state(self.s)['links']['request']['stopped'])

class Integration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
    def test_credentials_chinese_and_permissions_remain_useful(self):
        value={'authorization':'只授权本地开发，不允许生产写入','boundary':{'authorization':{'allowed':['代码','测试']}},'密码':'SYNTHETIC123','appSecret':'FAKEVALUE123','text':'验证码：123456 密钥:FAKEKEY999 Authorization: Basic YWJjOmRlZg== Cookie: sid=fakecookie'}
        text=canonical(privacy.sanitize(value))
        for secret in ('SYNTHETIC123','FAKEVALUE123','123456','FAKEKEY999','YWJjOmRlZg==','fakecookie'):self.assertNotIn(secret,text)
        self.assertIn('只授权本地开发',text);self.assertIn('不允许生产写入',text);self.assertIn('代码',text)
    def test_full_quoted_secrets_and_your_prefix_are_not_partially_retained(self):
        values=['your_real_private_value','FIRST SECOND; THIRD','quoted \" tail','line\nsecond']
        for value in values:
            structured=privacy.sanitize({'password':value,'authorization':'普通开发权限说明'})
            self.assertEqual(structured['password'],privacy.REDACTED)
            text=privacy.redact_text(canonical({'password':value,'appSecret':value,'密码':value}))
            self.assertNotIn('SECOND',text);self.assertNotIn('tail',text);self.assertNotIn('your_real',text);self.assertNotIn('second',text)
        self.assertEqual(privacy.sanitize({'authorization':{'allowed':['local code']}}),{'authorization':{'allowed':['local code']}})

    def test_distribution_protocol_and_unified_commands_without_source(self):
        app=self.root/'release';build(SOURCE,app);release_verify(app)
        self.assertEqual(json.loads((app/'工具/协议.json').read_text())['version'],VERSION)
        for name in ('AGENTS.md','CLAUDE.md'):
            text=(app/name).read_text();self.assertIn('### 总管工作法',text);self.assertIn('不信自报',text);self.assertIn('工具/开发协议.md',text);self.assertNotIn('portable/protocol.json',text);self.assertNotIn('直接实现',text)
        for args in [('onboarding','status'),('automation','status'),('context','enroll','--client','fixture'),('workflow','--help'),('knowledge','--help'),('sessions','--help'),('runner','--help')]:
            r=subprocess.run([sys.executable,'-I','-B',str(app/'工具/portable.pyz'),'--root',str(app),*args],cwd=self.root,capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
        self.assertFalse((app/'数据/私密').exists());self.assertFalse((app/'代码').iterdir().__next__ if False else any((app/'代码').iterdir()))
    def test_copy_invalidates_runtime_approvals_and_old_paths(self):
        s=Store(self.root/'old');s.put('runner:config',{'backup_dir':str(self.root/'must-not-write')});s.put('runner:heartbeat',{'pid':123});s.put('context_clients',{'old':{'device':s.device}})
        aid=s.approval(dict(task='t',action='a',target='x',scope='local',version='v',owner='o'))
        s.db.execute('PRAGMA wal_checkpoint(FULL)');s.db.close();shutil.copytree(self.root/'old',self.root/'new')
        new=Store(self.root/'new');self.addCleanup(new.db.close)
        self.assertEqual(new.get('runner:config',{}),{});self.assertEqual(new.get('context_clients',{}),{})
        self.assertEqual(new.db.execute('SELECT expires FROM approvals WHERE id=?',(aid,)).fetchone()[0],0)
        with self.assertRaises(Blocked):Runner(new).tick()
        with self.assertRaises(Blocked):pause(new,False)
        self.assertFalse((self.root/'must-not-write').exists())
        configure_local(new,{'backup_dir':str(self.root/'new-backup')})
        self.assertFalse(state(new)['links']);self.assertEqual(new.get('execution'),'unbound')
    def test_bot_conversation_is_retrievable_for_engineering_without_dispatch(self):
        from portable.context_server import Context,enroll
        s=Store(self.root/'app');self.addCleanup(s.db.close);repo=self.root/'repo';repo.mkdir();s.register('fixture',repo,'fixture');s.put('selected_project','fixture')
        cfg={'owners':{'feishu':{'f':'owner'},'wecom':{'w':'owner'}}}
        for i,text in enumerate(['做一个本地搜索','本人','找不到资料','输入关键词后查看结果','只做本地','能检索中文','实施']):
            result=event(s,cfg,{'channel':'feishu','identity':'f','id':str(i),'body':text})
            self.assertFalse(result['execution']);self.assertTrue(result.get('reply'))
        enroll(s,'engineer');pending=Context(s,'engineer').call('pending_requirements',{})
        self.assertEqual(len(pending),1);self.assertEqual(pending[0]['state'],'needs_engineer_plan')
        self.assertEqual(s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        self.assertTrue(event(s,cfg,{'channel':'feishu','identity':'f','id':'0','body':'repeat'})['duplicate'])
        setup_private(self.root/'one-channel.json',{'enabled':False,'owners':{'feishu':{'f':'owner'}},'feishu':{'appId':'fake-id','appSecret':'fake-secret'}})

class ApprovalConsumer(unittest.TestCase):
    def test_current_run_decision_once_actual_local_consumer_and_stale_rejection(self):
        from portable.bots import issue_approval,approval_execute_checkpoint
        with tempfile.TemporaryDirectory() as temp:
            s=Store(Path(temp).resolve()/'app')
            try:
                view={'task_id':'task','run_id':'run','status':'awaiting_acceptance','workspace':'fixture','verify':{'passed':True}}
                checkpoint=dict(goal='fixture',decisions=[],done=[],remaining=[],authorization='local checkpoint only',evidence=['fixture'])
                binding=dict(task='task',action='record_checkpoint',target='local-checkpoint',scope=checkpoint,version='run',owner='owner')
                cfg={'owners':{'feishu':{'f':'owner'},'wecom':{'w':'owner'}}}
                with patch('portable.team.transport',return_value=lambda *args:view):
                    aid=issue_approval(s,binding)
                    view['run_id']='new'
                    with self.assertRaises(Blocked):event(s,cfg,dict(channel='feishu',identity='f',id='old',body='批准 '+aid))
                    self.assertIsNone(s.db.execute('SELECT consumed FROM approvals WHERE id=?',(aid,)).fetchone()[0])
                    view['run_id']='run';aid=issue_approval(s,binding)
                    event(s,cfg,dict(channel='wecom',identity='w',id='current',body='批准 '+aid))
                    result=approval_execute_checkpoint(s,aid,'fixture-agent');self.assertTrue(result['local_action_executed'])
                    with self.assertRaises(Blocked):approval_execute_checkpoint(s,aid,'second-agent')
                    self.assertEqual(s.db.execute("SELECT count(*) FROM records WHERE kind='checkpoint'").fetchone()[0],1)
            finally:s.db.close()

class RepoBoundaries(unittest.TestCase):
    def test_links_are_metadata_only_submodules_fingerprinted_without_external_reads(self):
        from portable import workflows
        from portable.git_portable import git
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();repo=root/'repo';repo.mkdir();external=root/'external';external.write_text('do not read external body')
            git(repo,'init');(repo/'a').write_text('text');(repo/'internal').symlink_to('a');(repo/'external').symlink_to(external)
            git(repo,'add','.');git(repo,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-m','fixture')
            sub=root/'sub';sub.mkdir();git(sub,'init');(sub/'b').write_text('submodule');git(sub,'add','.');git(sub,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-m','sub')
            subprocess.run(['git','-c','protocol.file.allow=always','-C',str(repo),'submodule','add',str(sub),'modules/sub'],check=True,capture_output=True)
            s=Store(root/'app')
            try:
                s.register('fixture',repo,'fixture');first=workflows.repo_version(s,'fixture')
                self.assertEqual(first['files']['external'],{'symlink':str(external)})
                self.assertNotIn('do not read',canonical(first));self.assertIn('submodule',first['files']['modules/sub'])
                (repo/'modules/sub/b').write_text('dirty');self.assertNotEqual(first['sha256'],workflows.repo_version(s,'fixture')['sha256'])
            finally:s.db.close()

class SensitiveTransport(unittest.TestCase):
    def test_private_binary_ordinary_rejection_omit_receipt_and_encrypted_unlock(self):
        from portable.material_assets import preserve
        from portable import sensitive,migration
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();s=Store(root/'app');binary=b'\x89PNG\x00PRIVATE_SYNTHETIC_IMAGE_BYTES'
            asset=preserve(s,binary,'synthetic image',private=True);s.db.execute('PRAGMA wal_checkpoint(FULL)')
            with self.assertRaises(Blocked):migration.inventory(s.root)
            omitted=root/'ordinary';receipt=sensitive.pack(s.root,omitted,'omit');self.assertIn(asset['path'],receipt['excluded_private_assets']);self.assertFalse((omitted/'payload'/asset['path']).exists())
            self.assertEqual((s.root/asset['path']).read_bytes(),binary)
            identity=root/'identity';r=subprocess.run(['age-keygen','-o',str(identity)],capture_output=True,text=True,check=True);public=next(line.split(': ',1)[1] for line in r.stderr.splitlines() if line.startswith('Public key: '))
            encrypted=root/'encrypted';sensitive.pack(s.root,encrypted,'encrypted',public)
            self.assertNotIn(binary,(encrypted/'payload.tar.age').read_bytes())
            restored=root/'unlocked';sensitive.unpack(encrypted,restored,identity);self.assertEqual((restored/asset['path']).read_bytes(),binary)
            self.assertEqual((s.root/asset['path']).read_bytes(),binary);s.db.close()
    def test_quoted_json_chinese_credentials_in_git_history_and_untracked_block_normal_sets(self):
        from portable import git_portable,portability,sensitive
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();repo=root/'repo';repo.mkdir();git=git_portable.git;git(repo,'init')
            (repo/'config.json').write_text(canonical({'password':'SYNTHETIC_ONLY_12345','密钥':'SYNTHETIC_ONLY_67890'}));git(repo,'add','.');git(repo,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-m','synthetic fixture');(repo/'config.json').unlink()
            git(repo,'add','.');git(repo,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-m','remove')
            for operation in (lambda:git_portable.export(repo,root/'normal'),lambda:portability.create([{'id':'fixture','source':str(repo)}],root/'set')):
                with self.assertRaises(Blocked):operation()
            (repo/'settings.json').write_text('{"appSecret":"SYNTHETIC_ONLY_99999"}')
            with self.assertRaises(Blocked):git_portable.export(repo,root/'normal2')
            identity=root/'identity';r=subprocess.run(['age-keygen','-o',str(identity)],capture_output=True,text=True,check=True);public=next(line.split(': ',1)[1] for line in r.stderr.splitlines() if line.startswith('Public key: '))
            sensitive.pack(repo,root/'encrypted','encrypted',public);sensitive.unpack(root/'encrypted',root/'unlocked',identity)
            self.assertIn(b'SYNTHETIC_ONLY_99999',(root/'unlocked/settings.json').read_bytes());history=git(root/'unlocked','log','--format=%H').stdout.splitlines()[-1].decode()
            self.assertIn(b'SYNTHETIC_ONLY_12345',git(root/'unlocked','show',history+':config.json').stdout)
    def test_data_extract_rejects_parent_paths(self):
        import tarfile
        from portable import sensitive
        def archive(members):
            buf=io.BytesIO()
            with tarfile.open(fileobj=buf,mode='w') as stream:
                for name,body in members:
                    raw=body.encode();info=tarfile.TarInfo(name);info.size=len(raw);info.type=tarfile.REGTYPE
                    stream.addfile(info,io.BytesIO(raw))
            buf.seek(0)
            return tarfile.open(fileobj=buf,mode='r')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            with archive([('payload/a.txt','ok')]) as stream:
                sensitive._extract_data(stream,root/'ok')
            self.assertEqual((root/'ok/payload/a.txt').read_text(),'ok')
            with archive([('../escape.txt','no')]) as stream:
                with self.assertRaises(Blocked):sensitive._extract_data(stream,root/'bad')
    def test_legacy_extract_allows_inside_relative_links_only(self):
        # 3.10+ 也直接走回退实现，不靠官方 data filter 替这段代码过关。
        import stat
        import tarfile
        from portable import sensitive
        def file_info(name, body, mode=0o644):
            info=tarfile.TarInfo(name);info.size=len(body);info.mode=mode;info.type=tarfile.REGTYPE
            return info, body
        def link_info(name, target):
            info=tarfile.TarInfo(name);info.type=tarfile.SYMTYPE;info.linkname=target
            return info, b''
        def hard_info(name, target):
            info=tarfile.TarInfo(name);info.type=tarfile.LNKTYPE;info.linkname=target
            return info, b''
        def dev_info(name):
            info=tarfile.TarInfo(name);info.type=tarfile.CHRTYPE;info.devmajor=1;info.devminor=3
            return info, b''
        def open_archive(items):
            buf=io.BytesIO()
            with tarfile.open(fileobj=buf,mode='w') as stream:
                for info, body in items:
                    stream.addfile(info, io.BytesIO(body) if info.isreg() else None)
            buf.seek(0)
            return tarfile.open(fileobj=buf,mode='r')
        # payload/sub/../b.txt 只放进 legacy 和产品解包断言。官方 data filter 对这种名字各版本不同：3.12.3 不改名，建父目录时 FileExistsError；较新版本会先 normpath。对照包不含它。
        folded=file_info('payload/sub/../b.txt', b'folded')
        safe_items=[
            file_info('payload/a.txt', b'inside'),
            file_info('payload/dir/note.txt', b'note'),
            link_info('payload/dir/up', '../a.txt'),
            link_info('payload/dir/rel', 'note.txt'),
            file_info('payload/setuid.bin', b'x', 0o4755),
            file_info('c', b'chained'),
            link_info('b', 'c'),
            link_info('a', 'b'),
        ]
        legacy_items=safe_items[:4]+[folded]+safe_items[4:]
        rejected=[
            [file_info('/tmp/abs.txt', b'no')],
            [file_info('../escape.txt', b'no')],
            [file_info('payload/a.txt', b'ok'), link_info('payload/abs', '/etc/passwd')],
            [file_info('payload/a.txt', b'ok'), link_info('payload/out', '../../outside')],
            [file_info('payload/dir/a.txt', b'ok'), link_info('payload/dir/out', '../../../outside')],
            [file_info('payload/a.txt', b'ok'), hard_info('payload/hard', 'payload/a.txt')],
            [dev_info('payload/dev')],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            dest=root/'legacy';dest.mkdir()
            with open_archive(legacy_items) as stream:sensitive._extract_data_legacy(stream, dest)
            self.assertEqual((dest/'payload/a.txt').read_bytes(), b'inside')
            self.assertEqual(os.readlink(dest/'payload/dir/up'), '../a.txt')
            self.assertEqual((dest/'payload/dir/up').read_bytes(), b'inside')
            self.assertEqual(os.readlink(dest/'payload/dir/rel'), 'note.txt')
            self.assertEqual((dest/'payload/dir/rel').read_bytes(), b'note')
            self.assertEqual((dest/'payload/b.txt').read_bytes(), b'folded')
            mode=stat.S_IMODE((dest/'payload/setuid.bin').stat().st_mode)
            self.assertEqual(mode & 0o4000, 0)
            self.assertEqual(mode, 0o755)
            self.assertEqual(os.readlink(dest/'a'), 'b')
            self.assertEqual(os.readlink(dest/'b'), 'c')
            self.assertEqual((dest/'a').read_bytes(), b'chained')
            self.assertEqual((dest/'b').read_bytes(), b'chained')
            for index, items in enumerate(rejected):
                with self.subTest(index=index, names=[info.name for info,_ in items]):
                    bad=root/('bad'+str(index));bad.mkdir()
                    with open_archive(items) as stream:
                        with self.assertRaises(Blocked):sensitive._extract_data_legacy(stream, bad)
            escaped=root/'escaped';escaped.mkdir()
            with open_archive([link_info('p/q/d', '../../t'), link_info('p/q/e', 'd/../../../x')]) as stream:
                with self.assertRaises(Blocked):sensitive._extract_data_legacy(stream, escaped)
            self.assertFalse(escaped.exists())
            self.assertFalse((escaped/'p'/'q'/'e').is_symlink())
            folded_dest=root/'folded';folded_dest.mkdir()
            with open_archive([file_info('payload/keep.txt', b'keep'), folded]) as stream:sensitive._extract_data(stream, folded_dest)
            self.assertEqual((folded_dest/'payload/b.txt').read_bytes(), b'folded')
            self.assertEqual((folded_dest/'payload/keep.txt').read_bytes(), b'keep')
            if 'filter' in tarfile.TarFile.extractall.__code__.co_varnames:
                with open_archive(safe_items) as stream:sensitive._extract_data(stream, root/'official')
                self.assertEqual(os.readlink(root/'official/payload/dir/up'), '../a.txt')
                self.assertEqual((root/'official/payload/dir/up').read_bytes(), b'inside')
                self.assertEqual(stat.S_IMODE((root/'official/payload/setuid.bin').stat().st_mode) & 0o4000, 0)
                for index in (1, 2, 3, 4, 6):
                    with self.subTest(official=index):
                        with open_archive(rejected[index]) as stream:
                            with self.assertRaises(Blocked):sensitive._extract_data(stream, root/('official-bad'+str(index)))

class PlannerRecovery(unittest.TestCase):
    def test_connection_failure_does_not_consume_failure_rounds_or_duplicate_create(self):
        from portable import workflows,team
        from portable.planner import Planner
        from portable.git_portable import git
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();repo=root/'repo';repo.mkdir();git(repo,'init');git(repo,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','--allow-empty','-m','fixture')
            s=Store(root/'app');self.addCleanup(s.db.close);s.register('fixture',repo,'fixture');server=root/'server.py';server.write_text('# fixture only')
            s.put('team_config',{'python':sys.executable,'server':str(server),'caller':'portable:'+s.device})
            tasks={};creates=[];offline=[True];lose_response=[True]
            def call(op,a):
                if op=='__capabilities__':return {'tools':['create_task','get_task','get_task_result','lookup_request','list_tasks']}
                if op=='list_tasks':return {'tasks':[]}
                if op=='lookup_request':
                    if offline[0]:raise TimeoutError('fixture offline')
                    return {'found':a['idempotency_key'] in tasks,'task_id':tasks.get(a['idempotency_key'])}
                if op=='create_task':
                    creates.append(a['idempotency_key']);tasks[a['idempotency_key']]='fixture-task'
                    if lose_response[0]:lose_response[0]=False;raise TimeoutError('fixture response lost')
                    return {'task_id':'fixture-task'}
                if op=='get_task':return {'task_id':'fixture-task','status':'queued','run_id':'r'}
                raise AssertionError(op)
            team.bind_new_tasks(s,call,'fixture');rid=workflows.start(s,'fixture','fixture',dict(user='u',problem='p',flow='f',scope='s',acceptance='a'));workflows.answer(s,rid,'implement',True)
            for _ in range(4):Planner(s,call).tick()
            self.assertFalse(creates)
            ref=json.loads(s.db.execute("SELECT value FROM meta WHERE key LIKE 'planner:%'").fetchone()[0]);self.assertEqual(ref['attempts'],0)
            offline[0]=False;Planner(s,call).tick();Planner(s,call).tick()
            ref=json.loads(s.db.execute("SELECT value FROM meta WHERE key LIKE 'planner:%'").fetchone()[0]);self.assertEqual(ref['task'],'fixture-task');self.assertEqual(len(creates),1)
            self.assertFalse((repo/'evidence').exists());self.assertNotEqual(ref['workspace'],str(repo))

class TransportContainer(unittest.TestCase):
    def test_normalized_git_and_private_selection_use_real_container(self):
        from portable import transfer,git_portable
        from portable.material_assets import preserve
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();repo=root/'repo';repo.mkdir();git=git_portable.git;git(repo,'init');(repo/'a').write_text('tracked');git(repo,'add','.');git(repo,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-m','fixture');(repo/'a').write_text('unstaged');(repo/'link').symlink_to('a')
            package=root/'usb-file.portable';transfer.pack(repo,package);self.assertTrue(package.is_file());shutil.rmtree(repo);out=root/'restored';transfer.restore(package,out)
            self.assertEqual((out/'a').read_text(),'unstaged');self.assertTrue((out/'link').is_symlink());git(out,'fsck','--full')
            s=Store(root/'app');binary=b'\x00PRIVATE_SYNTHETIC_BINARY';asset=preserve(s,binary,'fixture',private=True);s.db.execute('PRAGMA wal_checkpoint(FULL)')
            with self.assertRaises(Blocked):transfer.pack(s.root,root/'ordinary.portable')
            transfer.pack(s.root,root/'omit.portable','omit');transfer.restore(root/'omit.portable',root/'omitted')
            self.assertFalse((root/'omitted'/asset['path']).exists());self.assertEqual((s.root/asset['path']).read_bytes(),binary)
            self.assertIn(asset['path'],json.loads((root/'omitted/携带选择.json').read_text())['excluded_private_assets']);s.db.close()

class UpgradeQuiescence(unittest.TestCase):
    def test_prepare_update_stops_only_owned_foreground_runner_before_swap(self):
        from portable.distribution import prepare_update
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'app';s=Store(root)
            code='import sys;sys.path.insert(0,'+repr(str(SOURCE))+');from portable.supervisor import main;sys.exit(main())'
            child=subprocess.Popen([sys.executable,'-I','-B','-c',code,'--root',str(root),'runner','run'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                for _ in range(50):
                    if s.get('runner:heartbeat'):break
                    time.sleep(.1)
                self.assertIsNone(child.poll());self.assertTrue(s.get('runner:heartbeat'))
                prepare_update(s);self.assertEqual(child.wait(timeout=5),0);self.assertTrue(s.get('runner:stopped'))
                self.assertEqual(s.get('execution'),'update-paused')
            finally:
                if child.poll() is None:child.terminate();child.wait(timeout=5)
                s.db.close()

class CandidateTargetAndBootstrap(unittest.TestCase):
    def test_unbound_mcp_new_project_real_bootstrap_no_model_no_overwrite(self):
        from portable.context_server import Context,enroll
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp).resolve()/'app');self.addCleanup(s.db.close);enroll(s,'fixture');ctx=Context(s,'fixture')
            rid=ctx.call('requirement_start',{'idea':'new local project','known':dict(user='u',problem='p',flow='f',scope='s',acceptance='a'),'instruction_id':'idea'})['id']
            current=ctx.call('requirement_status',{'id':rid});current=ctx.call('requirement_answer',{'id':rid,'field':'implement','value':True,'expected':current['revision'],'instruction_id':'intent'})
            result=ctx.call('requirement_create_project',{'id':rid,'name':'New project','project':'new-project','expected':current['revision'],'instruction_id':'bootstrap'})
            root=Path(result['path']);self.assertTrue((root/'.git').is_dir());self.assertTrue((root/'README.md').is_file());self.assertTrue((s.root/'知识库/项目/new-project.md').is_file())
            self.assertTrue(result['requirement']['intent']);self.assertEqual(s.get('execution'),'unbound');self.assertEqual(s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
            with self.assertRaises(Blocked):ctx.call('project_create',{'name':'overwrite','project':'new-project','instruction_id':'duplicate'})
            self.assertEqual((root/'README.md').read_text(),'# New project\n')
    def test_original_absolute_targets_rejected_candidate_relative_and_bound_harness_supported(self):
        from portable.verification import candidate_commands_checked,bind_candidate_commands
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();source=root/'original';candidate=root/'candidate';source.mkdir();candidate.mkdir()
            (source/'feature').write_text('old');(candidate/'feature').write_text('new')
            with self.assertRaises(Blocked):candidate_commands_checked(['cd '+str(source)+' && touch cache'],source)
            with self.assertRaises(Blocked):candidate_commands_checked(['python3 '+str(source/'tests.py')],source)
            command="python3 -c \"from pathlib import Path;assert Path('feature').read_text()=='new';Path('cache').write_text('candidate only')\""
            effective=bind_candidate_commands([command],source,candidate)[0];r=subprocess.run(effective,shell=True,cwd=candidate,capture_output=True);self.assertEqual(r.returncode,0);self.assertFalse((source/'cache').exists());self.assertTrue((candidate/'cache').exists())
            harness=root/'read_only_harness.py';harness.write_text('import sys\n')
            bound=bind_candidate_commands(['python3 '+str(harness)+' --target {candidate}'],source,candidate,{str(harness):digest(harness.read_bytes())})[0];self.assertIn(str(candidate),bound);self.assertIn('.portable-harnesses',bound)
    def test_sensitive_history_uses_declared_safe_snapshot_and_keeps_necessary_untracked(self):
        from portable.development import export
        from portable.git_portable import git
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();source=root/'source';source.mkdir();git(source,'init');(source/'config.json').write_text('{"password":"SYNTHETIC_OLD_HISTORY_ONLY"}');git(source,'add','.');git(source,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-m','fixture')
            (source/'config.json').unlink();(source/'necessary.py').write_text('print(1)\n');(source/'.env').write_text('PASSWORD=SYNTHETIC_LOCAL_ONLY')
            before=git(source,'rev-parse','HEAD').stdout;candidate=root/'candidate';info=export(source,candidate)
            self.assertTrue(info['history_detached']);self.assertFalse((candidate/'.env').exists());self.assertTrue((candidate/'necessary.py').is_file());self.assertEqual(git(source,'rev-parse','HEAD').stdout,before)
            self.assertEqual((source/'.env').read_text(),'PASSWORD=SYNTHETIC_LOCAL_ONLY')
            for oid in git(candidate,'cat-file','--batch-all-objects','--batch-check=%(objectname)').stdout.splitlines():
                if git(candidate,'cat-file','-t',oid.decode()).stdout.strip()==b'blob':self.assertNotIn(b'SYNTHETIC_OLD_HISTORY_ONLY',git(candidate,'cat-file','blob',oid.decode()).stdout)

class RuntimeAssetCAS(unittest.TestCase):
    def test_env_changed_at_real_exchange_boundary_is_preserved_and_conflict_reported(self):
        from portable import candidates,workflows,distribution
        from portable.git_portable import git
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp).resolve()/'app');self.addCleanup(s.db.close);root=Path(candidates.create_managed(s,'fixture','fixture')['path'])
            (root/'.gitignore').write_text('.env\n');git(root,'add','.gitignore');git(root,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-m','ignore runtime')
            (root/'.env').write_text('PASSWORD=SYNTHETIC_OLD_VALUE')
            version=workflows.repo_version(s,'fixture')['sha256'];value=candidates.prepare(s,'request','fixture',version)
            (Path(value['workspace'])/'feature.py').write_text('print(1)\n')
            original=distribution.exchange_directories;called=[]
            def race(left,right):
                if not called:(root/'.env').write_text('PASSWORD=SYNTHETIC_LATEST_VALUE')
                called.append(True);return original(left,right)
            with patch('portable.distribution.exchange_directories',side_effect=race):result=candidates.receive(s,'request')
            self.assertTrue(called);self.assertEqual(result['state'],'conflict');self.assertEqual((root/'.env').read_text(),'PASSWORD=SYNTHETIC_LATEST_VALUE');self.assertFalse((root/'feature.py').exists())
            self.assertTrue(Path(result['preserved']).exists())

class OriginalScopeControl(unittest.TestCase):
    def setUp(self):
        from portable.context_server import Context,enroll
        from portable import workflows,team
        from portable.git_portable import git
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve();self.s=Store(self.root/'app');self.addCleanup(self.s.db.close)
        self.repo=self.root/'repo';self.repo.mkdir();git(self.repo,'init');(self.repo/'README.md').write_text('fixture');git(self.repo,'add','.');git(self.repo,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-m','fixture')
        self.s.register('fixture',self.repo,'fixture');server=self.root/'server.py';server.write_text('# synthetic transport endpoint')
        self.s.put('team_config',{'python':sys.executable,'server':str(server),'caller':'portable:'+self.s.device});self.created=[];self.operations={};self.calls=[];self.view={};self.commands=['test -f README.md']
        team.bind_new_tasks(self.s,self.backend,'fixture');enroll(self.s,'a');enroll(self.s,'b');self.a=Context(self.s,'a',self.backend);self.b=Context(self.s,'b',self.backend)
        self.rid=workflows.start(self.s,'fixture','fixture',dict(user='u',problem='p',flow='f',scope='first',acceptance='readme'));workflows.answer(self.s,self.rid,'implement',True)
    def backend(self,operation,args):
        self.calls.append(operation)
        if operation=='__capabilities__':return {'tools':['create_task','get_task','get_task_result','lookup_request','list_tasks','task_settled']}
        if operation=='list_tasks':return {'tasks':[]}
        if operation=='lookup_request':return {'found':args['idempotency_key'] in self.operations,'task_id':'task','state':'completed'}
        if operation=='create_task':
            self.created.append(args);self.commands=args['acceptance_criteria'];self.view={'task_id':'task','run_id':'run1','workspace':args['workspace'],'caller':'portable:'+self.s.device,'status':'awaiting_acceptance','verify':{'passed':True,'ran_at':'initial'}};return {'task_id':'task','status':'queued'}
        if operation=='get_task':return json.loads(canonical(self.view))
        if operation=='get_task_result':return {'task_id':'task','run_id':self.view['run_id'],'exit_code':0,'execution_status':'done','verification':{'commands':[{'cmd':c,'exit_code':subprocess.run(c,shell=True,cwd=self.view['workspace'],capture_output=True).returncode} for c in self.commands]}}
        if operation=='task_settled':return {'task_id':'task','stopped':True,'state':'settled','status':self.view['status'],'run_id':self.view['run_id']}
        if operation=='cancel_task':self.view['status']='cancelled';return {'task_id':'task','cancel_effective':True}
        if operation=='continue_task':
            self.operations[args['idempotency_key']]=operation;self.commands=args['verification_commands'];self.view.update(run_id='run2',verify={'passed':False,'ran_at':'continued'});Path(self.view['workspace'],'second').write_text('new scope');return {'task_id':'task','requested':True}
        if operation=='reverify_task':self.operations[args['idempotency_key']]=operation;self.view['verify']={'passed':True,'ran_at':str(len(self.operations))};return {'task_id':'task'}
        if operation=='accept_task':self.operations[args['idempotency_key']]=operation;self.view['status']='completed';return {'task_id':'task'}
        raise AssertionError(operation)
    def review(self,context,instruction):
        inspected=context.call('engineering_context',{'id':self.rid,'files':['README.md'],'instruction_id':'inspect'+instruction})
        return context.call('engineering_review',{'id':self.rid,'inspection':inspected['inspection'],'revision':inspected['revision'],'plan':'fixture implementation','commands':['test -f README.md'],'confirmed':True,'instruction_id':'review'+instruction})
    def submit(self,planned):return self.a.call('request_submit',{'request':planned['request'],'instruction_id':'submit'+planned['request']})
    def test_two_clients_one_logical_revision_produce_one_queue_and_task(self):
        first=self.review(self.a,'a');second=self.review(self.b,'b');self.assertEqual(first['request'],second['request']);self.assertEqual(first['queue_key'],second['queue_key'])
        self.submit(first);self.b.call('request_submit',{'request':second['request'],'instruction_id':'second-submit'})
        self.assertEqual(len(self.created),1);self.assertEqual(self.s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
    def test_withdraw_stops_old_verification_acceptance_and_receive_and_formally_cancels(self):
        from portable import workflows
        first=self.review(self.a,'a');self.submit(first);current=workflows.status(self.s,self.rid);workflows.answer(self.s,self.rid,'implement',False,current['revision'])
        for _ in range(6):Reconciler(self.s,self.backend).tick()
        self.assertEqual(self.view['status'],'cancelled');self.assertEqual(self.calls.count('cancel_task'),1);self.assertNotIn('reverify_task',self.calls);self.assertNotIn('accept_task',self.calls)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM records WHERE kind='delivery'").fetchone()[0],0);self.assertNotIn('reception',self.s.get('candidate:engineering:'+first['queue_key']))
    def test_backlog_append_does_not_change_execution_scope_or_duplicate_task(self):
        from portable import workflows
        first=self.review(self.a,'a');self.submit(first);current=workflows.status(self.s,self.rid);workflows.answer(self.s,self.rid,'extra','future separate feature',current['revision'])
        again=self.review(self.b,'again');self.assertEqual(first['request'],again['request']);self.assertFalse(self.s.get('request-control:'+first['queue_key']))
    def test_reviewed_revision_continues_same_task_and_uses_replacement_verification(self):
        from portable import workflows
        first=self.review(self.a,'a');self.submit(first);current=workflows.status(self.s,self.rid);workflows.answer(self.s,self.rid,'scope','second scope',current['revision'])
        inspected=self.b.call('engineering_context',{'id':self.rid,'files':['README.md'],'instruction_id':'adjust-inspect'})
        self.b.call('request_adjust',{'request':first['request'],'inspection':inspected['inspection'],'revision':inspected['revision'],'plan':'add second marker in the same task','commands':['test -f second'],'confirmed':True,'instruction_id':'adjust'})
        for _ in range(10):Reconciler(self.s,self.backend).tick()
        self.assertEqual(len(self.created),1);self.assertEqual(self.calls.count('continue_task'),1);self.assertEqual(self.view['status'],'completed');self.assertEqual(self.commands,['test -f second'])
        self.assertFalse((self.repo/'second').exists());self.assertEqual(self.s.get('request-amendment:'+first['queue_key'])['revision'],workflows.status(self.s,self.rid)['execution_revision'])

class ComponentEntry(unittest.TestCase):
    def test_public_cli_roundtrip_and_unattended_activation_refusal(self):
        from portable.__main__ import main
        from portable import component_cli
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();app=root/'product';source=root/'config.json';source.write_text('{"theme":"dark"}')
            with __import__('contextlib').redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--root',str(app),'components','select-file','settings',str(source),'--adapter','json-config']),0)
                self.assertEqual(main(['--root',str(app),'components','doctor']),0)
            s=Store(app)
            try:
                self.assertEqual(component_cli.local(s)['selected'][0]['id'],'settings')
                with patch('sys.stdin.isatty',return_value=False),self.assertRaises(Blocked):component_cli.activate(s,'missing','settings','missing','service')
                value=s.get(component_cli.KEY);value['device']='different-device';s.put(component_cli.KEY,value);self.assertEqual(component_cli.local(s)['selected'],[])
            finally:s.db.close()
    def test_system_authorization_metadata_is_not_a_credential(self):
        self.assertFalse(privacy.CredentialContent().search("{'system_authorization':'first_launch_not_performed'}"))
        self.assertTrue(privacy.CredentialContent().search('{"authorization":"Bearer SYNTHETIC_TOKEN_123456"}'))

class SourceAndHandoffBoundaries(unittest.TestCase):
    def test_sql_code_preserved_dump_database_and_credentials_excluded(self):
        from portable.development import export
        from portable.git_portable import git
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();source=root/'source';source.mkdir();git(source,'init','-q');(source/'migrations').mkdir();(source/'queries').mkdir()
            (source/'migrations/schema.sql').write_text('CREATE TABLE fixture(id integer);')
            (source/'queries/list.sql').write_text('SELECT id FROM fixture;')
            (source/'dump.sql').write_text('-- MySQL dump\nINSERT INTO people VALUES (1);')
            (source/'fake.db').write_bytes(b'SQLite format 3\x00fixture')
            (source/'queries/private.sql').write_text('-- password="SYNTHETIC_PRIVATE_123456"\nSELECT 1;')
            git(source,'add','.');git(source,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-qm','fixture')
            target=root/'candidate';export(source,target)
            self.assertTrue((target/'migrations/schema.sql').is_file());self.assertTrue((target/'queries/list.sql').is_file());self.assertFalse((target/'dump.sql').exists());self.assertFalse((target/'fake.db').exists());self.assertNotIn('SYNTHETIC_PRIVATE', ''.join(p.read_text() for p in target.rglob('*.sql')))
class PersistentComponentGate(unittest.TestCase):
    def test_precise_persistent_gate_readback_health_and_required_components(self):
        from portable import components as c,component_install as ci,component_cli
        from test_components import ActualAdapterTests
        helper=ActualAdapterTests();helper.setUp();self.addCleanup(helper.doCleanups)
        root=helper.root;native=helper.compile(root/'native');recipe=root/'recipe.json';recipe.write_text(canonical({'program_component':'worker','profile':'native-version','parameters':{}}));key,recipient=helper.key()
        package=root/'service.bundle';c.save([c.register('worker',native,kind='cli',adapter='claude-native',version='1.2.3',selected=True),c.register('service',recipe,kind='launchd',adapter='launchd-recipe',selected=True)],package,recipient=recipient)
        generation=root/'generation';c.restore(package,generation,identity_file=key);row=next(r for r in c.plan(generation) if r['id']=='worker');prefix=root/'prefix';c.materialize_native(generation,'worker',prefix,trusted_component_hash=row['hash'])
        store=Store(root/'product');self.addCleanup(store.db.close);stage=root/'stage';c.stage_service(generation,'service',stage,device=store.device,bindings={'worker':prefix})
        local=component_cli.local(store);local['required']=[{'generation':str(generation),'id':'service','hash':next(r['hash'] for r in c.plan(generation) if r['id']=='service')}];store.put(component_cli.KEY,local)
        login=ci.check_login(store,generation,'worker',prefix,runner=lambda args:(0,'{"loggedIn":true}'));self.assertTrue(login['authenticated']);self.assertFalse(login['credentials_copied'])
        self.assertFalse(ci.check_login(store,generation,'worker',prefix,runner=lambda args:(1,'private fixture error never persisted'))['authenticated'])
        calls=[];directory=root/'LaunchAgents'
        self.assertFalse(ci.required_status(store)['complete'])
        denied=ci.install(store,generation,'service',stage,'service',launchdir=directory,confirm=lambda p:False,launchctl=lambda *a:calls.append(a));self.assertFalse(denied['installed']);self.assertFalse(directory.exists())
        installed=ci.install(store,generation,'service',stage,'service',launchdir=directory,confirm=lambda p:p['scope']=='persistent_install,service',launchctl=lambda *a:calls.append(a));self.assertEqual(len(calls),1);self.assertTrue(Path(installed['target']).is_file());self.assertFalse(ci.required_status(store)['complete'])
        ci.probe(store,'service',read_status=lambda _: 'last exit code = 0');self.assertTrue(ci.required_status(store)['complete'])
        with self.assertRaises(FileExistsError):ci.install(store,generation,'service',stage,'service',launchdir=directory,confirm=lambda p:True,launchctl=lambda *a:calls.append(a))
        self.assertEqual(len(calls),1)
        Path(installed['target']).write_bytes(b'changed');self.assertFalse(ci.required_status(store)['complete'])

class PlannerReviewedRecovery(unittest.TestCase):
    def test_plan_array_rejected_and_reviewed_resume_keeps_original_task(self):
        from portable import workflows,planner
        helper=OriginalScopeControl();helper.setUp();self.addCleanup(helper.doCleanups);s=helper.s;rid=helper.rid
        inspected=helper.a.call('engineering_context',{'id':rid,'files':[],'instruction_id':'new-planning-review'})
        key='planner:'+rid+':'+workflows.status(s,rid)['execution_revision']
        ref={'state':'blocked','task':'original-plan-task','request':'original-plan-request','workspace':str(helper.repo),'repo_sha256':inspected['repo']['sha256'],'inspection_revision':inspected['revision'],'inspection':inspected['inspection'],'attempts':3,'failed_run':'run3','last_fault':'PLAN_STRING_REQUIRED: plan was array'}
        from portable.team import require_readiness
        ref['request']='portable-plan:'+digest(canonical([s.device,rid,workflows.status(s,rid)['execution_revision'],ref['repo_sha256'],require_readiness(s,project='fixture')['epoch']]).encode());s.put(key,ref)
        visible=workflows.status(s,rid)['planning']
        self.assertEqual(visible['state'],'blocked');self.assertEqual(visible['task'],'original-plan-task');self.assertEqual(visible['attempts'],3);self.assertIn('PLAN_STRING_REQUIRED',visible['last_fault']);self.assertIn('planning_resume',visible['next'])
        public=helper.a.call('requirement_status',{'id':rid})['planning'];self.assertEqual(public,visible)
        calls=[]
        def call(op,args):
            if op=='get_task':return {'task_id':'original-plan-task','caller':'portable:'+s.device,'status':'awaiting_acceptance','run_id':'run3'}
            if op=='task_settled':return {'stopped':True}
            if op=='lookup_request':return {'found':False}
            if op=='continue_task':calls.append(args);return {'requested':True}
            raise AssertionError('must not create new task: '+op)
        with self.assertRaisesRegex(planner.PlanFault,'PLAN_STRING_REQUIRED'):planner.validate_result({'requirement_revision':'revision','repo_sha256':'sha','plan':['wrong shape'],'verification_commands':['true'],'review_confirmed':True},{'revision':'revision'},{'sha256':'sha'})
        with self.assertRaises(Blocked):planner.resume_reviewed(s,rid,inspected['inspection'],inspected['revision'],False,'a',call)
        result=planner.resume_reviewed(s,rid,inspected['inspection'],inspected['revision'],True,'a',call);self.assertFalse(result['new_task_created'])
        self.assertTrue(planner.resume_reviewed(s,rid,inspected['inspection'],inspected['revision'],True,'a',call)['existing'])
        planner.Planner(s,call).tick();self.assertEqual(len(calls),1);self.assertEqual(calls[0]['task_id'],'original-plan-task');self.assertIn('plan:非空string',calls[0]['spec']);self.assertIn('PLAN_STRING_REQUIRED',calls[0]['spec']);self.assertEqual(s.get(key)['reviewed_restarts'][0]['previous_attempts'],3)

class LegacyPublicEntry(unittest.TestCase):
    def test_cli_selection_mcp_preflight_all_worktrees_restore_and_inert_draft(self):
        from portable import legacy_cli as lc,transfer
        from portable.git_portable import git
        from portable.context_server import Context,enroll
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();repo=root/'repo';repo.mkdir();git(repo,'init','-q');(repo/'code.py').write_text('first');git(repo,'add','.');git(repo,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-qm','fixture');linked=root/'linked';git(repo,'worktree','add','-qb','feature',str(linked));(linked/'code.py').write_text('staged');git(linked,'add','code.py');(linked/'code.py').write_text('unstaged');(linked/'new.py').write_text('untracked')
            rule=root/'AGENTS.md';rule.write_text('Only local fixture development; no production.');state_root=root/'state';(state_root/'tasks').mkdir(parents=True);(state_root/'tasks/task-old.json').write_text(canonical({'task_id':'task-old','caller':'old-other-caller','runs':['run-old'],'status':'done'}));card=root/'card.md';card.write_text('original legacy card')
            s=Store(root/'product');self.addCleanup(s.db.close);s.register('fixture',repo,'fixture');chosen=lc.choose(s,['fixture'],['fixture='+str(rule)],histories=['old='+str(state_root)],evidence=['old=tasks/task-old.json'],archives=['cards='+str(card)],scope_reviewed=True)
            enroll(s,'fixture');ctx=Context(s,'fixture');preview=ctx.call('legacy_preflight',{'selection':chosen['selection']});self.assertEqual(len(preview['projects'][0]['worktrees']),2)
            with self.assertRaises(Blocked):ctx.call('legacy_capture',{'selection':chosen['selection'],'sensitive':False,'public_history':True,'instruction_id':'no-remote-plaintext'})
            package=root/'legacy.bundle';lc.capture(s,chosen['selection'],package,history_plaintext=True);restored=lc.restore(s,package,root/'restored');self.assertTrue(restored['complete']);self.assertEqual(s.db.execute('SELECT count(*) FROM projects').fetchone()[0],1)
            draft=ctx.call('legacy_draft',{'restored':restored['restored'],'task':'task-old','checkpoint':{'source_caller':'old-other-caller','authorization':{'forbidden':['finance'],'production':True}},'instruction_id':'draft'});self.assertEqual(draft['new_caller'],'portable:'+s.device);self.assertEqual(draft['current_execution_grants'],{});self.assertIn('finance',canonical(draft));self.assertEqual(draft['remaining'],'unknown')
            shortcut=root/'all-worktrees.bundle';result=transfer.pack(repo,shortcut);self.assertTrue(result['all_discovered_worktrees_captured']);self.assertFalse(result['complete']);transfer.restore(shortcut,root/'shortcut-restored');self.assertEqual(len(list((root/'shortcut-restored/projects/project/code').iterdir())),2)
            self.assertEqual((linked/'code.py').read_text(),'unstaged');self.assertTrue((linked/'new.py').is_file());self.assertFalse(any((s.root/'知识库').rglob('*task-old*')))
    def test_sensitive_full_git_history_uses_exact_encrypted_normalizer(self):
        from portable import legacy_cli as lc
        from portable.git_portable import git
        if not shutil.which('age-keygen'):self.skipTest('real age unavailable')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();repo=root/'repo';repo.mkdir();git(repo,'init','-q');(repo/'private.json').write_text('{"password":"SYNTHETIC_LEGACY_HISTORY_123"}');git(repo,'add','.');git(repo,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-qm','history');old=git(repo,'rev-parse','HEAD').stdout.decode().strip();(repo/'private.json').unlink();s=Store(root/'product');self.addCleanup(s.db.close);s.register('fixture',repo,'fixture');chosen=lc.choose(s,['fixture'],scope_reviewed=True)
            identity=root/'identity';process=subprocess.run(['age-keygen','-o',str(identity)],capture_output=True);self.assertEqual(process.returncode,0);identity.chmod(0o600);recipient=subprocess.check_output(['age-keygen','-y',str(identity)],stderr=subprocess.DEVNULL).decode().strip();package=root/'encrypted.bundle';lc.capture(s,chosen['selection'],package,sensitive=True,recipient=recipient);self.assertNotIn(b'SYNTHETIC_LEGACY',package.read_bytes());lc.restore(s,package,root/'restored',identity)
            raw=git(root/'restored/projects/fixture/code/0','show',old+':private.json').stdout;self.assertIn(b'SYNTHETIC_LEGACY_HISTORY_123',raw)
