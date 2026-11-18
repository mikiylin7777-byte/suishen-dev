"""二期：首次配置向导、搬出卸载、花费提醒、升级通道、按项目判断代码存放。"""
import json
import re
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from portable.core import Store,Blocked,canonical
from portable import settings,wizard,leave,cost,upgrade_channel,board

SOURCE=Path(__file__).resolve().parents[1]

def git_repo(path,remote=None):
    path.mkdir(parents=True)
    run=lambda *a:subprocess.run(['git','-C',str(path),*a],check=True,capture_output=True)
    run('init','-q');run('-c','user.name=t','-c','user.email=t@t','commit','-q','--allow-empty','-m','init')
    if remote:run('remote','add','origin',remote)
    return path

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.t=Path(self.tmp.name).resolve()
        self.home=self.t/'home';self.home.mkdir()
        self.env=patch.dict(os.environ,{'PORTABLE_HOME':str(self.home)});self.env.start()
        self.s=Store(self.home/'随身开发')
    def tearDown(self):
        try:self.s.db.close()
        except Exception:pass
        self.env.stop();self.tmp.cleanup()

class WizardTest(Base):
    def test_plain_answers_configure_rules_hook_tools_and_suggest_projects(self):
        (self.home/'.claude').mkdir();(self.home/'.claude/CLAUDE.md').write_text('我自己的规则\n')
        ws=self.home/'工作区';git_repo(ws/'记账本')
        r=wizard.run_wizard(self.s,{'owner_name':'小王','workspace':str(ws),'code_hosting':'只存本机','team_enabled':'没有','billing':'按量','cost_limit_yuan':5})
        self.assertEqual((r['称呼'],r['代码存放'],r['AI 战队']),('小王','只存本机','没有（AI 自己动手）'))
        self.assertEqual([p['名字'] for p in r['建议搬进来的项目']],['记账本'])
        text=(self.home/'.claude/CLAUDE.md').read_text()
        self.assertTrue(text.startswith('我自己的规则\n'));self.assertIn('@'+str(self.s.root)+'/数据/本机规则.md',text)
        self.assertFalse((self.home/'.codex/AGENTS.md').exists())  # 没装的工具不碰
        rules=(self.s.root/'数据/本机规则.md').read_text()
        self.assertIn('小王',rules);self.assertIn('单次超过 5 元',rules)
        self.assertEqual(len(list((self.s.root/'数据/接入备份').iterdir())),1)
        # 再跑一次不重复加行
        wizard.run_wizard(self.s,{'owner_name':'小王'},scan=False)
        self.assertEqual((self.home/'.claude/CLAUDE.md').read_text().count('本机规则.md'),1)
        self.assertFalse(wizard.needed(self.s))

    def test_partial_answers_keep_existing_settings(self):
        wizard.run_wizard(self.s,{'owner_name':'小王','team_enabled':'有','code_hosting':'remote','billing':'按量'},scan=False)
        wizard.run_wizard(self.s,{'tools':['codex']},scan=False)  # 只补接入，不该把战队/存放/计费改回默认
        v=settings.load(self.s)
        self.assertEqual((v['team_enabled'],v['code_hosting'],v['billing'],v['owner_name']),(True,'remote','metered','小王'))

    def test_unknown_answer_rejected_and_no_answers_in_non_tty_blocks(self):
        with self.assertRaises(Blocked):wizard.run_wizard(self.s,{'password':'x'})
        self.assertTrue(wizard.needed(self.s))

    def test_cli_show_questions_lists_only_plain_questions(self):
        out=subprocess.run(['python3','-B','-m','portable','--root',str(self.s.root),'wizard','--show-questions'],cwd=SOURCE,capture_output=True,text=True,env={**os.environ,'PYTHONPATH':str(SOURCE)})
        self.assertEqual(out.returncode,0,out.stderr)
        self.assertEqual(len(json.loads(out.stdout)['要问的事']),5)

