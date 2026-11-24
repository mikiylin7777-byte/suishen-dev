import concurrent.futures
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from portable.core import Blocked, Store, atomic, canonical, connect_readonly, digest, safe
from portable import migration as m
from portable.bots import event,notification,private_config
from portable.distribution import build,backup,update,material_move,release_verify
from portable.team import Team

SOURCE=Path(__file__).resolve().parents[1]

def cas_worker(root,expected,content,q):
    s=Store(root)
    try:s.overview(expected,content);q.put('ok')
    except Blocked:q.put('conflict')
    finally:s.db.close()

class Product(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.t=Path(self.temp.name).resolve()
        self.s=Store(self.t/'app')
        self.src=self.t/'source';self.src.mkdir()
        self.media=self.t/'media';self.media.mkdir()
        m.enroll(self.s,self.media)
        target=patch('portable.bots.approval_target',return_value={'run_id':'synthetic-fixture'})
        target.start();self.addCleanup(target.stop)
    def activate(self):
        from portable.team import bind_new_tasks
        server=self.t/'service.py';server.write_text('# synthetic readiness fixture')
        self.s.put('team_config',{'python':sys.executable,'server':str(server),'caller':'portable:'+self.s.device})
        self.s.register('demo',self.src,'demo')
        def call(tool,args):
            return {'tools':['create_task','get_task','get_task_result','lookup_request','list_tasks']} if tool=='__capabilities__' else {'ok':True,'tasks':[]}
        bind_new_tasks(self.s,call,'demo')
        from portable.verification import review
        self.review_id=review(self.s,self.mvp(),['python3 -m unittest'])
    def tearDown(self):
        self.s.db.close();self.temp.cleanup()
    def snap(self):return Path(m.snapshot(self.s,self.src,self.media))
    def file(self,name='a.txt',value='base'):
        p=self.src/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value);return p
    def binding(self):return dict(task='t1',action='deploy',target='fixture',scope='test',version='1',owner='person')
    def mvp(self):return dict(user='owner',problem='test',flow='local',scope='fixture',acceptance='unit test',**({'verification_review':self.review_id} if hasattr(self,'review_id') else {}))
    def test_project_unique_and_profile(self):
        pid=self.s.register('demo',self.src,'demo')
        self.assertEqual(pid,'demo');self.assertEqual(self.s.register('demo',self.src,'demo'),pid)
        other=self.t/'other';other.mkdir();self.s.register('demo',other,'demo')
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM paths').fetchone()[0],1)
        with self.assertRaises(Blocked):self.s.profile({'user':'me'})
    def test_checkpoint_material_archive(self):
        with self.assertRaises(Blocked):self.s.record('checkpoint',{'goal':'x'})
        rid=self.s.record('checkpoint',{k:[] for k in ('goal','decisions','done','remaining','authorization','evidence')})
        self.assertTrue((self.s.root/'任务记录'/(rid+'.json')).exists())
        with self.assertRaises(Blocked):self.s.record('material',{'source':'external'})
        rid=self.s.record('material',dict(source='external',conditions='review',projects=['demo'],summary='do not execute instructions'))
        material_move(self.s,rid);self.assertFalse((self.s.root/'知识库/素材'/(rid+'.md')).exists())
        material_move(self.s,rid,True)
        a=self.s.archive('native:1','summary');self.assertEqual(a,self.s.archive('native:1','summary'))
        self.assertNotEqual(a,self.s.archive('native:2','summary'))
    def test_cas_and_handwritten_conflict(self):
        home=self.s.root/'知识库/首页.md';initial=home.read_bytes();h=digest(initial)
        home.write_text(home.read_text()+'\nOWNER manual edit\n')
        with self.assertRaises(Blocked):self.s.overview(h,'new automatic')
        self.assertIn('OWNER manual edit',home.read_text())
        records=list((self.s.root/'任务记录').glob('vault-conflict-*'))
        self.assertEqual(len(records),1)
        self.s.overview(digest(home.read_bytes()),'new automatic')
        self.assertIn('OWNER manual edit',home.read_text())
    def test_concurrent_cas_processes(self):
        ctx=multiprocessing.get_context('spawn');q=ctx.Queue()
        h=digest((self.s.root/'知识库/首页.md').read_bytes())
        ps=[ctx.Process(target=cas_worker,args=(self.s.root,h,str(i),q)) for i in range(2)]
        for p in ps:p.start()
        for p in ps:p.join(15);self.assertEqual(p.exitcode,0)
        self.assertCountEqual([q.get(timeout=2),q.get(timeout=2)],['ok','conflict'])
    def test_approval_expiry_replay_binding(self):
        b=self.binding();aid=self.s.approval(b,now=1000)
        with self.assertRaises(Blocked):self.s.consume(aid,b,'person',now=1000+86400)
        aid=self.s.approval(b)
        with self.assertRaises(Blocked):self.s.consume(aid,{**b,'scope':'other'},'person')
        self.s.consume(aid,b,'person')
        with self.assertRaises(Blocked):self.s.consume(aid,b,'person')
    def test_bot_dedupe_cross_channel_approval(self):
        cfg={'owners':{'feishu':{'f':'person'},'wecom':{'w':'person'}}}
        b=self.binding();aid=__import__('portable.bots',fromlist=['issue_approval']).issue_approval(self.s,b)
        value=dict(channel='feishu',identity='f',id='message1',body={'command':'approve','id':aid,'binding':b})
        self.assertTrue(event(self.s,cfg,value)['approved'])
        self.assertTrue(event(self.s,cfg,value)['duplicate'])
        with self.assertRaises(Blocked):event(self.s,cfg,{**value,'channel':'wecom','identity':'w','id':'m2'})
        with self.assertRaises(Blocked):event(self.s,cfg,{**value,'identity':'attacker','id':'m3'})
        ordinary={**value,'id':'m4','body':'please deploy everything'}
        self.assertFalse(event(self.s,cfg,ordinary)['execution'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
    def test_notification_whitelist_and_replay(self):
        cfg={'owners':{'feishu':{'f':'person'}}}
        v=dict(channel='feishu',target='f',kind='completed',id='n1')
        self.assertTrue(notification(self.s,cfg,v)['send_allowed'])
        with self.assertRaises(Blocked):notification(self.s,cfg,v)
        with self.assertRaises(Blocked):notification(self.s,cfg,{**v,'id':'n2','kind':'progress'})
    def test_private_settings_permissions(self):
        path=self.t/'private.json';path.write_text('{"owners":{"feishu":{"f":"person"}}}');path.chmod(0o644)
        with self.assertRaises(Blocked):private_config(path)
        path.chmod(0o600);self.assertTrue(private_config(path)['owners'])
    def test_team_intent_dedupe_unknown(self):
        with self.assertRaises(Blocked):self.s.queue('implement',self.mvp())
        self.activate() # synthetic fixture only, no CLI promotes a real device.
        with self.assertRaises(Blocked):self.s.queue('analyze',self.mvp())
        key=self.s.queue('implement',self.mvp());self.assertEqual(key,self.s.queue('implement',self.mvp()))
        calls=[]
        def transport(tool,args):
            calls.append(tool)
            if tool=='lookup_request':raise Blocked('missing lookup capability')
            self.assertEqual(self.s.db.execute('SELECT state FROM outbox WHERE id=?',(key,)).fetchone()[0],'sending')
            raise TimeoutError()
        team=Team(self.s,transport)
        with self.assertRaises(Blocked):team.submit(key,str(self.src))
        with self.assertRaises(Blocked):team.submit(key,str(self.src))
        self.assertEqual(calls,['create_task','lookup_request'])
        with self.assertRaises(Blocked):team.action('accept','t1')
    def test_team_reference_queries_no_duplicate_create(self):
        self.activate();key=self.s.queue('implement',self.mvp());calls=[]
        def send(tool,args):calls.append(tool);return {'task_id':'fixture-task','status':'awaiting_acceptance'}
        team=Team(self.s,send);team.submit(key,str(self.src));team.submit(key,str(self.src))
        self.assertEqual(calls,['create_task','get_task'])
        self.assertEqual(self.s.db.execute('SELECT state FROM outbox').fetchone()[0],'submitted')
    def test_complete_restore_and_unknown_target(self):
        self.file();(self.src/'empty').mkdir();snap=self.snap();out=self.t/'restored';m.restore(snap,out)
        self.assertEqual((out/'a.txt').read_text(),'base');self.assertTrue((out/'empty').is_dir())
        with self.assertRaises(Blocked):m.restore(snap,out)
    def test_interrupted_never_published(self):
        self.file();self.file('b.txt')
        with self.assertRaises(Blocked):m.snapshot(self.s,self.src,self.media,fail_after=1)
        self.assertEqual(list(self.media.glob('snapshot-*')),[])
        half=next(self.media.glob('.partial-*'))
        with self.assertRaises(Blocked):m.restore(half,self.t/'out')
    def test_tamper_missing_extra_files(self):
        self.file();snap=self.snap();(snap/'payload/a.txt').write_text('bad')
        with self.assertRaises(Blocked):m.restore(snap,self.t/'out')
        (snap/'payload/a.txt').write_text('base');(snap/'payload/unknown').write_text('x')
        with self.assertRaises(Blocked):m.verify(snap)
    def test_manifest_traversal_and_links(self):
        self.file();snap=self.snap();manifest=json.loads((snap/'manifest.json').read_text())
        manifest['files']['../escape']=manifest['files'].pop('a.txt')
        atomic(snap/'manifest.json',canonical(manifest));atomic(snap/'COMPLETE',digest((snap/'manifest.json').read_bytes()))
        with self.assertRaises(Blocked):m.restore(snap,self.t/'out')
        self.assertFalse((self.t/'escape').exists())
    def test_source_and_destination_link_escape(self):
        self.file();(self.src/'link').symlink_to(self.t)
        with self.assertRaises(Blocked):self.snap()
        (self.src/'link').unlink();snap=self.snap();(self.t/'link').symlink_to(self.t/'app')
        with self.assertRaises(Blocked):m.restore(snap,self.t/'link/out')
        (snap/'payload/a.txt').unlink();(snap/'payload/a.txt').symlink_to(self.file('outside.txt'))
        with self.assertRaises(Blocked):m.verify(snap)
    def test_unknown_medium_and_space(self):
        other=self.t/'unknown';other.mkdir();self.file()
        with self.assertRaises(Blocked):m.snapshot(self.s,self.src,other)
        with patch('portable.migration.shutil.disk_usage',return_value=shutil._ntuple_diskusage(10,10,0)):
            with self.assertRaises(Blocked):self.snap()
        self.assertFalse(list(self.media.glob('snapshot-*')))
    def test_ordinary_snapshot_secret_block_includes_guidance(self):
        phrases=('这是升级前的安全检查','不要手改数据库','只读方式确认是不是真密钥','测试样例','报总管或用户','脱敏后重试','【密钥】')
        self.file('.env','name=fixture')
        with self.assertRaises(Blocked) as caught:self.snap()
        text=str(caught.exception)
        self.assertIn('敏感文件不得进入普通快照',text)
        for phrase in phrases:self.assertIn(phrase,text)
        (self.src/'.env').unlink()
        self.file('note.txt','api_key = SYNTHETIC_FIXTURE_ONLY')
        with self.assertRaises(Blocked) as caught:self.snap()
        text=str(caught.exception)
        self.assertIn('疑似秘密内容',text)
        for phrase in phrases:self.assertIn(phrase,text)
    def test_secret_and_special_git_block(self):
        self.file('.env','SYNTHETIC_ONLY=value')
        with self.assertRaises(Blocked):self.snap()
        (self.src/'.env').unlink();self.file('config.txt','api_key = SYNTHETIC_FIXTURE_ONLY')
        with self.assertRaises(Blocked):self.snap()
        (self.src/'config.txt').unlink();self.file('.git','gitdir: /unknown/path')
        with self.assertRaises(Blocked):self.snap()
    def test_empty_password_in_review_archive_allows_snapshot(self):
        self.file('数据/复盘/20261101-note.txt','ERP_DB_PASSWORD=""\n')
        snap=self.snap()
        self.assertEqual((snap/'payload/数据/复盘/20261101-note.txt').read_text(),'ERP_DB_PASSWORD=""\n')
        self.file('数据/复盘/20261101-note.txt','ERP_DB_PASSWORD="abc12345"\n')
        with self.assertRaises(Blocked):self.snap()
    def test_sqlite_consistent_online_backup(self):
        db=sqlite3.connect(self.src/'live.sqlite');db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE t(v)');db.execute('INSERT INTO t VALUES (42)');db.commit()
        snap=self.snap();out=self.t/'restored';m.restore(snap,out)
        restored=sqlite3.connect(out/'live.sqlite');self.assertEqual(restored.execute('SELECT v FROM t').fetchone()[0],42);restored.close();db.close()
    def test_database_unknown_plugin_block(self):
        self.file('unknown.db','not sqlite')
        with self.assertRaises(Blocked):self.snap()
    def test_git_history_index_unstaged_untracked(self):
        def git(*args):return m.git(self.src,*args)
        subprocess.run(['git','init','-q',str(self.src)],check=True)
        self.file();git('add','a.txt');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','unpublished fixture')
        self.file(value='staged');git('add','a.txt');self.file(value='unstaged');self.file('untracked.txt','keep')
        before=git('status','--porcelain=v1','-z');head=git('rev-parse','HEAD');index=(self.src/'.git/index').read_bytes()
        snap=self.snap();out=self.t/'restore-git';m.restore(snap,out)
        self.assertEqual((out/'.git/index').read_bytes(),index)
        self.assertEqual(m.git(out,'status','--porcelain=v1','-z'),before)
        self.assertEqual(m.git(out,'rev-parse','HEAD'),head)
        # Git status may refresh index metadata; check staged blob separately.
        self.assertEqual(m.git(out,'show',':a.txt'),b'staged')
        self.assertEqual((out/'a.txt').read_text(),'unstaged');self.assertEqual((out/'untracked.txt').read_text(),'keep')
    def test_git_unknown_ignored_block(self):
        subprocess.run(['git','init','-q',str(self.src)],check=True)
        self.file('.gitignore','ignored\n');self.file('ignored','unknown')
        with self.assertRaises(Blocked):self.snap()
    def test_three_way_conflict_and_database_not_text_merge(self):
        roots=[self.t/n for n in ('base','left','right')]
        for p in roots:p.mkdir()
        for p,v in zip(roots,('base','left','right')):(p/'a.txt').write_text(v)
        for p in roots:
            db=sqlite3.connect(p/'state.sqlite');db.execute('CREATE TABLE t(v)');db.execute('INSERT INTO t VALUES (?)',(p.name,));db.commit();db.close()
        result=m.reconcile(*roots,self.t/'merged')
        self.assertEqual(set(result['conflicts']),{'a.txt','state.sqlite'})
        self.assertEqual((self.t/'merged/冲突/left/a.txt').read_text(),'left')
        self.assertFalse((self.t/'merged/合并/state.sqlite').exists())
    def test_three_way_nonconflict_no_mtime(self):
        roots=[self.t/n for n in ('base','left','right')]
        for p in roots:p.mkdir();(p/'a').write_text('base')
        (roots[1]/'a').write_text('left');os.utime(roots[2]/'a',(time.time()+1000,)*2)
        m.reconcile(*roots,self.t/'merged');self.assertEqual((self.t/'merged/合并/a').read_text(),'left')
    def test_finalize_fail_closed(self):
        self.activate()
        with self.assertRaises(Blocked):m.finalize(self.s)
    def test_sync_window(self):
        self.file();self.s.register('demo',self.src,'demo')
        with self.s.db:self.s.db.execute('INSERT INTO changes VALUES (?,?)',('demo',time.time()))
        self.assertTrue(m.sync(self.s,'demo',self.media)['pending'])
        with self.s.db:self.s.db.execute('UPDATE changes SET changed=?',(time.time()-31,))
        self.assertIn('snapshot',m.sync(self.s,'demo',self.media))
    def test_backup_and_update_block_active(self):
        release=self.t/'release';candidate=self.t/'candidate';build(SOURCE,release);build(SOURCE,candidate)
        s=Store(release)
        try:
            saved=backup(s,self.t/'backups');self.assertTrue(Path(saved['backup'],'BACKUP.json').exists())
            self.assertTrue(update(s,candidate)['backup_required'])
            s.put('execution','active')
            with self.assertRaises(Blocked):update(s,candidate,self.t/'upgrades')
            s.put('execution','unbound');self.assertTrue(update(s,candidate,self.t/'upgrades')['applied'])
            self.assertEqual(s.doctor()['database'],'ok')
        finally:s.db.close()
    def test_independent_release_source_removed_restore(self):
        # Build from a disposable source, then make BOTH its sources and release unavailable.
        isolated=self.t/'isolated-source';isolated.mkdir()
        for name in ('portable','adapters','scripts'):
            shutil.copytree(SOURCE/name,isolated/name,ignore=shutil.ignore_patterns('__pycache__','node_modules'))
        for name in ('AGENTS.md','CLAUDE.md','使用说明.md'):shutil.copy2(SOURCE/name,isolated/name)
        release=self.t/'release';build(isolated,release)
        # Only fixture code is portable; real personal projects are never copied.
        code=release/'代码/demo';code.mkdir();(code/'README.md').write_text('fixture')
        s=Store(release);s.register('demo',code,'demo');device=s.device;s.db.close()
        snap=Path(m.snapshot(self.s,release,self.media,'portable'))
        isolated.rename(self.t/'source-unavailable');release.rename(self.t/'release-unavailable')
        restored=self.t/'restored';m.restore(snap,restored)
        env={**os.environ,'PYTHONPATH':'','PYTHONNOUSERSITE':'1'}
        r=subprocess.run([str(restored/'开始使用.command'),'doctor'],cwd=self.t,env=env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        result=json.loads(r.stdout);self.assertEqual(result['database'],'ok');self.assertNotEqual(result['device'],device)
        self.assertEqual(result['execution'],'restored-readonly')
        rs=Store(restored)
        try:
            self.assertEqual(rs.db.execute('SELECT path FROM paths WHERE device=?',(rs.device,)).fetchone()[0],str(restored/'代码/demo'))
            with self.assertRaises(Blocked):rs.queue('implement',self.mvp())
        finally:rs.db.close()
    def test_store_opens_checkpointed_wal_without_shm(self):
        # Python 3.9 cannot mode=ro a checkpointed WAL database once -shm is gone.
        root=self.s.root
        self.s.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db=self.s.dbpath
        self.s.db.close()
        class _Quiet:
            class db:
                @staticmethod
                def close():
                    return None
        self.s=_Quiet()
        for suffix in ('-wal','-shm'):
            sidecar=Path(str(db)+suffix)
            if sidecar.is_file() and not sidecar.is_symlink():sidecar.unlink()
        self.assertFalse(Path(str(db)+'-shm').exists())
        self.assertEqual(db.read_bytes()[18],2)
        before=db.read_bytes()
        check=connect_readonly(db)
        try:self.assertEqual(check.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0],'1')
        finally:check.close()
        if sys.version_info<(3,10):self.assertFalse(Path(str(db)+'-shm').exists())
        self.s=Store(root)
        self.assertEqual(self.s.get('schema'),'1')
        self.assertEqual(db.read_bytes()[:16],before[:16])
    def test_readonly_open_keeps_uncheckpointed_wal_and_does_not_write_user_db(self):
        src=self.t/'live.sqlite'
        held=sqlite3.connect(src)
        held.execute('PRAGMA journal_mode=WAL')
        held.execute('PRAGMA wal_autocheckpoint=0')
        held.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        held.execute("INSERT INTO meta VALUES ('schema','1')")
        held.commit()
        held.execute("INSERT INTO meta VALUES ('extra','pending')")
        held.commit()
        folder=self.t/'quiescent';folder.mkdir()
        db=folder/'state.sqlite'
        shutil.copyfile(src,db)
        shutil.copyfile(Path(str(src)+'-wal'),Path(str(db)+'-wal'))
        held.close()
        before=db.read_bytes()
        wal_before=Path(str(db)+'-wal').read_bytes()
        self.assertFalse(Path(str(db)+'-shm').exists())
        check=connect_readonly(db)
        try:
            self.assertEqual(check.execute("SELECT value FROM meta WHERE key='extra'").fetchone()[0],'pending')
        finally:check.close()
        self.assertEqual(db.read_bytes(),before)
        self.assertEqual(Path(str(db)+'-wal').read_bytes(),wal_before)
    def test_copied_directory_changes_identity_and_disables_execution(self):
        self.activate();self.s.put('team_config',{'caller':'portable:old'})
        self.s.db.execute('PRAGMA wal_checkpoint(FULL)')
        copied=self.t/'copied';shutil.copytree(self.s.root,copied)
        new=Store(copied)
        try:
            self.assertNotEqual(new.device,self.s.device)
            self.assertEqual(new.get('execution'),'copied-readonly')
            self.assertEqual(new.get('team_config'),{})
            with self.assertRaises(Blocked):new.queue('implement',self.mvp())
        finally:new.db.close()
    def test_foreign_host_same_path_changes_identity(self):
        old=self.s.device
        with patch('portable.core.host_identity',return_value='synthetic-foreign-host'):
            new=Store(self.s.root)
            try:
                self.assertNotEqual(new.device,old)
                self.assertEqual(new.get('execution'),'copied-readonly')
            finally:new.db.close()
    def test_git_deleted_historical_secret_is_blocked(self):
        subprocess.run(['git','init','-q',str(self.src)],check=True)
        self.file('.env','SYNTHETIC=value')
        m.git(self.src,'add','.env');m.git(self.src,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','synthetic secret')
        m.git(self.src,'rm','.env');m.git(self.src,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','delete fixture')
        with self.assertRaises(Blocked):self.snap()
    def test_update_path_whitelist_and_rollback_new_files_mode(self):
        release=self.t/'release';candidate=self.t/'candidate';build(SOURCE,release);build(SOURCE,candidate)
        manifest=json.loads((candidate/'发行.json').read_text())
        manifest['files']['知识库/首页.md']=digest((candidate/'知识库/首页.md').read_bytes())
        atomic(candidate/'发行.json',canonical(manifest))
        with self.assertRaises(Blocked):release_verify(candidate)
        del manifest['files']['知识库/首页.md']
        extra='工具/adapters/bots/new-file.mjs';(candidate/extra).write_text('// fixture')
        manifest['files'][extra]=digest((candidate/extra).read_bytes());atomic(candidate/'发行.json',canonical(manifest))
        s=Store(release)
        before=(release/'开始使用.command').read_bytes();mode=(release/'开始使用.command').stat().st_mode
        from portable import distribution as d
        real=d.release_verify;count=0
        def fail_final(path):
            nonlocal count
            count+=1
            if count==3:raise Blocked('synthetic final verification failure')
            return real(path)
        try:
            with patch('portable.distribution.release_verify',side_effect=fail_final):
                with self.assertRaises(Blocked):update(s,candidate,self.t/'backups')
            self.assertEqual((release/'开始使用.command').read_bytes(),before)
            self.assertEqual((release/'开始使用.command').stat().st_mode,mode)
            self.assertFalse((release/extra).exists())
            release_verify(release)
        finally:s.db.close()
    def test_manual_daily_backup_counts_for_today_and_records_gap(self):
        import datetime
        self.s.put('runner:daily',(datetime.date.today()-datetime.timedelta(days=3)).isoformat())
        backup(self.s,self.t/'backups','daily')
        self.assertEqual(self.s.get('runner:daily'),datetime.date.today().isoformat())
        self.assertEqual(self.s.get('runner:backup-gap')['missed_days'],2)

    def test_backup_skips_review_lock_dir(self):
        # 2026-11-15/16 事故：复盘留下的空锁文件让每日备份一直失败
        lock=self.s.root/'数据/复盘/.锁/demo.lock';lock.parent.mkdir(parents=True,exist_ok=True);lock.write_text('')
        r=backup(self.s,self.t/'backups','daily');self.assertTrue(Path(r['backup']).is_dir())
        self.assertFalse((Path(r['backup'])/'数据/复盘/.锁/demo.lock').exists())

    def test_backup_rotation_deletes_expired(self):
        # 11-14：过期的自动备份直接删（升级留 2 份），不再挪进恢复区越攒越多；不是本程序备份的目录不动
        stranger=self.t/'backups/upgrade-20990101-000000-x';stranger.mkdir(parents=True);(stranger/'别人的.txt').write_text('keep')
        for i in range(4):r=backup(self.s,self.t/'backups','upgrade')
        mine=[p for p in (self.t/'backups').glob('upgrade-*') if (p/'BACKUP.json').is_file()]
        self.assertEqual(len(mine),2);self.assertFalse((self.t/'backups/恢复区').exists())
        self.assertTrue((stranger/'别人的.txt').is_file())
        self.assertTrue(r['permanent_delete'])
        from portable.distribution import expire_backups
        link=self.t/'backups/upgrade-link';link.symlink_to(mine[0])
        self.assertEqual(expire_backups(self.t/'backups',[link],'BACKUP.json'),[]);self.assertTrue(mine[0].exists())
    def test_installed_bot_bridge_no_source_dependency(self):
        release=self.t/'release';build(SOURCE,release)
        cfg=self.t/'private.json';cfg.write_text(canonical({'owners':{'feishu':{'f':'person'}}}));cfg.chmod(0o600)
        req={'operation':'event','value':{'channel':'feishu','identity':'f','id':'m1','body':'ordinary material'}}
        p=subprocess.run([sys.executable,str(release/'工具/bot_bridge.py'),str(release),str(cfg)],input=canonical(req),capture_output=True,text=True,cwd=self.t,env={**os.environ,'PYTHONPATH':''})
        self.assertEqual(p.returncode,0,p.stderr);self.assertFalse(json.loads(p.stdout)['execution'])
    def test_lifecycle_durable_unknown_queries_only(self):
        self.activate();calls=[]
        def send(tool,args):
            calls.append(tool)
            if tool=='lookup_request':return {'found':True,'state':'unknown'}
            if tool=='get_task':return {'task_id':'fixture','run_id':'r1','status':'awaiting_acceptance','workspace':str(self.src),'caller':'portable:'+self.s.device}
            self.assertEqual(self.s.db.execute('SELECT state FROM outbox').fetchone()[0],'sending')
            raise TimeoutError()
        team=Team(self.s,send)
        key=team.new_instruction('reverify_task',{'task_id':'fixture'})['instruction_id']
        with self.assertRaises(Blocked):team.lifecycle('reverify_task',{'task_id':'fixture'},key)
        result=team.lifecycle('reverify_task',{'task_id':'fixture'},key)
        self.assertEqual(calls,['get_task','get_task','get_task','get_task','reverify_task','get_task','lookup_request']);self.assertEqual(result['state'],'unknown')
        self.s.put('execution','unbound')
        with self.assertRaises(Blocked):team.lifecycle('accept_task',{'task_id':'fixture'})
    def test_observer_persistent_debounce(self):
        self.file();self.s.register('demo',self.src,'demo')
        self.assertTrue(m.observe(self.s,'demo',now=100)['changed'])
        self.assertFalse(m.observe(self.s,'demo',now=110)['changed'])
        self.assertEqual(self.s.db.execute('SELECT changed FROM changes').fetchone()[0],100)
        self.file(value='changed');self.assertTrue(m.observe(self.s,'demo',now=120)['changed'])
        self.assertEqual(self.s.db.execute('SELECT changed FROM changes').fetchone()[0],120)
    def test_nested_git_worktree_is_blocked(self):
        self.file('代码/demo/.git','gitdir: /external/unknown')
        with self.assertRaises(Blocked):self.snap()
    def test_managed_symlink_and_foreign_database_are_not_initialized(self):
        root=self.t/'unsafe';root.mkdir();(root/'数据').symlink_to(self.t/'app/数据')
        with self.assertRaises(Blocked):Store(root)
        unknown=self.t/'unknown-app/数据';unknown.mkdir(parents=True)
        c=sqlite3.connect(unknown/'state.sqlite');c.execute('CREATE TABLE real_data(v)');c.commit();c.close()
        before=(unknown/'state.sqlite').read_bytes()
        with self.assertRaises(Blocked):Store(unknown.parent)
        self.assertEqual((unknown/'state.sqlite').read_bytes(),before)
    def test_standard_worktree_independent_restore_preserves_changes(self):
        main=self.t/'main-repo';main.mkdir()
        subprocess.run(['git','init','-q',str(main)],check=True)
        (main/'a.txt').write_text('base');m.git(main,'add','a.txt');m.git(main,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','base')
        wt=self.t/'worktree';m.git(main,'worktree','add','-b','portable-fixture',str(wt))
        (wt/'a.txt').write_text('staged');m.git(wt,'add','a.txt');(wt/'a.txt').write_text('unstaged');(wt/'untracked').write_text('keep')
        head=m.git(wt,'rev-parse','HEAD');status=m.git(wt,'status','--porcelain=v1','-z')
        snapshot=m.snapshot(self.s,wt,self.media,'worktree')
        main.rename(self.t/'main-unavailable');wt.rename(self.t/'worktree-unavailable')
        out=self.t/'restored-worktree';m.restore(snapshot,out)
        self.assertTrue((out/'.git').is_dir());self.assertEqual(m.git(out,'rev-parse','HEAD'),head)
        self.assertEqual(m.git(out,'status','--porcelain=v1','-z'),status)
        self.assertEqual(m.git(out,'show',':a.txt'),b'staged');self.assertEqual((out/'a.txt').read_text(),'unstaged')
    def test_known_ignored_cache_classification_not_unknown_drop(self):
        import py_compile
        subprocess.run(['git','init','-q',str(self.src)],check=True)
        self.file('.gitignore','__pycache__/\n');p=self.file('module.py','print(1)')
        # macOS 自带的 python3 把字节码写到 ~/Library/Caches，不会出现在仓库里。
        # 分类看的是仓库内被 git 忽略的 .pyc，所以测试把文件直接写进 __pycache__。
        pyc=self.src/'__pycache__'/f'module.{sys.implementation.cache_tag}.pyc'
        pyc.parent.mkdir()
        py_compile.compile(str(p),cfile=str(pyc),doraise=True)
        self.assertEqual(pyc.resolve().parent.parent,self.src.resolve())
        snap=self.snap();manifest=m.verify(snap)
        self.assertTrue(manifest['excluded_derived'])
        self.assertFalse(any(k.endswith('.pyc') for k in manifest['files']))
        self.file('__pycache__/unknown.txt','personal content')
        with self.assertRaises(Blocked):self.snap()
    def test_bot_natural_queries_and_context_bound_approval(self):
        cfg={'owners':{'feishu':{'f':'person'},'wecom':{'w':'person'}}}
        self.s.register('demo',self.src,'demo')
        def send(mid,text):return event(self.s,cfg,dict(channel='feishu',identity='f',id=mid,body=text))
        self.assertIn('demo',send('1','查看项目')['reply'])
        self.assertIn('demo',send('2','选择项目 demo')['reply'])
        draft='请实施：\n用户：本人\n问题：整理文档\n核心流程：本地\n首版范围：fixture\n验收：测试'
        self.assertIn('未派工',send('3',draft)['reply'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        aid=__import__('portable.bots',fromlist=['issue_approval']).issue_approval(self.s,self.binding());self.assertTrue(send('4','批准 '+aid)['approved'])
        with self.assertRaises(Blocked):event(self.s,cfg,dict(channel='wecom',identity='w',id='5',body='批准 '+aid))
        calls=[]
        def backend(tool,args):
            calls.append(tool)
            self.assertEqual(self.s.db.execute('SELECT state FROM outbox').fetchone()[0],'sending')
            return {'task_id':'synthetic-task','status':'queued'}
        self.activate() # synthetic route fixture; never a product activation command
        with patch('portable.team.transport',return_value=backend):
            self.assertFalse(send('6',draft)['execution'])
        self.assertEqual(calls,[])
    def test_password_age_non_tty_rejected(self):
        from portable.tools import encrypt,decrypt_sensitive
        p=self.file('fixture-sensitive','fixture')
        with patch('portable.tools.shutil.which',return_value='/fixture/age'),patch('sys.stdin.isatty',return_value=False):
            with self.assertRaises(Blocked):encrypt(p,self.t/'new.age')
            with self.assertRaises(Blocked):decrypt_sensitive(p,self.t/'new-sensitive')
    def test_setup_menu_never_enables_execution(self):
        from portable.console import setup,menu
        with patch('sys.stdin.isatty',return_value=False):
            self.assertEqual(menu(self.s)['execution'],'unbound')
            with self.assertRaises(Blocked):setup(self.s)
        answers=['fixture-user','goal','demo','MVP workflow','T3','external fixture','completed','yes']
        with patch('sys.stdin.isatty',return_value=True),patch('builtins.input',side_effect=answers):
            self.assertFalse(setup(self.s)['execution_enabled'])
        self.assertEqual(self.s.get('execution'),'unbound')
    def test_readiness_requires_handshake_and_scopes_new_requests(self):
        from portable.team import require_readiness
        self.s.put('execution','active')
        with self.assertRaises(Blocked):self.s.queue('implement',self.mvp())
        self.activate();key=self.s.queue('implement',self.mvp())
        other=self.t/'other-workspace';other.mkdir()
        with self.assertRaises(Blocked):Team(self.s,lambda *args:None).submit(key,str(other))
        server=Path(self.s.get('team_config')['server']);server.write_text('# changed service')
        with self.assertRaises(Blocked):require_readiness(self.s)
    def test_actual_age_synthetic_public_key_roundtrip(self):
        from portable.tools import encrypt
        age=shutil.which('age');keygen=shutil.which('age-keygen')
        if not age or not keygen:self.skipTest('EXTERNAL_REQUIRED: age not installed; no encryption claim')
        private=self.t/'synthetic-age-key'
        generated=subprocess.run([keygen,'-o',str(private)],capture_output=True)
        self.assertEqual(generated.returncode,0,'synthetic key generation failed')
        pub=subprocess.run([keygen,'-y',str(private)],capture_output=True,text=True,check=True).stdout.strip()
        original=self.file('sensitive-fixture.txt','synthetic sensitive fixture only')
        cipher=self.t/'sensitive.age';encrypt(original,cipher,pub)
        restored=self.t/'restored-sensitive'
        result=subprocess.run([age,'-d','-i',str(private),'-o',str(restored),str(cipher)],capture_output=True)
        self.assertEqual(result.returncode,0,'synthetic decryption failed')
        self.assertEqual(restored.read_bytes(),original.read_bytes())
        self.assertEqual(private.stat().st_mode & 0o077,0)
    def test_actual_password_age_roundtrip_private_pty(self):
        import pty,select,signal
        if not shutil.which('age'):self.skipTest('EXTERNAL_REQUIRED: age unavailable')
        original=self.file('password-fixture.txt','synthetic password-mode fixture only')
        cipher=self.t/'password-fixture.age';restored=self.t/'password-restored'
        phrase=b'only-synthetic-fixture-phrase-2026'
        def run(arguments,prompts):
            pid,master=pty.fork()
            if pid==0:
                os.chdir(SOURCE)
                os.execv(sys.executable,[sys.executable,'-m','portable','--root',str(self.t/'pty-app'),*arguments])
            stage=0;buffer=b'';status=None;deadline=time.monotonic()+15
            try:
                while time.monotonic()<deadline:
                    ready,_,_=select.select([master],[],[],.1)
                    if ready:
                        try:chunk=os.read(master,4096)
                        except OSError:chunk=b''
                        buffer=(buffer+chunk)[-8192:]
                        if stage<len(prompts) and prompts[stage] in buffer.lower():
                            os.write(master,phrase+b'\n');stage+=1;buffer=b''
                    ended,code=os.waitpid(pid,os.WNOHANG)
                    if ended:status=code;break
                if status is None:
                    os.kill(pid,signal.SIGTERM);os.waitpid(pid,0)
                    self.fail('private password PTY timed out; captured output intentionally not logged')
                self.assertEqual(os.waitstatus_to_exitcode(status),0,'private PTY operation failed; output intentionally not logged')
                self.assertEqual(stage,len(prompts))
            finally:os.close(master)
        run(['encrypt',str(original),str(cipher)],[b'enter passphrase',b'confirm passphrase'])
        run(['decrypt-sensitive',str(cipher),str(restored)],[b'enter passphrase'])
        self.assertEqual(restored.read_bytes(),original.read_bytes())
        self.assertEqual(restored.stat().st_mode & 0o077,0)
    def test_age_missing_synthetic_only(self):
        from portable.tools import encrypt
        synthetic=self.file('synthetic-sensitive.txt','fixture-only-not-real')
        with patch('portable.tools.shutil.which',return_value=None):
            with self.assertRaises(Blocked):encrypt(synthetic,self.t/'out.age','age1fixture')
        self.assertFalse((self.t/'out.age').exists())

if __name__=='__main__':unittest.main()
