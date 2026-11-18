import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from portable import portability as p, git_portable as g, db_adapters as d
from portable.core import Blocked

class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
    def repo(self,name='repo'):
        r=self.root/name;r.mkdir();g.git(r,'init');g.git(r,'config','user.name','Fixture');g.git(r,'config','user.email','fixture@invalid');(r/'a').write_text('base\n');g.git(r,'add','.');g.git(r,'commit','-m','base');return r
    def test_staged_unstaged_untracked_refs_preserved(self):
        r=self.repo();(r/'a').write_text('staged');g.git(r,'add','a');(r/'a').write_text('unstaged');(r/'u').write_text('unknown');g.git(r,'tag','unpublished');before=g.fingerprint(r)
        out=self.root/'export';g.export(r,out)
        self.assertEqual(g.git(r,'status','--porcelain').stdout,g.git(out,'status','--porcelain').stdout);self.assertEqual(g.git(r,'show',':a').stdout,g.git(out,'show',':a').stdout);self.assertEqual(before,g.fingerprint(r));shutil.rmtree(r);g.git(out,'fsck','--full');self.assertEqual((out/'u').read_text(),'unknown')
    def test_worktree_config_and_no_old_path(self):
        r=self.repo();wt=self.root/'wt';g.git(r,'worktree','add','-b','topic',str(wt));g.git(r,'config','extensions.worktreeConfig','true');g.git(wt,'config','--worktree','core.autocrlf','false');(wt/'a').write_text('edit');out=self.root/'export';g.export(wt,out);self.assertTrue((out/'.git/portable-worktree-config').exists());shutil.rmtree(r);shutil.rmtree(wt);g.git(out,'fsck','--full');self.assertFalse((out/'.git/commondir').exists())
    def test_alternates_materialized(self):
        r=self.repo();clone=self.root/'shared';g.git(r,'clone','--shared',str(r),str(clone));out=self.root/'out';g.export(clone,out);shutil.rmtree(r);shutil.rmtree(clone);g.git(out,'fsck','--full');self.assertFalse((out/'.git/objects/info/alternates').exists())
    def test_shallow_fails_with_boundary(self):
        r=self.repo();clone=self.root/'shallow';g.git(r,'clone','--depth=1','file://'+str(r),str(clone))
        with self.assertRaisesRegex(Blocked,'shallow history boundary'):g.export(clone,self.root/'out')
    def test_submodule_recursive_independent(self):
        sub=self.repo('sub');r=self.repo();res=subprocess.run(['git','-c','protocol.file.allow=always','-C',str(r),'submodule','add',str(sub),'modules/sub'],capture_output=True);self.assertEqual(res.returncode,0,res.stderr);g.git(r,'commit','-am','sub');out=self.root/'out';g.export(r,out);shutil.rmtree(r);shutil.rmtree(sub);g.git(out/'modules/sub','fsck','--full');self.assertTrue((out/'modules/sub/.git').is_dir())
    def test_lfs_missing_and_valid(self):
        r=self.repo();data=b'large fixture';oid=hashlib.sha256(data).hexdigest();(r/'asset').write_text('version https://git-lfs.github.com/spec/v1\noid sha256:'+oid+'\nsize '+str(len(data))+'\n');g.git(r,'add','.');g.git(r,'commit','-m','lfs')
        with self.assertRaisesRegex(Blocked,'missing LFS'):g.export(r,self.root/'bad')
        obj=r/'.git/lfs/objects'/oid[:2]/oid[2:4]/oid;obj.parent.mkdir(parents=True);obj.write_bytes(data);g.export(r,self.root/'good');self.assertEqual((self.root/'good'/obj.relative_to(r)).read_bytes(),data);g.prepare(r,self.root/'prepared',fetch=True,remote='file://'+str(r));self.assertEqual((self.root/'prepared'/obj.relative_to(r)).read_bytes(),data)
    def test_safe_links_external_registration_unknown_ignored_cache(self):
        r=self.repo();(r/'link').symlink_to(r/'a');external=self.root/'extra';external.write_text('registered');(r/'outside').symlink_to(external);(r/'.gitignore').write_text('node_modules/\nunknown\n');(r/'package-lock.json').write_text('{}');(r/'node_modules').mkdir();(r/'node_modules/cache').write_text('rebuild');(r/'unknown').write_text('preserve')
        with self.assertRaisesRegex(Blocked,'external link'):g.export(r,self.root/'bad')
        g.export(r,self.root/'good',external_links={'outside':str(external)});out=self.root/'good';self.assertFalse((out/'node_modules').exists());self.assertEqual((out/'unknown').read_text(),'preserve');self.assertFalse(os.path.isabs(os.readlink(out/'link')));self.assertEqual((out/'outside').read_text(),'registered')
    def test_cache_exclusion_preserves_tracked_staged_unstaged_and_needed_untracked(self):
        r=self.repo();(r/'.gitignore').write_text('node_modules/cache/\n')
        (r/'package-lock.json').write_text('{}');(r/'node_modules/cache').mkdir(parents=True)
        owned=r/'node_modules/owned.js';owned.write_text('committed')
        g.git(r,'add','.');g.git(r,'commit','-m','tracked dependency fixture')
        owned.write_text('staged');g.git(r,'add','node_modules/owned.js');owned.write_text('unstaged-essential')
        (r/'node_modules/cache/disposable').write_text('cache');(r/'node_modules/necessary.js').write_text('needed untracked')
        out=self.root/'carried';g.export(r,out)
        self.assertEqual((out/'node_modules/owned.js').read_text(),'unstaged-essential')
        self.assertEqual(g.git(out,'show',':node_modules/owned.js').stdout,b'staged')
        self.assertEqual((out/'node_modules/necessary.js').read_text(),'needed untracked')
        self.assertFalse((out/'node_modules/cache/disposable').exists())
        self.assertEqual(g.git(r,'status','--porcelain').stdout,g.git(out,'status','--porcelain').stdout)

    def test_set_transaction_incremental_tamper_restore(self):
        r=self.repo();spec=[{'id':'one','source':str(r)}];a=self.root/'set';p.create(spec,a)
        b=self.root/'next';value=p.create(spec,b,previous=a);self.assertGreater(value['reused_files'],0);shutil.rmtree(r);out=self.root/'restored';p.restore(b,out);g.git(out/'one','fsck','--full');(b/'payload/one/a').write_text('tamper')
        with self.assertRaises(Blocked):p.verify(b)
    def test_project_set_executes_database_dependency(self):
        r=self.repo();source=self.root/'external.sqlite';db=sqlite3.connect(source);db.execute('CREATE TABLE t(v)');db.execute('INSERT INTO t VALUES (9)');db.commit();db.close()
        pkg=self.root/'pkg';value=p.create([{'id':'one','source':str(r),'dependencies':[{'engine':'sqlite','connection':{'path':str(source)}}]}],pkg)
        self.assertEqual(value['projects'][0]['dependencies'][0]['engine'],'sqlite');dump=pkg/'payload/one/.portable-databases/0';d.Adapter('sqlite').restore(dump,{'path':str(self.root/'restored-db')},authorize_write=True)
        with contextlib.closing(sqlite3.connect(self.root/'restored-db')) as db:self.assertEqual(db.execute('SELECT v FROM t').fetchone()[0],9)
    def test_set_interrupt_space_change(self):
        r=self.repo();spec=[{'id':'one','source':str(r)}]
        with self.assertRaises(Blocked):p.create(spec,self.root/'out',fail_after=0)
        self.assertFalse((self.root/'out').exists())
        with patch('portable.portability.shutil.disk_usage',return_value=shutil._ntuple_diskusage(1,1,0)):
            with self.assertRaises(Blocked):p.create(spec,self.root/'out')
    def test_history_merge_and_conflict_both_sides(self):
        base=self.repo();left=self.root/'left';right=self.root/'right';g.export(base,left);g.export(base,right)
        for r,value in ((left,'left'),(right,'right')):
            g.git(r,'config','user.name','Fixture');g.git(r,'config','user.email','fixture@invalid');(r/'a').write_text(value);g.git(r,'add','.');g.git(r,'commit','-m',value);(r/(value+'.txt')).write_text('uncommitted')
        before=[g.fingerprint(v) for v in (base,left,right)];out=self.root/'merge';report=g.reconcile(base,left,right,out);self.assertIn('a',report['conflicts']);self.assertEqual(before,[g.fingerprint(v) for v in (base,left,right)]);self.assertEqual(g.git(out/'merged','show',':2:a').stdout,b'left');self.assertEqual(g.git(out/'merged','show',':3:a').stdout,b'right');self.assertTrue((out/'merged/left.txt').exists());self.assertTrue((out/'merged/right.txt').exists())
    def test_history_clean_merge(self):
        base=self.repo();left=self.root/'left';right=self.root/'right';g.export(base,left);g.export(base,right);(left/'l').write_text('left');(right/'r').write_text('right');(right/'.gitignore').write_text('ignored-asset\n');(right/'ignored-asset').write_text('keep unknown ignored');report=g.reconcile(base,left,right,self.root/'merge');self.assertTrue(report['clean']);self.assertEqual((self.root/'merge/merged/ignored-asset').read_text(),'keep unknown ignored')
    def test_separate_gitdir_missing_objects_and_source_change(self):
        r=self.repo();gd=self.root/'external-git';g.git(r,'init','--separate-git-dir',str(gd));out=self.root/'out';g.export(r,out);shutil.rmtree(gd);g.git(out,'fsck','--full')
        real=g.git
        def mutate(root,*args,**kwargs):
            result=real(root,*args,**kwargs)
            if args[0]=='index-pack':(out/'a').write_text('concurrent change')
            return result
        with patch('portable.git_portable.git',side_effect=mutate):
            with self.assertRaisesRegex(Blocked,'source changed'):g.export(out,self.root/'changed')
        self.assertFalse((self.root/'changed').exists())
    def test_explicit_shallow_preparation_preserves_dirty_state(self):
        r=self.repo();(r/'a').write_text('second');g.git(r,'commit','-am','second');clone=self.root/'shallow';g.git(r,'clone','--depth=1','file://'+str(r),str(clone));(clone/'a').write_text('staged');g.git(clone,'add','a');(clone/'a').write_text('unstaged');before=g.fingerprint(clone)
        with self.assertRaises(Blocked):g.prepare(clone,self.root/'bad',fetch=True)
        out=self.root/'complete';g.prepare(clone,out,fetch=True,remote='file://'+str(r));self.assertEqual(g.git(out,'rev-parse','--is-shallow-repository').stdout.strip(),b'false');self.assertEqual(g.git(out,'show',':a').stdout,b'staged');self.assertEqual((out/'a').read_text(),'unstaged');self.assertEqual(before,g.fingerprint(clone))
    def test_binary_database_merge_preserves_sides(self):
        base=self.repo();db=sqlite3.connect(base/'data.sqlite');db.execute('CREATE TABLE t(v)');db.commit();db.close();(base/'binary').write_bytes(b'\0base');g.git(base,'add','.');g.git(base,'commit','-m','db')
        left=self.root/'left';right=self.root/'right';g.export(base,left);g.export(base,right)
        for repo,name in ((left,'left'),(right,'right')):
            with contextlib.closing(sqlite3.connect(repo/'data.sqlite')) as db:
                db.execute('INSERT INTO t VALUES (?)',(name,));db.commit()
            (repo/'binary').write_bytes(b'\0'+name.encode())
        result=g.reconcile(base,left,right,self.root/'merge');self.assertEqual(set(result['conflicts']),{'binary','data.sqlite'})
        for name in ('left','right'):
            with contextlib.closing(sqlite3.connect(self.root/'merge'/name/'data.sqlite')) as db:self.assertEqual(db.execute('SELECT v FROM t').fetchone()[0],name)
    def test_set_detects_source_change_and_payload_symlink(self):
        r=self.repo();real=g.export
        def mutate(*args,**kw):
            result=real(*args,**kw);(r/'a').write_text('racing writer');return result
        with patch('portable.git_portable.export',side_effect=mutate):
            with self.assertRaisesRegex(Blocked,'set changed'):p.create([{'id':'one','source':str(r)}],self.root/'bad')
        self.assertFalse((self.root/'bad').exists());pkg=self.root/'pkg';p.create([{'id':'one','source':str(r)}],pkg);os.rename(pkg/'payload',self.root/'payload');(pkg/'payload').symlink_to(self.root/'payload')
        with self.assertRaises(Blocked):p.verify(pkg)
    def test_unknown_database_and_secret_fail_closed(self):
        r=self.root/'files';r.mkdir();(r/'x.db').write_text('not a database')
        with self.assertRaises(Blocked):p.create([{'id':'x','source':str(r)}],self.root/'out')
        (r/'x.db').unlink();(r/'.env').write_text('fixture')
        with self.assertRaises(Blocked):p.create([{'id':'x','source':str(r)}],self.root/'out')