class LeaveTest(Base):
    def test_plan_first_then_restore_rules_and_archive_everything(self):
        (self.home/'.claude').mkdir();(self.home/'.claude/CLAUDE.md').write_text('我自己的规则\n')
        (self.home/'.codex').mkdir()
        proj=git_repo(self.home/'项目甲')
        board.apply(self.s,[{'id':'a','name':'甲','path':str(proj)}])
        wizard.run_wizard(self.s,{'owner_name':'小王'},scan=False)
        root=self.s.root
        plan=leave.leave(self.s)
        self.assertFalse(plan['已执行']);self.assertTrue(root.exists());self.assertEqual(len(plan['规则文件']),2)
        r=leave.leave(self.s,confirm=True)
        self.assertEqual((self.home/'.claude/CLAUDE.md').read_text(),'我自己的规则\n')
        self.assertFalse((self.home/'.codex/AGENTS.md').exists())  # 向导新建的空文件移进归档
        self.assertFalse(root.exists());moved=Path(r['随身开发移到'])
        self.assertTrue((moved/'数据/state.sqlite').exists())
        self.assertTrue(proj.exists() and (proj/'.git').exists())
        self.assertIn(str(proj),Path(r['恢复方法']).read_text())
        self.assertFalse(r['永久删除'])

class CostTest(Base):
    def test_subscription_never_asks_metered_asks_over_limit_and_budget(self):
        self.assertFalse(cost.check(self.s,999)['先问用户'])
        settings.set_value(self.s,'billing','metered');settings.set_value(self.s,'cost_limit_yuan',10);settings.set_value(self.s,'monthly_budget_yuan',30)
        self.assertFalse(cost.check(self.s,8)['先问用户'])
        self.assertTrue(cost.check(self.s,12)['先问用户'])
        cost.record(self.s,25,'跑一批测试')
        r=cost.check(self.s,8);self.assertTrue(r['先问用户']);self.assertIn('月预算',r['原因'])
        self.assertEqual(cost.show(self.s)['本月已花'],25)

    def test_estimate_and_bad_values(self):
        self.assertEqual(cost.estimate(1_000_000,500_000,3,15),10.5)
        with self.assertRaises(Blocked):cost.check(self.s,-1)
        with self.assertRaises(Blocked):cost.record(self.s,1,' ')
        with self.assertRaises(Blocked):settings.set_value(self.s,'billing','free')

class PerProjectHostingTest(Base):
    def test_adopt_records_github_remote_without_credentials_and_local_otherwise(self):
        from portable.move_in import adopt
        gh=git_repo(self.home/'gh',remote='https://user:tok123@github.com/u/gh.git');lo=git_repo(self.home/'lo')
        from portable.move_in import scan;scan(self.s,[self.home])
        a=adopt(self.s,gh,confirmed='搬 gh');b=adopt(self.s,lo,confirmed='搬 lo')
        self.assertEqual(a['代码存放'],'连着在线仓库：https://github.com/u/gh.git');self.assertNotIn('tok123',canonical(board.load(self.s)))
        self.assertEqual(b['代码存放'],'只存本机')
        rules=Path(settings.render_rules(self.s)).read_text()
        self.assertIn('每个项目按它自己的情况走',rules);self.assertIn('GitHub',rules)
        self.assertIn('git remote',rules)

    def test_named_remote_fallback_origin_priority_and_no_remote(self):
        repo=git_repo(self.home/'named')
        self.assertEqual(settings.hosting_of(repo),'local')
        def remote(name,url):
            subprocess.run(['git','-C',str(repo),'remote','add',name,url],check=True,capture_output=True)
        remote('gitea','https://user:password@example.test/u/repo.git')
        self.assertEqual(settings.hosting_of(repo),'https://example.test/u/repo.git')
        remote('origin','https://other:secret@origin.test/u/repo.git')
        self.assertEqual(settings.hosting_of(repo),'https://origin.test/u/repo.git')
        rules=Path(settings.render_rules(self.s)).read_text()
        self.assertNotIn('有 origin',rules)
        self.assertIn('git remote 里有在线仓库地址',rules)

