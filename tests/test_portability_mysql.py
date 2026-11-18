"""Safety protocol tests and OPTIONAL real MySQL, never business databases."""
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from portable import db_adapters as d, portability
from portable.core import Blocked


# 短路径临时目录（数据库 socket 路径有长度上限）：macOS 用 /private/tmp，其他系统用 /tmp。
SHORT_TMP='/private/tmp' if os.path.isdir('/private/tmp') else '/tmp'


class DatabaseRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()

    def tools(self,engine):
        result={}
        for name in d.Adapter.TOOLS[engine]:
            path=self.root/name
            path.write_text('#!/bin/sh\nexit 0\n');path.chmod(0o755)
            result[name]=str(path)
        return result

    def test_private_provider_selection_permissions_links_and_no_leaks(self):
        for engine in ('mysql','postgres'):
            secret='synthetic-private-password'
            content='[client]\npassword='+secret+'\n' if engine=='mysql' else '*:*:*:fixture:'+secret+'\n'
            selected=self.root/(engine+'-private');selected.write_text(content);selected.chmod(0o600)
            reference={'kind':'portable.private-file/v1','engine':engine,'path':str(selected)}
            provider=d.PrivateFileCredentials.from_reference(reference,engine)
            adapter=d.Adapter(engine,self.tools(engine),credential_provider=provider)
            seen=[]
            def run(argv,**kwargs):
                env=kwargs['env'];candidate=Path(argv[1].split('=',1)[1]) if engine=='mysql' else Path(env['PGPASSFILE'])
                self.assertEqual(stat.S_IMODE(candidate.stat().st_mode),0o600)
                self.assertIn(secret,candidate.read_text())
                self.assertNotEqual(candidate,selected)
                self.assertNotIn(secret,str(argv));self.assertNotIn(secret,str(env))
                self.assertNotIn('MYSQL_PWD',env);self.assertNotIn('MYSQL_TEST_LOGIN_FILE',env)
                if engine=='mysql':self.assertEqual(argv[2],'--no-login-paths')
                seen.append(candidate)
                return subprocess.CompletedProcess(argv,0,b'',b'')
            with patch('portable.db_adapters.subprocess.run',side_effect=run):adapter.call(next(iter(adapter.tools)),['--version'])
            self.assertFalse(seen[0].exists())
            selected.chmod(0o644)
            with self.assertRaisesRegex(Blocked,'0600'):adapter.call(next(iter(adapter.tools)),['--version'])
            selected.chmod(0o600)
            linked=self.root/(engine+'-link');linked.symlink_to(selected)
            with self.assertRaisesRegex(Blocked,'links'):d.PrivateFileCredentials(engine,linked).snapshot(self.root)
            with patch('portable.db_adapters.os.getuid',return_value=os.getuid()+1):
                with self.assertRaisesRegex(Blocked,'owned'):provider.snapshot(self.root)
            hard=self.root/(engine+'-hard');os.link(selected,hard)
            with self.assertRaisesRegex(Blocked,'0600'):provider.snapshot(self.root)
            hard.unlink()
            parent=self.root/(engine+'-directory-link');parent.symlink_to(self.root,target_is_directory=True)
            with self.assertRaisesRegex(Blocked,'links'):d.PrivateFileCredentials(engine,parent/selected.name).snapshot(self.root)
        for content in ('[client]\npassword=x\nforce=1\n','[client]\npassword=x\n  force=1\n','!include /unselected\n[client]\npassword=x\n'):
            selected=self.root/'invalid';selected.write_text(content);selected.chmod(0o600)
            with self.assertRaises(Blocked):d.PrivateFileCredentials('mysql',selected).snapshot(self.root)
        with self.assertRaises(Blocked):d.PrivateFileCredentials.from_reference({'password':'forbidden'},'mysql')

    def test_events_are_separate_verified_assets_and_never_restored(self):
        adapter=d.Adapter('mysql',self.tools('mysql'));calls=[];active=[]
        @contextlib.contextmanager
        def barrier():
            active.append(True)
            try:yield
            finally:active.pop()
        def call(name,args,*,stdout=None,stdin=None):
            calls.append((name,args))
            if '--version' in args:return b'mysql 8.4.11'
            if any('SELECT DISTINCT' in a for a in args):self.assertTrue(active);return b'InnoDB\n'
            if any('JSON_OBJECT' in a for a in args):return b'{"name":"writer","status":"ENABLED"}\n'
            if name=='mysqldump':
                self.assertTrue(active)
                if '--skip-events' in args:stdout.write(b'CREATE TABLE evidence(v int); INSERT INTO evidence VALUES(42);')
                elif '--events' in args:stdout.write(b'CREATE EVENT writer ON SCHEDULE EVERY 1 SECOND DO INSERT INTO evidence VALUES(99);')
                else:self.fail('dump does not explicitly classify runtime definitions')
                return b''
            if stdin is not None:
                content=stdin.read();self.assertNotIn(b'CREATE EVENT',content)
                self.assertIn('--binary-mode',args);self.assertIn('--batch',args)
                self.assertNotIn('--abort-source-on-error',args);self.assertNotIn('--force',args)
                return b''
            return b'0'
        with patch.object(adapter,'call',side_effect=call):
            package=self.root/'package';metadata=adapter.backup({'database':'fixture'},package,mysql_ddl_barrier=barrier())
            result=adapter.restore(package,{'database':'new_fixture'},authorize_write=True)
            self.assertEqual(result['pending_runtime_definitions'],['events.sql'])
            self.assertFalse(result['runtime_assets_executed'])
            inventory=json.loads((package/'runtime.json').read_text())
            self.assertEqual(inventory['events'],[{'name':'writer','status':'ENABLED'}])
            self.assertIn('events.sql',metadata['assets'])
            (package/'events.sql').write_bytes(b'changed')
            with self.assertRaisesRegex(Blocked,'checksum'):adapter.preflight(package)
            metadata.pop('runtime_policy');(package/'manifest.json').write_text(json.dumps(metadata))
            with self.assertRaisesRegex(Blocked,'separation'):adapter.restore(package,{'database':'new_fixture'},authorize_write=True)
        self.assertFalse(any('--lock-all-tables' in args for _,args in calls))

    def test_postgres_runtime_inventory_blocks_unknown_extensions(self):
        adapter=d.Adapter('postgres',self.tools('postgres'))
        with patch.object(adapter,'call',return_value=b'{"subscriptions":[{"name":"source","enabled":true,"publications":["pub"]}],"extensions":[]}') as call:
            inventory=adapter.postgres_runtime(['--dbname=fixture'])
            self.assertTrue(inventory['pending'])
            self.assertNotIn('subconninfo',str(call.call_args))
        for extension in ('pg_cron','pgagent','unknown_extension'):
            with patch.object(adapter,'call',return_value=json.dumps({'subscriptions':[],'extensions':[extension]}).encode()):
                with self.assertRaisesRegex(Blocked,'project-specific'):adapter.postgres_runtime([])

    def test_registered_ddl_hook_cli_and_failure_cleanup(self):
        events=[]
        @contextlib.contextmanager
        def protect(connection):
            if connection['database']!='fixture':raise Blocked('project identity mismatch')
            events.append('enter')
            try:yield
            finally:events.append('exit')
        hooks=d.ProjectDDLHooks().register('project',protect)
        adapter=d.Adapter('mysql',self.tools('mysql'),ddl_hooks=hooks)
        with patch.object(adapter,'versions',side_effect=Blocked('test tool failure')):
            with self.assertRaisesRegex(Blocked,'test tool failure'):
                adapter.backup({'database':'fixture'},self.root/'failed',mysql_ddl_hook='project')
        self.assertEqual(events,['enter','exit']);self.assertFalse((self.root/'failed').exists())
        with self.assertRaisesRegex(Blocked,'not registered'):
            adapter.backup({'database':'fixture'},self.root/'unknown',mysql_ddl_hook='json-quiesced-true')
        connection=self.root/'connection.json';connection.write_text('{"database":"fixture"}')
        # Project wrapper passes its trusted registry to the real CLI entry.
        with patch('portable.db_adapters.shutil.which',side_effect=lambda n:adapter.tools.get(n)):
            with patch.object(d.Adapter,'versions',side_effect=Blocked('test tool failure')):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code=d.main(['database','backup','mysql',str(self.root/'cli'),'--connection',str(connection),'--ddl-hook','project'],ddl_hooks=hooks)
        self.assertEqual(code,2);self.assertIn('test tool failure',output.getvalue())
        self.assertEqual(events,['enter','exit','enter','exit'])

    def test_project_collection_passes_local_hooks_and_private_reference(self):
        source=self.root/'source';source.mkdir();(source/'readme').write_text('synthetic project')
        private=self.root/'private';private.write_text('[client]\npassword=synthetic\n');private.chmod(0o600)
        hooks=d.ProjectDDLHooks();events=[]
        @contextlib.contextmanager
        def protect(connection):
            events.append('enter')
            try:yield
            finally:events.append('exit')
        hooks.register('project',protect)
        def call(adapter,name,args,*,stdout=None,stdin=None):
            if '--version' in args:return b'mysql 8.4.11'
            self.assertEqual(events,['enter'])
            self.assertIsInstance(adapter.credential_provider,d.PrivateFileCredentials)
            if any('SELECT DISTINCT' in a for a in args):return b'InnoDB'
            if any('JSON_OBJECT' in a for a in args):return b''
            if stdout is not None:stdout.write(b'synthetic dump');return b''
            return b'0'
        spec={'id':'fixture','source':str(source),'dependencies':[{'engine':'mysql','connection':{'database':'fixture'},'tools':self.tools('mysql'),'ddl_hook':'project','credential_reference':{'kind':'portable.private-file/v1','engine':'mysql','path':str(private)}}]}
        with patch.object(d.Adapter,'call',new=call):
            target=self.root/'collection'
            portability.create([spec],target,database_ddl_hooks=hooks)
        self.assertEqual(events,['enter','exit'])
        manifest=(target/'manifest.json').read_text()
        self.assertNotIn('password',manifest);self.assertNotIn(str(private),manifest)
        portability.verify(target)
        events.clear()
        connection_file=self.root/'connection.json';connection_file.write_text('{"database":"fixture"}')
        reference_file=self.root/'reference.json';reference_file.write_text(json.dumps(spec['dependencies'][0]['credential_reference']))
        tools=spec['dependencies'][0]['tools']
        with patch.object(d.Adapter,'call',new=call), patch('portable.db_adapters.shutil.which',side_effect=lambda name:tools.get(name)):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result=d.main(['database','backup','mysql',str(self.root/'cli-package'),'--connection',str(connection_file),'--credential-reference',str(reference_file),'--ddl-hook','project'],ddl_hooks=hooks)
        self.assertEqual(result,0);self.assertEqual(events,['enter','exit'])
        self.assertNotIn('synthetic',output.getvalue());self.assertNotIn(str(private),output.getvalue())


