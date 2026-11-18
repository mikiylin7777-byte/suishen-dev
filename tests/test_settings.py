import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from portable.core import Store,Blocked
from portable import settings,board

class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.t=Path(self.tmp.name).resolve()
        self.s=Store(self.t/'包')
        self.a=self.t/'a';self.a.mkdir();self.b=self.t/'b';self.b.mkdir()
    def tearDown(self):self.tmp.cleanup()

    def test_defaults_without_file_are_generic(self):
        v=settings.load(self.s)
        self.assertEqual((v['owner_name'],v['team_name'],v['workspace'],v['projects'],v['extra_rules']),('用户','AI 战队','',None,[]))

    def test_fresh_user_registry_apply_keeps_nothing_hardcoded(self):
        r=board.apply(self.s)
        self.assertEqual((r['registered'],r['removed_from_registry']),([],[]))

    def test_no_settings_means_registry_apply_changes_nothing(self):
        board.apply(self.s,[{'id':'a','name':'甲','path':str(self.a)}])
        self.s.register('乙',str(self.b),'b')
        r=board.apply(self.s)
        self.assertEqual(sorted(r['registered']),['a','b']);self.assertEqual(r['removed_from_registry'],[])

    def test_init_captures_all_current_registrations_once(self):
        board.apply(self.s,[{'id':'a','name':'甲','path':str(self.a),'status_path':str(self.a/'S.md')}])
        self.s.register('乙',str(self.b),'b')
        v=settings.init_from_registry(self.s)
        self.assertEqual([e['id'] for e in v['projects']],['a','b'])
        self.assertEqual(v['projects'][0]['status_path'],str(self.a/'S.md'))
        self.assertEqual(v['projects'][1]['status_path'],str(self.b)+'/STATUS.md')
        p=settings.path(self.s);self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
        settings.set_value(self.s,'projects',[v['projects'][0]])
        self.assertEqual([e['id'] for e in settings.init_from_registry(self.s)['projects']],['a'])

    def test_projects_from_settings_drive_registry_apply_and_guard_still_stops(self):
        settings.set_value(self.s,'projects',[{'id':'a','name':'甲','path':str(self.a)}])
        self.assertEqual(board.apply(self.s)['registered'],['a'])
        self.s.register('乙',str(self.b),'b')
        with self.assertRaises(Blocked):board.apply(self.s)

    def test_rules_file_rendered_from_settings(self):
        settings.set_value(self.s,'team_name','小明的战队')
        settings.set_value(self.s,'extra_rules',['旧系统只移不删'])
        text=(self.s.root/settings.RULES).read_text()
        self.assertIn('小明的战队',text);self.assertIn('- 旧系统只移不删',text)

    def test_bad_values_rejected(self):
        for k,v in (('nope',1),('team_name',3),('extra_rules',['']),('projects',[{'id':'x'}]),('projects',[{'id':'x','name':'x','path':'p'}]*2)):
            with self.assertRaises(Blocked):settings.set_value(self.s,k,v)
        self.assertFalse(settings.path(self.s).exists())

    def test_cli_settings_set_show(self):
        run=lambda *a:subprocess.run([sys.executable,'-m','portable','--root',str(self.s.root),*a],capture_output=True,text=True,cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(run('settings','set','owner_name','小明').returncode,0)
        out=run('settings','show');self.assertEqual(out.returncode,0,out.stderr)
        self.assertIn('小明',out.stdout)

class OptionalHostingAndTeamTest(SettingsTest):
    def test_default_is_local_only_without_team(self):
        settings.render_rules(self.s)
        text=(self.s.root/settings.RULES).read_text()
        self.assertIn('新建项目默认只存本机',text);self.assertIn('没连在线仓库的，代码只存本机',text);self.assertIn('没有接入 AI 战队',text)
        self.assertNotIn('Gitea）。',text);self.assertNotIn('新建项目默认推到在线仓库',text)
        self.assertNotIn('【战队续接】',text);self.assertNotIn('【交接包·战队】',text)

    def test_remote_hosting_and_team_are_opt_in(self):
        settings.set_value(self.s,'code_hosting','remote');settings.set_value(self.s,'hosting_url','http://git.example/me')
        settings.set_value(self.s,'team_enabled',True);settings.set_value(self.s,'team_name','小明的战队')
        text=(self.s.root/settings.RULES).read_text()
        self.assertIn('在线仓库（http://git.example/me）',text);self.assertIn('CI 全绿',text);self.assertIn('已接入「小明的战队」',text)
        self.assertIn('【战队续接】',text);self.assertIn('不为提高战队利用率恢复暂停项目',text);self.assertIn('【交接包·战队】',text);self.assertIn('clientRequestId',text)
        self.assertNotIn('派工入口命令',text)
        settings.set_value(self.s,'team_entry','~/执行墙/bin/codex-dispatch')
        self.assertIn('派工入口命令：~/执行墙/bin/codex-dispatch',(self.s.root/settings.RULES).read_text())
        with self.assertRaises(Blocked):settings.set_value(self.s,'team_entry','a\nb')
        for k,v in (('code_hosting','github'),('team_enabled','yes'),('hosting_url',1)):
            with self.assertRaises(Blocked):settings.set_value(self.s,k,v)

    def test_public_rules_do_not_assume_gitea_or_team(self):
        from portable.core import PROTOCOL
        text='\n'.join(PROTOCOL['public_rules'])
        self.assertNotIn('Gitea',text);self.assertIn('本机流程',text);self.assertIn('没接入战队时，大活由当前窗口按块顺序做',text)

    def test_fresh_install_without_team_runs_doctor_and_overview(self):
        from portable.board import overview
        self.assertIsNone(self.s.get('team_config'))
        self.assertEqual(self.s.doctor()['database'],'ok')
        board.apply(self.s,[{'id':'a','name':'甲','path':str(self.a)}])
        self.assertIn('1 个项目',overview(self.s)['brief'])

if __name__=='__main__':unittest.main()

class DispatchRuleTest(unittest.TestCase):
    def test_dispatch_defaults_to_window_and_parallel_blocks(self):
        # 0.2.43（见 DECISIONS 2026-11-14）：默认窗口自己写；大活拆成互不冲突的块并行派战队。
        from portable.core import PROTOCOL
        rules=PROTOCOL['public_rules']
        rule=next(x for x in rules if x.startswith('【派工】'))
        for words in ('默认窗口自己写','一个窗口能做完的改动','不派战队','互不冲突的块','同时并行派给 AI 战队','拆成 N 块并行','报错原文发给战队会话','改为自己做'):
            self.assertIn(words,rule)
        self.assertNotIn('默认用战队入口派给它',rule);self.assertNotIn('这次没派工，原因：',rule)
        risk=next(x for x in rules if x.startswith('【按风险分级】'))
        for words in ('低风险','高风险','自己审','只拦严重和重要问题','ci merge','备份','回滚','拿不准算哪一档，就按高风险','改规矩'):
            self.assertIn(words,risk)
        self.assertIn('同一个窗口负责到底',rule);self.assertIn('上线和改真实数据',rule);self.assertIn('派工一律走战队入口',rule)  # 0.2.43 瘦身：【一人到底】【派工可见】并入【派工】
        self.assertLess(rules.index(risk),rules.index(next(x for x in rules if x.startswith('【标准套路】'))))