class ChineseNameMoveInTest(Base):
    def test_chinese_project_name_moves_in(self):
        from portable.move_in import adopt
        from portable.move_in import scan;repo=git_repo(self.home/'记账本');scan(self.s,[self.home])
        r=adopt(self.s,repo,confirmed='搬记账本')
        self.assertEqual(r['项目'],'记账本');self.assertTrue(r['编号'].startswith('project-'))
        self.assertEqual([e['name'] for e in board.load(self.s)],['记账本'])

class UpgradeChannelTest(unittest.TestCase):
    def _pkg(self,where,version):
        from portable.distribution import build
        build(SOURCE,where)
        m=json.loads((where/'发行.json').read_text());m['version']=version;(where/'发行.json').write_text(canonical(m))
        return where

    def test_one_command_upgrade_keeps_data_and_rollback_restores_old_program(self):
        from portable.core import VERSION
        with tempfile.TemporaryDirectory() as d:
            t=Path(d).resolve();root=self._pkg(t/'随身开发','0.2.20');new=self._pkg(t/'新版',VERSION)
            s=Store(root);settings.set_value(s,'owner_name','小王');s.record('checkpoint',{'goal':'g','decisions':[],'done':[],'remaining':[],'authorization':[],'evidence':[]});s.db.close()
            calls=[]
            with patch('portable.automation.pause',side_effect=lambda st,paused=True,**kw:calls.append(paused) or {}):
                r=upgrade_channel.upgrade(root,new,launchctl=lambda *a:calls.append(a))
            self.assertEqual(r['升级'],'0.2.20 → '+VERSION);self.assertEqual(r['自检版本'],VERSION)
            self.assertEqual(json.loads((root/'发行.json').read_text())['version'],VERSION)
            self.assertIn('小王',(root/'数据/本机规则.md').read_text())
            self.assertTrue(Path(r['备份']).is_dir());self.assertEqual(r['备份'].split('/')[-2],'随身开发备份')
            with self.assertRaises(Blocked):upgrade_channel.upgrade(root,new)  # 同版本不升
            with patch('portable.automation.pause',return_value={}):
                back=upgrade_channel.rollback(root)
            self.assertEqual(json.loads((root/'发行.json').read_text())['version'],'0.2.20')
            self.assertEqual(back['退回'],VERSION+' → 0.2.20')
            s=Store(root);self.assertEqual(settings.load(s)['owner_name'],'小王');s.db.close()

    def test_delivered_037_package_upgrades_to_current(self):
        from portable.core import VERSION
        with tempfile.TemporaryDirectory() as d:
            t=Path(d).resolve();root=self._pkg(t/'随身开发','0.2.37');new=self._pkg(t/'新版',VERSION)
            self.assertNotEqual(VERSION,'0.2.37')
            with patch('portable.automation.pause',return_value={}):
                result=upgrade_channel.upgrade(root,new,launchctl=lambda *a: None)
            self.assertEqual(result['升级'],'0.2.37 → '+VERSION)
            self.assertEqual(json.loads((root/'发行.json').read_text())['version'],VERSION)

    def test_upgrade_from_028_keeps_new_local_registration(self):
        from portable.core import VERSION
        with tempfile.TemporaryDirectory() as d:
            t=Path(d).resolve();root=self._pkg(t/'installed','0.2.28');new=self._pkg(t/'new',VERSION)
            s=Store(root)
            settings.save(s,{'projects':[]})
            s.register('Local',str(git_repo(t/'project')),'only-local')
            s.db.close()
            # Only launchctl is simulated; release verification, upgrade and registry alignment run on isolated fixtures.
            with patch('portable.upgrade_channel._launch',return_value=False):
                result=upgrade_channel.upgrade(root,new)
            self.assertEqual(result['升级'],'0.2.28 → '+VERSION)
            self.assertEqual(result['项目数'],1)
            self.assertIn('新登记的项目已写进本机设置：Local',result['收尾'])
            s=Store(root)
            try:
                self.assertEqual([e['id'] for e in settings.load(s)['projects']],['only-local'])
                self.assertEqual([e['id'] for e in board.load(s)],['only-local'])
            finally:s.db.close()

    def test_registry_failure_keeps_new_release_and_restores_background(self):
        from portable.core import VERSION
        from portable.automation import state
        with tempfile.TemporaryDirectory() as d:
            t=Path(d).resolve();root=self._pkg(t/'installed','0.2.24');new=self._pkg(t/'new',VERSION)
            s=Store(root)
            settings.save(s,{'projects':[]})
            project=git_repo(t/'project')
            s.register('Local',str(project),'only-local')
            project.rename(t/'moved-project')  # 名单会自动补齐，但项目目录失效仍必须拦下。
            s.db.close()
            calls=[]
            # Only the service existence check is simulated; pause/resume and missing-path validation use real fixtures.
            with patch('portable.upgrade_channel.Path.home',return_value=t):
                from portable.supervisor import identity
                plist=t/'Library/LaunchAgents'/(identity(root)+'.plist')
                plist.parent.mkdir(parents=True);plist.write_text('fixture')
                with self.assertRaises(Blocked) as caught:
                    upgrade_channel.upgrade(root,new,launchctl=lambda action,p:calls.append(action))
            message=str(caught.exception)
            for text in ('程序已换成新版 '+VERSION,'收尾没做完','后台已恢复','项目登记'):
                self.assertIn(text,message)
            self.assertEqual(calls,['stop','start'])
            self.assertEqual(json.loads((root/'发行.json').read_text())['version'],VERSION)
            s=Store(root)
            try:
                self.assertFalse(state(s)['paused'])
                self.assertEqual(s.get(upgrade_channel.LAST)['to'],VERSION)
                self.assertTrue((root/settings.RULES).is_file())
            finally:s.db.close()

    def test_resume_still_starts_service_when_unpause_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve()
            with patch('portable.automation.pause',side_effect=Blocked('pause failed')), patch('portable.upgrade_channel._launch') as launch:
                with self.assertRaises(Blocked):upgrade_channel._resume(root)
            launch.assert_called_once_with(root,'start',None)

    def test_restore_attempts_every_step_after_failures(self):
        calls=[]
        def action(name,fail=False):
            def run(*args):
                calls.append(name)
                if fail:raise Blocked(name)
            return run
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();s=Store(root);settings.save(s,{});s.db.close()
            with patch('portable.team_retire.retire',side_effect=action('retire',True)), patch('portable.board.apply',side_effect=action('registry',True)), patch('portable.settings.render_rules',side_effect=action('rules')):
                done,failed=upgrade_channel._restore(root,{'environment':{'AI_TEAM_CODEX_TASK':'task','DISPATCH_STATE_ROOT':'state'}},['a','b'],'python')
            # 自带战队后台停用失败也不拦后面几步
            self.assertEqual(calls,['retire','registry','rules'])
            self.assertEqual(len(failed),2)
            self.assertIn('本机规则已重新生成',done)

    def test_older_candidate_refused_without_rollback(self):
        from portable.core import VERSION
        with tempfile.TemporaryDirectory() as d:
            t=Path(d).resolve();root=self._pkg(t/'随身开发',VERSION);old=self._pkg(t/'旧版','0.2.19')
            with self.assertRaises(Blocked):upgrade_channel.upgrade(root,old)