class RealMySQLTests(unittest.TestCase):
    def test_real_mysql_password_events_and_sql_error(self):
        """Own short-path datadir/socket, no networking; terminate only own Popen."""
        candidate=os.environ.get('PORTABLE_MYSQL_BIN')
        if candidate:
            tools=Path(candidate)
        elif shutil.which('mysqld'):
            tools=Path(shutil.which('mysqld')).resolve().parent
        else:
            downloaded=Path('/nonexistent')
            candidates=list(downloaded.glob('mysql-*/bin')) if downloaded.exists() else []
            tools=candidates[0] if len(candidates)==1 else Path('/nonexistent')
        if not all((tools/n).is_file() for n in ('mysqld','mysqldump','mysql')):
            self.skipTest('real MySQL tools absent; protocol tests are not real-engine validation')
        with tempfile.TemporaryDirectory(prefix='pmy-',dir=SHORT_TMP) as temporary:
            root=Path(temporary);home=root/'home';home.mkdir();data=root/'data';log=root/'server.log';sock=root/'mysql.sock'
            env={'PATH':os.defpath,'HOME':str(home),'MYSQL_TEST_LOGIN_FILE':str(home/'absent')}
            init=subprocess.run([str(tools/'mysqld'),'--no-defaults','--initialize-insecure','--basedir='+str(tools.parent),'--datadir='+str(data),'--log-error='+str(log)],capture_output=True,env=env,timeout=60)
            if init.returncode:
                diagnostic=log.read_text() if log.exists() else init.stderr.decode(errors='replace')
                if any(reason in diagnostic for reason in ('Operation not permitted','Permission denied','Invalid permissions for mapped object')):
                    self.skipTest('sandbox/platform denied MySQL initialization memory/resources; caller VERIFY required')
                self.fail('isolated MySQL initialization failed')
            server=subprocess.Popen([str(tools/'mysqld'),'--no-defaults','--basedir='+str(tools.parent),'--datadir='+str(data),'--skip-networking','--mysqlx=OFF','--socket='+str(sock),'--pid-file='+str(root/'pid'),'--log-error='+str(log),'--event-scheduler=OFF'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            args=['--no-defaults','--no-login-paths','--host=localhost','--socket='+str(sock),'--user=root','--batch','--skip-column-names']
            def sql(statement):
                result=subprocess.run([str(tools/'mysql'),*args],input=statement.encode(),capture_output=True,env=env,timeout=30)
                self.assertEqual(result.returncode,0,'isolated synthetic SQL failed')
                return result.stdout.strip()
            try:
                ready=False
                for _ in range(150):
                    if server.poll() is not None:break
                    if sock.exists():
                        result=subprocess.run([str(tools/'mysql'),*args,'--execute=SELECT @@server_uuid'],capture_output=True,env=env,timeout=5)
                        if result.returncode==0:
                            expected=(data/'auto.cnf').read_text().split('server-uuid=',1)[1].strip()
                            self.assertEqual(result.stdout.decode().strip(),expected);ready=True;break
                    time.sleep(.1)
                if not ready:
                    diagnostic=log.read_text() if log.exists() else ''
                    if 'Operation not permitted' in diagnostic or 'Permission denied' in diagnostic:
                        self.skipTest('sandbox denied private MySQL socket/shared resources; caller VERIFY required')
                    self.fail('isolated MySQL server failed to start')
                sql("CREATE DATABASE source_fixture; CREATE DATABASE restored_fixture; CREATE DATABASE failed_fixture; CREATE TABLE source_fixture.evidence(v INT) ENGINE=InnoDB; INSERT INTO source_fixture.evidence VALUES(42); CREATE TABLE source_fixture.event_writes(v INT) ENGINE=InnoDB; CREATE EVENT source_fixture.writer ON SCHEDULE EVERY 1 SECOND STARTS CURRENT_TIMESTAMP + INTERVAL 1 SECOND ENABLE DO INSERT INTO event_writes VALUES(99);")
                # A synthetic account only; password never appears in command argv/output.
                password='syntheticFixture_42!'
                sql("CREATE USER 'fixture'@'localhost' IDENTIFIED BY '"+password+"'; GRANT ALL PRIVILEGES ON source_fixture.* TO 'fixture'@'localhost'; GRANT ALL PRIVILEGES ON restored_fixture.* TO 'fixture'@'localhost'; GRANT ALL PRIVILEGES ON failed_fixture.* TO 'fixture'@'localhost'; GRANT SHOW_ROUTINE ON *.* TO 'fixture'@'localhost';")
                private=root/'selected.cnf';private.write_text('[client]\npassword='+password+'\n');private.chmod(0o600)
                hooks=d.ProjectDDLHooks()
                @contextlib.contextmanager
                def exclusive_fixture(connection):
                    self.assertEqual(connection['socket'],str(sock));self.assertEqual(connection['database'],'source_fixture')
                    # All test DDL is owned by this process and obeys this lock.
                    with (root/'ddl.guard').open('w') as guard:
                        fcntl.flock(guard,fcntl.LOCK_EX)
                        try:yield
                        finally:fcntl.flock(guard,fcntl.LOCK_UN)
                hooks.register('fixture',exclusive_fixture)
                adapter=d.Adapter('mysql',{n:str(tools/n) for n in ('mysql','mysqldump')},credential_provider=d.PrivateFileCredentials('mysql',private),ddl_hooks=hooks)
                connection={'host':'localhost','socket':str(sock),'user':'fixture','database':'source_fixture'}
                package=root/'package';metadata=adapter.backup(connection,package,mysql_ddl_hook='fixture')
                self.assertIn(b'CREATE', (package/'events.sql').read_bytes());self.assertIn(b'writer',(package/'events.sql').read_bytes())
                self.assertNotIn(b'CREATE EVENT',(package/'dump').read_bytes())
                inventory=json.loads((package/'runtime.json').read_text());self.assertEqual(inventory['events'],[{'name':'writer','status':'ENABLED'}])
                sql('SET GLOBAL event_scheduler=ON')  # Own isolated temporary instance only.
                self.assertEqual(sql('SELECT @@event_scheduler'),b'ON')
                result=adapter.restore(package,{**connection,'database':'restored_fixture'},authorize_write=True)
                self.assertEqual(result['pending_runtime_definitions'],['events.sql'])
                self.assertEqual(sql('SELECT v FROM restored_fixture.evidence'),b'42')
                deadline=time.monotonic()+10
                while int(sql('SELECT COUNT(*) FROM source_fixture.event_writes'))==0 and time.monotonic()<deadline:
                    time.sleep(.2)
                self.assertEqual(sql('SELECT COUNT(*) FROM restored_fixture.event_writes'),b'0')
                self.assertEqual(sql("SELECT COUNT(*) FROM information_schema.events WHERE event_schema='restored_fixture'"),b'0')
                self.assertGreater(int(sql('SELECT COUNT(*) FROM source_fixture.event_writes')),0)
                with self.assertRaisesRegex(Blocked,'not empty'):adapter.restore(package,{**connection,'database':'restored_fixture'},authorize_write=True)
                # Actual SQL error after valid data: return failure, keep partial target,
                # stop before the sentinel SQL, and reject retry into nonempty target.
                with (package/'dump').open('ab') as out:out.write(b'\nTHIS IS INTENTIONALLY INVALID SQL;\nCREATE TABLE must_not_run(v INT);\n')
                metadata['sha256']=hashlib.sha256((package/'dump').read_bytes()).hexdigest();metadata['bytes']=(package/'dump').stat().st_size
                (package/'manifest.json').write_text(json.dumps(metadata))
                with self.assertRaisesRegex(Blocked,'mysql failed; exit'):
                    adapter.restore(package,{**connection,'database':'failed_fixture'},authorize_write=True)
                self.assertEqual(sql('SELECT v FROM failed_fixture.evidence'),b'42')
                self.assertEqual(sql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='failed_fixture' AND table_name='must_not_run'"),b'0')
                with self.assertRaisesRegex(Blocked,'not empty'):adapter.restore(package,{**connection,'database':'failed_fixture'},authorize_write=True)
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:server.wait(timeout=20)
                    except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
                self.assertIsNotNone(server.poll(),'owned temporary server was not stopped')