class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
    def test_sqlite_real_backup_restore_and_corruption(self):
        src=self.root/'source.sqlite';c=sqlite3.connect(src);c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE t(v)');c.execute('INSERT INTO t VALUES (7)');c.commit();self.addCleanup(c.close)
        a=d.Adapter('sqlite');pkg=self.root/'pkg';a.backup({'path':str(src)},pkg)
        with self.assertRaises(Blocked):a.restore(pkg,{'path':str(self.root/'restored')})
        a.restore(pkg,{'path':str(self.root/'restored')},authorize_write=True)
        with contextlib.closing(sqlite3.connect(self.root/'restored')) as db:self.assertEqual(db.execute('SELECT v FROM t').fetchone()[0],7)
        (pkg/'dump').write_bytes(b'corrupt')
        with self.assertRaises(Blocked):a.preflight(pkg)
    def tools(self,engine):
        result={}
        for tool in d.Adapter.TOOLS[engine]:
            path=self.root/tool
            path.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\np=Path(__file__)\nwith (p.parent/"calls").open("a") as f:f.write(json.dumps(sys.argv)+"\\n")\nif "--version" in sys.argv:print("fixture 16.2")\nelif any("json_build_object" in a for a in sys.argv):print(json.dumps({"subscriptions":[],"extensions":[]}))\nelif any("JSON_OBJECT" in a for a in sys.argv):pass\nelif any("SELECT DISTINCT" in a for a in sys.argv):print("InnoDB")\nelif any("SELECT" in a for a in sys.argv):print("0")\nelif p.name in ("pg_dump","mysqldump"):sys.stdout.buffer.write(b"synthetic database dump")\n');path.chmod(0o755);result[tool]=str(path)
        return result
    def test_postgres_mysql_executable_protocol(self):
        for engine in ('postgres','mysql'):
            with self.subTest(engine=engine):
                tools=self.tools(engine);a=d.Adapter(engine,tools);pkg=self.root/(engine+'-package');a.backup({'database':'synthetic','host':'localhost'},pkg,mysql_ddl_barrier=contextlib.nullcontext());a.restore(pkg,{'database':'empty_fixture'},authorize_write=True)
        calls=(self.root/'calls').read_text();self.assertIn('--format=custom',calls);self.assertIn('--single-transaction',calls);self.assertNotIn('--lock-all-tables',calls);self.assertIn('--skip-lock-tables',calls);self.assertIn('--no-login-paths',calls);self.assertNotIn('--abort-source-on-error',calls);self.assertIn('--batch',calls);self.assertNotIn('--force',calls)
    def test_database_clients_do_not_inherit_personal_credentials(self):
        for engine in ('postgres','mysql'):
            adapter=d.Adapter(engine,self.tools(engine));name=next(iter(adapter.tools))
            with patch.dict(os.environ,{'PGPASSFILE':'/unselected/password','PGSERVICEFILE':'/unselected/service','MYSQL_TEST_LOGIN_FILE':'/unselected/login','MYSQL_PWD':'fixture-only'}):
                with patch('portable.db_adapters.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'',b'')) as run:
                    adapter.call(name,['--version'])
                env=run.call_args.kwargs['env'];argv=run.call_args.args[0]
                self.assertNotEqual(env['PGPASSFILE'],'/unselected/password')
                self.assertNotEqual(env['PGSERVICEFILE'],'/unselected/service')
                self.assertNotIn('MYSQL_TEST_LOGIN_FILE',env);self.assertNotIn('MYSQL_PWD',env)
                self.assertNotIn('fixture-only',str(argv))
                if engine=='mysql':self.assertEqual(argv[1:3],['--no-defaults','--no-login-paths'])

    def test_mysql_default_never_requests_global_lock(self):
        adapter=d.Adapter('mysql',self.tools('mysql'));pkg=self.root/'unsafe'
        try:adapter.backup({'database':'synthetic'},pkg)
        except Blocked:pass
        calls=(self.root/'calls').read_text() if (self.root/'calls').exists() else ''
        self.assertNotIn('--lock-all-tables',calls)
        self.assertFalse(pkg.exists(),'DDL consistency requires a real project barrier')

    def test_mysql_engine_precheck_and_barrier(self):
        tools=self.tools('mysql');adapter=d.Adapter('mysql',tools);events=[]
        @contextlib.contextmanager
        def barrier():
            events.append('stop-ddl')
            try:yield
            finally:events.append('resume-ddl')
        pkg=self.root/'safe'
        meta=adapter.backup({'database':'synthetic'},pkg,mysql_ddl_barrier=barrier())
        self.assertEqual(events,['stop-ddl','resume-ddl'])
        self.assertEqual(meta['consistency'],'mysql-innodb-snapshot-with-project-ddl-barrier')
        calls=[json.loads(line) for line in (self.root/'calls').read_text().splitlines()]
        query=next(i for i,c in enumerate(calls) if any('SELECT DISTINCT' in a for a in c))
        dump=next(i for i,c in enumerate(calls) if '--single-transaction' in c)
        self.assertLess(query,dump)
        for engine in ('MyISAM','UNKNOWN','InnoDB\nMyISAM'):
            Path(tools['mysql']).write_text(Path(tools['mysql']).read_text().replace('print("InnoDB")', 'print('+repr(engine)+')'))
            with self.assertRaisesRegex(Blocked,'project-specific'):
                adapter.backup({'database':'synthetic'},self.root/'rejected',mysql_ddl_barrier=barrier())
            self.assertFalse((self.root/'rejected').exists())
            Path(tools['mysql']).write_text(Path(tools['mysql']).read_text().replace('print('+repr(engine)+')','print("InnoDB")'))
        path=Path(tools['mysql']);path.write_text(path.read_text().replace('print("InnoDB")','sys.exit(7)'))
        with self.assertRaisesRegex(Blocked,'exit 7'):
            adapter.backup({'database':'synthetic'},self.root/'failed',mysql_ddl_barrier=barrier())
        self.assertFalse((self.root/'failed').exists())
        calls=[json.loads(line) for line in (self.root/'calls').read_text().splitlines()]
        self.assertEqual(sum('--single-transaction' in c for c in calls),2)

    def test_database_tool_failure_and_nonempty_restore_are_rejected(self):
        tools=self.tools('postgres');adapter=d.Adapter('postgres',tools);pkg=self.root/'pkg';adapter.backup({'database':'source_fixture'},pkg)
        path=Path(tools['psql']);path.write_text(path.read_text().replace('print("0")','print("1")'))
        with self.assertRaisesRegex(Blocked,'not empty'):adapter.restore(pkg,{'database':'existing_fixture'},authorize_write=True)
        path=Path(tools['pg_restore']);path.write_text(path.read_text()+'\nif "--list" in sys.argv:sys.exit(7)\n')
        with self.assertRaisesRegex(Blocked,'exit 7'):adapter.preflight(pkg)
    def test_missing_tools_credentials_unknown_engine(self):
        with self.assertRaises(Blocked):d.Adapter('unknown')
        with patch('portable.db_adapters.shutil.which',return_value=None):
            with self.assertRaises(Blocked):d.Adapter('postgres')
        with self.assertRaises(Blocked):d.Adapter('sqlite').connection({'password':'synthetic'})
    def test_postgres_real_temporary_engine_when_available(self):
        server=shutil.which('postgres')
        serverbin=Path(server).resolve().parent if server else self.root/'absent'
        tools={n:str(serverbin/n) if (serverbin/n).is_file() else None for n in ('initdb','pg_ctl','pg_dump','pg_restore','psql')}
        if not all(tools.values()):self.skipTest('local PostgreSQL engine/toolchain absent; protocol fixture remains separate')
        data=self.root/'pgdata';sock=self.root/'socket';sock.mkdir();env={'HOME':str(self.root),'PATH':os.defpath,'LC_ALL':'C'}
        def command(name,*args):
            return subprocess.run([tools[name],*args],capture_output=True,text=True,env=env,timeout=30)
        result=command('initdb','-D',str(data),'-U','fixture','--auth=trust','--no-locale','--encoding=UTF8')
        if result.returncode and ('Operation not permitted' in result.stderr or 'Permission denied' in result.stderr):self.skipTest('sandbox denied PostgreSQL shared memory; caller VERIFY required')
        self.assertEqual(result.returncode,0,result.stderr)
        result=command('pg_ctl','-D',str(data),'-l',str(self.root/'postgres.log'),'-o',"-F -h '' -k "+str(sock)+' -p 55439','-w','start')
        if result.returncode:
            log=(self.root/'postgres.log').read_text()
            if 'Operation not permitted' in log or 'Permission denied' in log:self.skipTest('sandbox denied isolated PostgreSQL socket: caller VERIFY required')
            self.fail(result.stderr+log)
        self.addCleanup(lambda:command('pg_ctl','-D',str(data),'-m','immediate','-w','stop'))
        args=['--no-psqlrc','--no-password','--host='+str(sock),'--port=55439','--username=fixture','--dbname=postgres','--set=ON_ERROR_STOP=1']
        for sql in ('CREATE DATABASE source_fixture','CREATE DATABASE restored_fixture'):
            result=command('psql',*args,'--command='+sql);self.assertEqual(result.returncode,0,result.stderr)
        result=command('psql',*args[:-1], '--dbname=source_fixture','--set=ON_ERROR_STOP=1','--command=CREATE TABLE evidence(v integer); INSERT INTO evidence VALUES (42)');self.assertEqual(result.returncode,0,result.stderr)
        adapter=d.Adapter('postgres',{k:tools[k] for k in ('pg_dump','pg_restore','psql')});connection={'host':str(sock),'port':55439,'user':'fixture','database':'source_fixture'};pkg=self.root/'real-postgres';adapter.backup(connection,pkg);adapter.restore(pkg,{**connection,'database':'restored_fixture'},authorize_write=True)
        result=command('psql',*args,'--dbname=restored_fixture','--tuples-only','--no-align','--command=SELECT v FROM evidence');self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'42')
        with self.assertRaises(Blocked):adapter.restore(pkg,{**connection,'database':'restored_fixture'},authorize_write=True)
    def test_group_executes_barrier(self):
        src=self.root/'x';db=sqlite3.connect(src);db.execute('CREATE TABLE t(v)');db.commit();db.close();events=[]
        @contextlib.contextmanager
        def barrier():
            events.append('stop')
            try:yield
            finally:events.append('resume')
        value=d.backup_group([{'engine':'sqlite','connection':{'path':str(src)}}],self.root/'group',barrier());self.assertEqual(events,['stop','resume']);self.assertEqual(value['cross_database_consistency'],'quiesced-by-executing-barrier')

if __name__=='__main__':unittest.main()