class NoOwnerPathsTest(unittest.TestCase):
    def test_runtime_has_no_owner_home_paths(self):
        hits=[p.name for p in (SOURCE/'portable').glob('*.py') if re.search(r'/Users/[A-Za-z]',p.read_text())]
        self.assertEqual(hits,[])

class FriendEditionTest(unittest.TestCase):
    def test_friend_edition_has_no_team_backend_and_works(self):
        from portable.distribution import build,release_verify
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve()/'随身开发'
            build(SOURCE,root,'friend')
            for name in ('AGENTS.md','CLAUDE.md'):
                # 0.2.37：总管工作法是本机的多窗口做法，朋友版不带；角色词也不应出现在朋友的入口里。
                text=(root/name).read_text()
                self.assertNotIn('### 总管工作法',text);self.assertNotIn('t3_thread_send',text)
                self.assertIn('### 装好后第一件事',text)
            full=Path(d).resolve()/'自用版'
            build(SOURCE,full,'full')
            self.assertIn('### 总管工作法',(full/'AGENTS.md').read_text());self.assertIn('不信自报',(full/'AGENTS.md').read_text())
            self.assertFalse((root/'工具/team-host-release').exists());self.assertFalse((root/'工具/adapters/team-server').exists())
            m=release_verify(root);self.assertEqual(m['edition'],'friend')
            out=subprocess.run(['python3',str(root/'工具/portable.pyz'),'--root',str(root),'doctor'],capture_output=True,text=True)
            self.assertEqual(out.returncode,0,out.stderr);self.assertEqual(json.loads(out.stdout)['projects'],0)

    def test_personal_marker_stops_the_build(self):
        from portable.distribution import build
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve()/'随身开发'
            with self.assertRaises(Blocked) as e:build(SOURCE,root,'friend',['随身开发公共协议'])
            self.assertIn('个人信息',str(e.exception));self.assertFalse(root.exists())

    def test_package_script_makes_clean_zip(self):
        import zipfile,importlib.util
        spec=importlib.util.spec_from_file_location('pf',SOURCE/'scripts/package_friend.py');pf=importlib.util.module_from_spec(spec);spec.loader.exec_module(pf)
        with tempfile.TemporaryDirectory() as d:
            out=Path(d).resolve()/'朋友版.zip';r=pf.package(out)
            self.assertEqual(r['个人信息检查'],'通过')
            with zipfile.ZipFile(out) as z:
                names=z.namelist()
                self.assertFalse([n for n in names if '/._' in n or 'team-host-release' in n])
                info=z.getinfo('随身开发/开始使用.command');self.assertTrue((info.external_attr>>16)&0o100)
            with self.assertRaises(Blocked):pf.package(out)

    def test_short_latin_marker_matches_whole_word_only(self):
        from portable.distribution import personal_hits
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'a.py').write_text('# StartQzx and Qzxy are other words\n')
            (root/'b.py').write_text('# 见 Qzx 11-17\n')
            (root/'c.md').write_text('名单里的词：示例乙\n')
            hits=personal_hits(root,['Qzx','示例乙'],local=False)
            self.assertEqual(sorted(hits),['b.py：Qzx','c.md：示例乙'])

    def test_merge_check_scans_shipped_files_including_runtime_modules(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('pc',SOURCE/'tools/ci/privacy_check.py');pc=importlib.util.module_from_spec(spec);spec.loader.exec_module(pc)
        self.assertTrue(pc.markers(SOURCE/'tools/ci/privacy_markers.txt'))
        hits=pc.check(SOURCE,['Qzxvw','随身开发公共协议'])
        self.assertTrue(any(h.startswith('工具/portable.pyz!portable/') for h in hits),hits)
        self.assertFalse([h for h in hits if h.endswith('Qzxvw')])

class OtherAIToolsTest(Base):
    def test_opencode_gets_a_read_instruction_and_leave_removes_it(self):
        (self.home/'.config/opencode').mkdir(parents=True);(self.home/'.codex').mkdir()
        (self.home/'.codex/AGENTS.md').write_text('@'+str(self.s.root)+'/工具/开发协议.md\n我的规则\n')  # 早期写法
        r=wizard.run_wizard(self.s,{'owner_name':'小王'},scan=False)
        self.assertEqual(sorted(t['工具'] for t in r['接入的 AI 工具']),['codex','opencode'])
        oc=(self.home/'.config/opencode/AGENTS.md').read_text()
        self.assertIn('读文件工具',oc);self.assertIn(str(self.s.root)+'/数据/本机规则.md',oc);self.assertNotIn('@/',oc)
        leave.leave(self.s,confirm=True)
        self.assertEqual((self.home/'.codex/AGENTS.md').read_text(),'我的规则\n')
        self.assertFalse((self.home/'.config/opencode/AGENTS.md').exists())

if __name__=='__main__':unittest.main()

class WizardProtectionTest(Base):
    def test_installs_with_fake_launchctl_and_builds_board(self):
        from portable import supervisor
        repo=git_repo(self.t/'project')
        (repo/'STATUS.md').write_text('---\n项目: 示例\n阶段: 开发\n---\n')
        board.apply(self.s,[{'id':'example','name':'示例','path':str(repo)}])
        calls=[]
        def ctl(args,**kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args,0 if len(calls)>1 else 1)
        with patch('pathlib.Path.home',return_value=self.home),patch('sys.platform','darwin'),patch.object(supervisor.subprocess,'run',side_effect=ctl):
            # Board reads Git too; use a plain project to keep this fake specific to launchctl.
            with patch.object(board,'_read_with_source',return_value=('---\n项目: 示例\n---\n','工作区')),patch.object(board,'_read',return_value=''):
                r=wizard.run_wizard(self.s,{'tools':[]},home=self.home,scan=False)
        self.assertTrue(r['已配置']);self.assertEqual(r['提醒'],[])
        self.assertEqual(self.s.get('runner:config')['backup_dir'],str(self.home/'随身开发备份'))
        self.assertTrue(any(a[1]=='bootstrap' for a in calls))
        self.assertTrue((self.s.root/'看板/全部项目.md').is_file())

    def test_failed_install_keeps_configuration_and_existing_backup(self):
        from portable import supervisor
        existing=str(self.t/'custom-backup')
        self.s.put('runner:config',{'backup_dir':existing,'node':'custom'})
        with patch('sys.platform','darwin'),patch.object(supervisor,'service',side_effect=Blocked('fake failure')):
            r=wizard.run_wizard(self.s,{'tools':[]},home=self.home,scan=False)
        self.assertTrue(r['已配置']);self.assertIn('本机没有自动备份',r['提醒'][0]);self.assertIn('帮我开后台',r['提醒'][0])
        self.assertEqual(self.s.get('runner:config'),{'backup_dir':existing,'node':'custom'})


class RetireBundledTeamTest(unittest.TestCase):
    """自带战队后台停用（见 DECISIONS 2026-11-17）：旧文件移进归档、只移不删；有没结束的派工记录就不动。"""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.t=Path(self.tmp.name).resolve()
        self.home=self.t/'home';self.home.mkdir();self.root=self.home/'随身开发'
        self.s=Store(self.root)
        for rel in ('工具/team-host-release/host.py','工具/adapters/team-server/run_server.py','工具/runtimes/x/install.json','数据/team-mcp/audit.jsonl'):
            p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('x')
        (self.root/'数据/team-mcp-config.json').write_text('{}')
        (self.root/'工具/mcp.json').write_text('{"mcpServers":{"ai_team":{}}}')
        self.ctx=self.root/'工具/context';self.ctx.mkdir();(self.ctx/'mcp.json').write_text('{"mcpServers":{"portable_context":{}}}')
        (self.root/'工具/adapters/bots').mkdir();(self.root/'工具/adapters/bots/keep.py').write_text('k')
        self.s.put('team_config',{'caller':'portable:x'})
    def tearDown(self):
        self.s.db.close();self.tmp.cleanup()
    def _row(self,state,ref='t1'):
        with self.s.db:self.s.db.execute("INSERT INTO outbox(id,state,reference,body) VALUES(?,?,?,?)",(state+ref,state,ref,'{}'))

    def test_own_team_connection_and_its_config_are_kept(self):
        from portable import team_retire
        own=self.t/'我的战队/run_server.py';own.parent.mkdir();own.write_text('x')
        self.s.put('team_config',{'python':'/usr/bin/python3','server':str(own),'caller':'portable:x'})
        self._row('submitted')  # 自己战队上还在跑的活，不拦、不改
        r=team_retire.retire(self.s,archive_home=self.home)
        self.assertEqual(r['状态'],'已停用');self.assertEqual(r['保留'],'你自己的战队连接')
        self.assertFalse((self.root/'工具/team-host-release').exists())
        self.assertTrue((self.root/'工具/mcp.json').exists())
        self.assertEqual(self.s.get('team_config')['server'],str(own))
        self.assertEqual(self.s.db.execute("SELECT state FROM outbox").fetchone()[0],'submitted')

    def test_own_team_without_bundled_leftovers_is_untouched(self):
        from portable import team_retire
        import shutil
        for rel in ('工具/team-host-release','工具/adapters/team-server','工具/runtimes','数据/team-mcp'):shutil.rmtree(self.root/rel)
        (self.root/'数据/team-mcp-config.json').unlink()
        own=self.t/'我的战队/run_server.py';own.parent.mkdir();own.write_text('x')
        self.s.put('team_config',{'python':'/usr/bin/python3','server':str(own),'caller':'portable:x'})
        r=team_retire.retire(self.s,archive_home=self.home)
        self.assertEqual(r['状态'],'没有自带战队后台')
        self.assertTrue((self.root/'工具/mcp.json').exists());self.assertEqual(self.s.get('team_config')['server'],str(own))
        self.assertFalse((self.home/'归档').exists())

    def test_moves_everything_to_archive_and_clears_config(self):
        from portable import team_retire
        self._row('terminal')
        r=team_retire.retire(self.s,archive_home=self.home)
        self.assertEqual(r['状态'],'已停用')
        arch=Path(r['归档']);self.assertTrue(arch.is_relative_to(self.home/'归档'))
        for rel in ('工具/team-host-release','工具/adapters/team-server','工具/runtimes','数据/team-mcp','数据/team-mcp-config.json','工具/mcp.json'):
            self.assertFalse((self.root/rel).exists(),rel);self.assertTrue((arch/rel).exists(),rel)
        self.assertEqual((arch/'数据/team-mcp/audit.jsonl').read_text(),'x')
        self.assertTrue((self.root/'工具/adapters/bots/keep.py').exists())
        self.assertTrue((self.ctx/'mcp.json').exists())
        self.assertEqual(self.s.get('team_config',{}),{})
        self.assertTrue(self.s.get('team_config:retired'))
        self.assertEqual([r[0] for r in self.s.db.execute('SELECT state FROM outbox')],['historical-readonly'])
        # 再跑一次什么都不做；之后升级前的对账不再需要战队
        self.assertEqual(team_retire.retire(self.s,archive_home=self.home)['状态'],'没有自带战队后台')
        from portable.team import reconcile_outbox
        self.assertEqual(reconcile_outbox(self.s),{'terminal':0})

    def test_open_dispatch_keeps_everything(self):
        from portable import team_retire
        self._row('pending')
        r=team_retire.retire(self.s,archive_home=self.home)
        self.assertEqual(r['状态'],'没停用');self.assertIn('没结束',r['原因'])
        self.assertTrue((self.root/'数据/team-mcp/audit.jsonl').exists())
        self.assertEqual(self.s.get('team_config'),{'caller':'portable:x'})
        self.assertFalse((self.home/'归档').exists())

    def test_cross_disk_copy_then_remove(self):
        from portable import team_retire
        real=os.rename
        def fake(a,b):
            if '归档' in str(b):raise OSError(18,'cross-device')
            return real(a,b)
        with patch('portable.team_retire.os.rename',side_effect=fake):
            r=team_retire.retire(self.s,archive_home=self.home)
        self.assertEqual(r['状态'],'已停用')
        self.assertTrue((Path(r['归档'])/'工具/runtimes/x/install.json').exists())
        self.assertFalse((self.root/'工具/runtimes').exists())

    def test_rollback_to_version_that_ships_it_keeps_it(self):
        from portable import team_retire
        (self.root/'发行.json').write_text(json.dumps({'version':'0.2.51','files':{'工具/team-host-release/host.py':'x'}}))
        self.assertEqual(team_retire.retire(self.s,archive_home=self.home)['状态'],'这一版自带，不动')
        self.assertTrue((self.root/'工具/team-host-release/host.py').exists());self.assertTrue(self.s.get('team_config'))

    def test_new_builds_carry_no_bundled_team(self):
        src=Path(__file__).resolve().parents[1]
        text=(src/'portable/distribution.py').read_text()
        self.assertNotIn("candidate/'工具/team-host-release'",text)
        self.assertNotIn("candidate/'工具/adapters/team-server'",text)

class TalkStyleTest(Base):
    """说话方式：朋友版第一次配置默认大白话；完整版和已经配过的不动。"""
    def _edition(self,edition):
        self.s.root.mkdir(parents=True,exist_ok=True)
        (self.s.root/'发行.json').write_text(json.dumps({'version':'0.2.52',**({'edition':edition} if edition else {})}))
    def _rules(self):return (self.s.root/settings.RULES).read_text()

    def test_friend_first_setup_defaults_to_plain_talk(self):
        self._edition('friend')
        wizard.run_wizard(self.s,{'owner_name':'小王'},scan=False)
        self.assertEqual(settings.load(self.s)['talk_style'],'大白话')
        self.assertIn('- 【说话方式】大白话',self._rules())

    def test_full_edition_and_rerun_keep_numbered(self):
        self._edition(None)
        wizard.run_wizard(self.s,{'owner_name':'小王'},scan=False)
        self.assertEqual(settings.load(self.s)['talk_style'],'')
        self.assertNotIn('说话方式',self._rules())
        # 已经配过的朋友版再跑一次（比如升级后）也不改
        self._edition('friend')
        wizard.run_wizard(self.s,{'owner_name':'小王'},scan=False)
        self.assertNotIn('说话方式',self._rules())

    def test_user_can_switch_and_bad_value_rejected(self):
        settings.set_value(self.s,'talk_style','带编号')
        self.assertIn('- 【说话方式】带编号',self._rules())
        with self.assertRaises(Blocked):settings.set_value(self.s,'talk_style','随便')

    def test_public_report_rule_appends_plain_talk_clause_only(self):
        rules=json.loads((Path(__file__).resolve().parents[1]/'portable/protocol.json').read_text())['public_rules']
        report=[r for r in rules if r.startswith('【汇报】')];self.assertEqual(len(report),1)
        self.assertTrue(report[0].startswith('【汇报】汇报和过程消息都用简体中文大白话（命令、代码、报错原文照抄），越短越好'))
        self.assertIn('写了【说话方式】大白话时',report[0]);self.assertIn('没写时照上面带编号',report[0])

class UpgradePreloadTest(unittest.TestCase):
    def test_every_runtime_module_is_loaded_before_files_are_swapped(self):
        import sys
        from portable.core import PROTOCOL
        missed=upgrade_channel.preload()
        self.assertEqual(missed,[])
        for name in PROTOCOL['distribution']['runtime_files']:
            if name.endswith('.py') and name not in ('__init__.py','__main__.py'):
                self.assertIn('portable.'+name[:-3],sys.modules,name)
