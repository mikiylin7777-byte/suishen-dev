"""Real archived release, isolated HOME; launchd alone is an audited substitute."""
import hashlib
import json
import os
import contextlib
import plistlib
import sqlite3
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from portable import distribution,settings,upgrade_channel,supervisor,automation
from portable.core import Store,Blocked,VERSION

SOURCE=Path(__file__).resolve().parents[1]
FIXTURE=SOURCE/'tests/fixtures/friend-0.2.31.zip'
SHA='a89fe0aa847c17666eff0346f132bc1be64e059bdfa406dcf4460c353343a404'

class ExistingFriendUpgradeTest(unittest.TestCase):
    def scenario(self,paused=False,custom=False,fail=False):
        archive=Path(os.environ.get('PORTABLE_OLD_FRIEND_ARCHIVE',str(FIXTURE)))
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),SHA)
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'HOME':d}):
            home=Path(d).resolve();os.environ['HOME']=str(home)
            with zipfile.ZipFile(archive) as z:z.extractall(home)
            root=home/'随身开发';new=home/'candidate'
            distribution.build(SOURCE,new,'friend')
            def git(repo,*args):
                return subprocess.check_output(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false','-C',str(repo),*args],stderr=subprocess.DEVNULL)
            s=Store(root)
            settings.save(s,{'owner_name':'朋友','team_enabled':False,'team_name':'Codex 执行墙','projects':[]})
            repos=[];heads=[]
            for i in range(5):
                repo=home/('project'+str(i));repo.mkdir();git(repo,'init')
                git(repo,'remote','add','origin','https://github.com/example/project%d.git'%i)
                (repo/'STATUS.md').write_text('---\n项目: project%d\n状态: %s\n---\n'%(i,'暂缓' if i==4 else '进行中'))
                (repo/'tracked.txt').write_text('original\n');git(repo,'add','.');git(repo,'commit','-m','fixture')
                (repo/'tracked.txt').write_text('changed\n');(repo/'untracked.txt').write_text('untracked\n')
                s.register('project'+str(i),str(repo),'p'+str(i));repos.append(repo);heads.append(git(repo,'rev-parse','HEAD').strip())
            settings.keep_local_registrations(s)
            if custom:s.put('runner:config',{'backup_dir':str(home/'custom'),'node':'kept'})
            if paused:automation.pause(s)
            before=settings.load(s);s.db.close()
            audit=[]
            def ctl(action,path):
                self.assertTrue(path.is_relative_to(home/'Library/LaunchAgents'))
                audit.append(action)
                if action=='start' and fail:raise Blocked('simulated launchd denial')
            with patch('portable.upgrade_channel.Path.home',return_value=home):
                if fail:
                    with self.assertRaisesRegex(Blocked,'simulated launchd denial'):upgrade_channel.upgrade(root,new,launchctl=ctl)
                else:upgrade_channel.upgrade(root,new,launchctl=ctl)
                s=Store(root)
                self.assertEqual(settings.load(s),before)
                self.assertEqual({r[0] for r in s.db.execute('SELECT id FROM projects')},{'p'+str(i) for i in range(5)})
                self.assertEqual(automation.state(s)['paused'],paused)
                cfg=s.get('runner:config');self.assertTrue(cfg['backup_dir'])
                if custom:self.assertEqual(cfg,{'backup_dir':str(home/'custom'),'node':'kept'})
                self.assertTrue((root/'看板/全部项目.md').is_file())
                self.assertIn('project4',(root/'看板/全部项目.md').read_text())
                self.assertEqual(len(list((home/'Library/LaunchAgents').glob('*.plist'))),1)
                self.assertFalse(upgrade_channel._initialize_local(root,launchctl=ctl))
                definition=next((home/'Library/LaunchAgents').glob('*.plist'))
                command=plistlib.loads(definition.read_bytes())['ProgramArguments']
                self.assertEqual(command[0],str(Path(sys.executable).absolute()))
                if paused:
                    tick=subprocess.run([*command[:-1],'tick'],capture_output=True,text=True)
                    self.assertEqual(tick.returncode,0,tick.stderr)
                    self.assertTrue(json.loads(tick.stdout)['paused'])
                self.assertEqual(len(list((home/'Library/LaunchAgents').glob('*.plist'))),1)
                # Actual daily backup, not the upgrade safety snapshot. No fetch or dispatch.
                daily=distribution.backup(s,cfg['backup_dir'],'daily')
                external=Path(daily['external']['backup'])
                self.assertFalse(daily['external']['failed'])
                for i,repo in enumerate(repos):
                    dest=external/('p'+str(i))
                    self.assertEqual((dest/'未跟踪文件/untracked.txt').read_text(),'untracked\n')
                    self.assertIn('+changed',(dest/'未提交改动.patch').read_text())
                    git(repo,'bundle','verify',str(dest/'repo.bundle'))
                    self.assertIn(heads[i],git(repo,'bundle','list-heads',str(dest/'repo.bundle')))
                    self.assertEqual(git(repo,'rev-parse','HEAD').strip(),heads[i])
                    self.assertEqual(git(repo,'remote','get-url','origin').decode().strip(),'https://github.com/example/project%d.git'%i)
                    self.assertEqual((repo/'tracked.txt').read_text(),'changed\n')
                s.db.close()
                with self.assertRaises(Blocked):upgrade_channel.upgrade(root,new,launchctl=ctl)
                self.assertEqual(audit.count('start'),0 if paused else 1)
                upgrade_channel.rollback(root,launchctl=ctl)
                self.assertEqual(json.loads((root/'发行.json').read_text())['version'],'0.2.31')
                self.assertFalse(list((home/'Library/LaunchAgents').glob('*.plist')))
                self.assertTrue(list((home/'Library/LaunchAgents').glob('*.upgrade-disabled')))
                s=Store(root);self.assertEqual(settings.load(s),before);s.db.close()
                for i,repo in enumerate(repos):
                    self.assertEqual(git(repo,'rev-parse','HEAD').strip(),heads[i])
                    self.assertEqual((repo/'tracked.txt').read_text(),'changed\n')
                    self.assertEqual((repo/'untracked.txt').read_text(),'untracked\n')
    def test_archived_upgrade_daily_backup_and_rollback(self):self.scenario()
    def test_custom_backup_and_pause_preserved(self):self.scenario(paused=True,custom=True)
    def test_missing_backup_keeps_explicit_pause(self):self.scenario(paused=True)
    def test_launch_failure_reported_and_rollback_available(self):self.scenario(fail=True)

    def test_system_start_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            home=Path(d).resolve();root=home/'installed'
            plist=home/'Library/LaunchAgents'/(supervisor.identity(root)+'.plist')
            plist.parent.mkdir(parents=True);plist.write_text('isolated service fixture')
            with patch('portable.upgrade_channel.Path.home',return_value=home), patch('portable.upgrade_channel.sys.platform','darwin'), patch('portable.upgrade_channel.subprocess.run',return_value=subprocess.CompletedProcess([],1)) as run:
                with self.assertRaisesRegex(Blocked,'后台启动未确认'):upgrade_channel._launch(root,'start')
                self.assertTrue(all(c.args[0][0]=='/bin/launchctl' for c in run.call_args_list))


class UpgradeReviewRegressionTest(unittest.TestCase):
    @contextlib.contextmanager
    def installed(self,configured=True):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'HOME':d}):
            home=Path(d).resolve();os.environ['HOME']=str(home)
            with zipfile.ZipFile(FIXTURE) as z:z.extractall(home)
            root=home/'随身开发';new=home/'candidate'
            distribution.build(SOURCE,new,'friend')
            if configured:
                s=Store(root);settings.save(s,{'owner_name':'朋友','projects':[],'team_enabled':False});s.db.close()
            audit=[]
            def ctl(action,path):
                self.assertTrue(path.is_relative_to(home/'Library/LaunchAgents'))
                audit.append(action)
            with patch('portable.upgrade_channel.Path.home',return_value=home):
                yield home,root,new,audit,ctl

    def test_two_upgrades_rollback_keeps_preexisting_service_and_ignores_stale_marker(self):
        with self.installed() as (home,root,new,audit,ctl):
            # Synthetic intermediate uses this candidate's code and a supported version label.
            intermediate=home/'intermediate';distribution.build(SOURCE,intermediate,'friend')
            manifest=json.loads((intermediate/'发行.json').read_text());manifest['version']='0.2.36'
            (intermediate/'发行.json').write_text(json.dumps(manifest))
            upgrade_channel.upgrade(root,intermediate,launchctl=ctl)
            plist=next((home/'Library/LaunchAgents').glob('*.plist'));original=plist.read_bytes()
            s=Store(root)
            self.assertEqual(s.get(upgrade_channel.LAST)['created_service'],str(plist))
            # Simulate a previous f108ae9 install that left a persistent marker behind.
            s.put('upgrade:created-service',str(plist));s.db.close()
            upgrade_channel.upgrade(root,new,launchctl=ctl)
            s=Store(root)
            self.assertIsNone(s.get(upgrade_channel.LAST)['created_service'])
            self.assertIsNone(s.get('upgrade:created-service'));s.db.close()
            back=upgrade_channel.rollback(root,launchctl=ctl)
            self.assertEqual(back['退回'],VERSION+' → 0.2.36')
            self.assertEqual(plist.read_bytes(),original)
            self.assertFalse(list(plist.parent.glob('*.upgrade-disabled')))
            self.assertEqual(audit,['start','stop','start','stop','start'])

    def test_failed_rollback_preflight_backup_and_exchange_restore_current_service(self):
        with self.installed() as (home,root,new,audit,ctl):
            upgrade_channel.upgrade(root,new,launchctl=ctl)
            plist=next((home/'Library/LaunchAgents').glob('*.plist'));original=plist.read_bytes()
            s=Store(root);last=s.get(upgrade_channel.LAST);s.db.close()
            for operation in ('prepare_update','backup','atomic_upgrade'):
                with self.subTest(operation=operation):
                    audit.clear()
                    with patch('portable.distribution.'+operation,side_effect=Blocked('injected '+operation)):
                        with self.assertRaisesRegex(Blocked,'injected '+operation):upgrade_channel.rollback(root,launchctl=ctl)
                    self.assertEqual(json.loads((root/'发行.json').read_text())['version'],VERSION)
                    self.assertEqual(plist.read_bytes(),original)
                    self.assertFalse(list(plist.parent.glob('*.upgrade-disabled')))
                    self.assertEqual(audit,['stop','start'])
                    s=Store(root);self.assertEqual(s.get(upgrade_channel.LAST),last)
                    self.assertFalse(automation.state(s)['paused']);s.db.close()
            upgrade_channel.rollback(root,launchctl=ctl)
            self.assertFalse(plist.exists())

    def test_unconfigured_upgrade_does_not_report_initialized(self):
        with self.installed(configured=False) as (home,root,new,audit,ctl):
            out=upgrade_channel.upgrade(root,new,launchctl=ctl)
            self.assertNotIn('每日备份配置与看板已初始化',out['收尾'])
            self.assertTrue(any('尚未配置' in line for line in out['收尾']))
            # 朋友版升级默认打开自动接着做，并说清怎么关（见 DECISIONS 2026-11-18）
            self.assertEqual(out['自动接着做'],'已打开自动接着做，不要就说『关掉自动接着做』')
            from portable import settings
            s=Store(root);settings.set_value(s,'auto_continue',False);self.assertFalse(settings.auto_continue_on(s));s.db.close()
            self.assertFalse(list((home/'Library/LaunchAgents').glob('*.plist')))
            s=Store(root);self.assertIsNone(s.get('runner:config'));s.db.close()

    def test_non_mac_reports_failure_without_initialization_claim_or_plist(self):
        with self.installed() as (home,root,new,audit,ctl):
            with patch('portable.upgrade_channel.sys.platform','linux'):
                with self.assertRaisesRegex(Blocked,'不是 Mac') as caught:upgrade_channel.upgrade(root,new)
            self.assertNotIn('每日备份配置与看板已初始化',str(caught.exception))
            self.assertFalse(list((home/'Library/LaunchAgents').glob('*.plist')))
            s=Store(root);self.assertIsNone(s.get('runner:config'));s.db.close()

    def test_backup_snapshot_is_standalone_readonly_and_source_stays_wal(self):
        with tempfile.TemporaryDirectory() as d:
            home=Path(d).resolve();s=Store(home/'app');s.put('fixture','known content')
            try:
                saved=Path(distribution.backup(s,home/'backups','daily')['backup'])/'数据/state.sqlite'
                self.assertEqual(saved.read_bytes()[18:20],b'\x01\x01')
                self.assertFalse(saved.with_name(saved.name+'-wal').exists())
                self.assertFalse(saved.with_name(saved.name+'-shm').exists())
                with contextlib.closing(sqlite3.connect(saved.as_uri()+'?mode=ro',uri=True)) as db:
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                    self.assertEqual(db.execute("SELECT value FROM meta WHERE key='fixture'").fetchone()[0],'known content')
                self.assertEqual(s.db.execute('PRAGMA journal_mode').fetchone()[0],'wal')
            finally:s.db.close()

    @unittest.skipUnless(sys.platform=='darwin','actual macOS packaged CLI check; launchd stays paused')
    def test_packaged_cli_uses_same_interpreter_as_generated_service(self):
        with self.installed() as (home,root,new,audit,ctl):
            s=Store(root);automation.pause(s);s.db.close()
            # Initial archive has no plist; pause is explicit, so CLI never calls launchctl.
            out=subprocess.run([sys.executable,'-I','-B',str(new/'工具/portable.pyz'),'--root',str(root),'upgrade','apply',str(new)],capture_output=True,text=True)
            self.assertEqual(out.returncode,0,out.stderr+' '+out.stdout)
            self.assertIn('保持暂停',out.stdout)
            plist=next((home/'Library/LaunchAgents').glob('*.plist'))
            command=plistlib.loads(plist.read_bytes())['ProgramArguments']
            self.assertEqual(command[0],str(Path(sys.executable).absolute()))
            out=subprocess.run([*command[:-1],'tick'],capture_output=True,text=True)
            self.assertEqual(out.returncode,0,out.stderr)
            self.assertTrue(json.loads(out.stdout)['paused'])


    def test_existing_service_keeps_custom_settings_and_binds_upgrade_python(self):
        with self.installed() as (home,root,new,audit,ctl):
            service=supervisor.service(root,home/'Library/LaunchAgents','install')
            plist=Path(service['definition']);value=plistlib.loads(plist.read_bytes())
            value['ThrottleInterval']=123;value['RunAtLoad']=False
            value['StandardOutPath']=str(root/'数据/custom.log')
            value['ProgramArguments'][0]='previous-python'
            plist.write_bytes(plistlib.dumps(value))
            s=Store(root);automation.pause(s);s.db.close()
            upgrade_channel.upgrade(root,new,launchctl=ctl)
            after=plistlib.loads(plist.read_bytes())
            for key in ('ThrottleInterval','RunAtLoad','StandardOutPath'):
                self.assertEqual(after[key],value[key])
            self.assertEqual(after['ProgramArguments'][0],str(Path(sys.executable).absolute()))
            command=after['ProgramArguments']
            tick=subprocess.run([*command[:-1],'tick'],capture_output=True,text=True)
            self.assertEqual(tick.returncode,0,tick.stderr)
            self.assertTrue(json.loads(tick.stdout)['paused'])
            s=Store(root);self.assertIsNone(s.get(upgrade_channel.LAST)['created_service']);s.db.close()

    def test_invalid_rollback_candidate_never_stops_or_archives_current_service(self):
        with self.installed() as (home,root,new,audit,ctl):
            upgrade_channel.upgrade(root,new,launchctl=ctl)
            plist=next((home/'Library/LaunchAgents').glob('*.plist'));original=plist.read_bytes()
            s=Store(root);last=s.get(upgrade_channel.LAST);s.db.close()
            manifest=Path(last['previous_package'])/'发行.json';original_manifest=manifest.read_bytes()
            value=json.loads(original_manifest);value['files']['工具/portable.pyz']='0'*64
            manifest.write_text(json.dumps(value));audit.clear()
            with self.assertRaisesRegex(Blocked,'发行校验失败'):upgrade_channel.rollback(root,launchctl=ctl)
            self.assertEqual(audit,[]);self.assertEqual(plist.read_bytes(),original)
            self.assertEqual(json.loads((root/'发行.json').read_text())['version'],VERSION)
            manifest.write_bytes(original_manifest)
            upgrade_channel.rollback(root,launchctl=ctl)
            self.assertFalse(plist.exists())

    def test_retirement_failure_after_exchange_keeps_runner_paused(self):
        with self.installed() as (home,root,new,audit,ctl):
            upgrade_channel.upgrade(root,new,launchctl=ctl)
            plist=next((home/'Library/LaunchAgents').glob('*.plist'));original=plist.read_bytes()
            real_rename=Path.rename
            def rename(path,destination):
                if path==plist:raise PermissionError('injected recipe archive denial')
                return real_rename(path,destination)
            audit.clear()
            with patch.object(Path,'rename',autospec=True,side_effect=rename):
                with self.assertRaisesRegex(Blocked,'停用失败'):upgrade_channel.rollback(root,launchctl=ctl)
            self.assertEqual(audit,['stop']);self.assertEqual(plist.read_bytes(),original)
            self.assertEqual(json.loads((root/'发行.json').read_text())['version'],'0.2.31')
            s=Store(root)
            self.assertTrue(automation.state(s)['paused'])
            self.assertFalse(s.get('runner:service')['running'])
            self.assertTrue(s.get('runner:service')['paused']);s.db.close()
