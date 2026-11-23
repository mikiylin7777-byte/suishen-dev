"""0.2.1 注册表、全局进度、--sync 与外部备份回归（全部在临时目录）。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.dont_write_bytecode=True
from portable.core import Store,Blocked
from portable import board

STATUS='---\n项目: 甲\n阶段: 开发中\n完成度: 40\n最后更新: 2026-10-26\n下一步: 做报表\n卡在: 无\n---\n\n## 做到哪了\n'
FEATURES='| PRD编号 | 功能 | 状态 | 验收 | 说明 |\n|---|---|---|---|---|\n| A-1.1 | 登录 | ✅完成 | 真实数据 | |\n| A-1.2 | 报表 | 🔄开发中 | 无 | |\n| 无 | 导出 | ⏳未开始 | 无 | 见 DECISIONS |\n'


def _private_words():
    """本机不想发出去的词：仓库里有 tools/ci/privacy_markers.txt 就读它（见 DECISIONS 2026-11-17），公开版里它是示例名单。"""
    f=Path(__file__).resolve().parents[1]/'tools/ci/privacy_markers.txt'
    return [l.strip() for l in f.read_text(encoding='utf-8').splitlines() if l.strip() and not l.strip().startswith('#')] if f.is_file() else []

class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.t=Path(self.tmp.name).resolve()
        self.s=Store(self.t/'包')
        home=patch.dict(os.environ,{'PORTABLE_HOME':str(self.t/'home')});home.start();self.addCleanup(home.stop)  # 不读真实的 ~/归档
        self.repo=self.t/'repo';self.repo.mkdir()
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        (self.repo/'STATUS.md').write_text(STATUS);(self.repo/'功能清单.md').write_text(FEATURES)
        (self.repo/'docs').mkdir();(self.repo/'docs/PRD.md').write_text('# PRD\n')
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True)
        subprocess.run(['git','-C',str(self.repo),'-c','user.name=t','-c','user.email=t@t','commit','-qm','init'],check=True)
        (self.repo/'草稿.txt').write_text('未跟踪')
        self.card=self.t/'拍档/05/甲';self.card.mkdir(parents=True)
        self.mother=self.t/'拍档';(self.mother/'母体.md').write_text('不动')
        r=str(self.repo)
        self.entries=[{'id':'jia','name':'甲','path':r,'status_path':r+'/STATUS.md','features_path':r+'/功能清单.md','prd_path':r+'/docs/PRD.md','card_path':str(self.card/'甲_项目卡.md')},
                      {'id':'mother','name':'拍档','path':str(self.mother),'status_path':str(self.mother/'STATUS.md'),'backup':'full'}]
    def tearDown(self):self.tmp.cleanup()

    def test_public_rules_ship_no_owner_names(self):
        from portable.core import PROTOCOL
        text='\n'.join(PROTOCOL['public_rules'])
        for word in ('/Users/','~/工作区','OWNER 用',*_private_words()):self.assertNotIn(word,text)

    def test_apply_replaces_registry_and_exports(self):
        old=self.t/'old';old.mkdir();self.s.register('旧','%s'%old,'old-one')
        result=board.apply(self.s,self.entries,remove=['old-one'])
        self.assertEqual(result['removed_from_registry'],['old-one']);self.assertTrue(old.is_dir())
        exported=json.loads((self.s.root/'数据/项目注册表.json').read_text())
        self.assertEqual([p['id'] for p in exported['projects']],['jia','mother'])
        with self.assertRaises(Blocked):board.apply(self.s,[{'id':'x','name':'x','path':str(self.t/'none')}])

    def test_apply_stops_on_local_only_project(self):
        board.apply(self.s,self.entries)
        extra=self.t/'扫码';extra.mkdir();self.s.register('扫码库存',str(extra),'local-1')
        before=(self.s.root/'数据/项目注册表.json').read_text()
        with self.assertRaises(Blocked) as e:board.apply(self.s,self.entries)
        self.assertIn('扫码库存',str(e.exception));self.assertIn('local-1',str(e.exception))
        ids=[r['id'] for r in self.s.db.execute('SELECT id FROM projects')]
        self.assertIn('local-1',ids);self.assertIn('jia',ids)
        self.assertEqual((self.s.root/'数据/项目注册表.json').read_text(),before)
        board.apply(self.s,self.entries+[{'id':'local-1','name':'扫码库存','path':str(extra)}])
        self.assertIn('local-1',[r['id'] for r in self.s.db.execute('SELECT id FROM projects')])
        result=board.apply(self.s,self.entries,remove=['local-1'])
        self.assertEqual(result['removed_from_registry'],['local-1']);self.assertTrue(extra.is_dir())

    def test_overview_counts_and_sync(self):
        board.apply(self.s,self.entries)
        result=board.overview(self.s)
        jia=result['projects'][0]
        self.assertEqual((jia['阶段'],jia['完成度'],jia['下一步']),('开发中','40','做报表'))
        self.assertEqual(jia['功能'],{'✅':1,'🔄':1,'⏳':1,'⛔':0,'🚫':0})
        self.assertEqual(result['projects'][1]['阶段'],'缺STATUS.md')
        self.assertIn('| 甲 | - | - | 开发中 | 40% |',result['page'])
        synced=board.overview(self.s,sync=True)['sync']
        b=self.s.root/'看板'
        self.assertEqual((b/'甲/STATUS.md').read_text(),STATUS)
        self.assertTrue((b/'全部项目.md').is_file());self.assertTrue((b/'跨项目避坑.md').is_file())
        self.assertFalse((self.card/'STATUS.md').exists())  # 不再往旧的项目卡目录复制
        (self.repo/'STATUS.md').write_text(STATUS.replace('40','50'))
        board.overview(self.s,sync=True);self.assertIn('50',(b/'甲/STATUS.md').read_text())

    def test_reads_merged_main_when_worktree_is_behind(self):
        remote=self.t/'remote.git';subprocess.run(['git','init','-q','--bare','-b','main',str(remote)],check=True)  # 不依赖本机 init.defaultBranch
        g=lambda *a:subprocess.run(['git','-C',str(self.repo),'-c','user.name=t','-c','user.email=t@t',*a],check=True,capture_output=True)
        g('branch','-M','main');g('remote','add','gitea',str(remote));g('push','-q','gitea','main')
        other=self.t/'other';subprocess.run(['git','clone','-q',str(remote),str(other)],check=True)
        (other/'STATUS.md').write_text(STATUS.replace('2026-10-26','2026-10-28').replace('做报表','做导出'))
        subprocess.run(['git','-C',str(other),'-c','user.name=t','-c','user.email=t@t','commit','-qam','x'],check=True)
        subprocess.run(['git','-C',str(other),'push','-q','origin','HEAD:main'],check=True)
        board.apply(self.s,self.entries)
        self.assertEqual(board.overview(self.s)['projects'][0]['下一步'],'做报表')  # not fetched yet
        self.assertIn('jia',board.fetch_remotes(self.s))
        r=board.overview(self.s)['projects'][0]
        self.assertEqual((r['下一步'],r['来源']),('做导出','gitea/main'))
        self.assertNotIn('未合并',board.render([r]))
        self.assertEqual((self.repo/'STATUS.md').read_text(),STATUS)  # worktree untouched

    def test_reads_development_branch_when_it_is_newest(self):
        remote=self.t/'remote.git';subprocess.run(['git','init','-q','--bare','-b','main',str(remote)],check=True)
        g=lambda *a:subprocess.run(['git','-C',str(self.repo),'-c','user.name=t','-c','user.email=t@t',*a],check=True,capture_output=True)
        g('branch','-M','main');g('remote','add','gitea',str(remote));g('push','-q','gitea','main')
        other=self.t/'other';subprocess.run(['git','clone','-q',str(remote),str(other)],check=True)
        o=lambda *a:subprocess.run(['git','-C',str(other),'-c','user.name=t','-c','user.email=t@t',*a],check=True,capture_output=True)
        (other/'STATUS.md').write_text(STATUS.replace('2026-10-26','2026-10-28').replace('做报表','做导出'))
        o('commit','-qam','main');o('push','-q','origin','HEAD:main')
        o('checkout','-qb','development')
        (other/'STATUS.md').write_text(STATUS.replace('2026-10-26','2026-10-30').replace('做报表','做打印'))
        o('commit','-qam','dev');o('push','-q','origin','development')
        board.apply(self.s,self.entries)
        self.assertIn('jia',board.fetch_remotes(self.s))
        r=board.overview(self.s)['projects'][0]
        self.assertEqual((r['下一步'],r['来源'],r['最后更新']),('做打印','gitea/development','2026-10-30'))
        self.assertIn('开发分支上的进度，还没合进主线',board.render([r]))
        # 开发分支合回主线、主线又往前走了：改读主线
        o('checkout','-q','main');o('merge','-q','development');(other/'STATUS.md').write_text(STATUS.replace('2026-10-26','2026-11-01').replace('做报表','做上线'))
        o('commit','-qam','main2');o('push','-q','origin','main')
        before=board.board_fingerprint(self.s);board.fetch_remotes(self.s)
        self.assertNotEqual(before,board.board_fingerprint(self.s))
        r=board.overview(self.s)['projects'][0]
        self.assertEqual((r['下一步'],r['来源']),('做上线','gitea/main'))
        # 工作区自己最新：照旧读工作区
        (self.repo/'STATUS.md').write_text(STATUS.replace('2026-10-26','2026-11-05'))
        self.assertEqual(board.overview(self.s)['projects'][0]['来源'],'工作区')

    def _g(self,*a,root=None,date=None):
        env=dict(os.environ,**({'GIT_COMMITTER_DATE':date,'GIT_AUTHOR_DATE':date} if date else {}))
        return subprocess.run(['git','-C',str(root or self.repo),'-c','user.name=t','-c','user.email=t@t',*a],check=True,capture_output=True,text=True,env=env).stdout.strip()

    def test_all_files_follow_development_and_row_names_source_and_running_version(self):
        g=self._g
        g('branch','-M','main');g('update-ref','refs/remotes/origin/main','HEAD')
        g('checkout','-qb','work/old-1106')  # 登记的目录停在旧分支上
        g('checkout','-qb','tmp-dev')
        (self.repo/'STATUS.md').write_text(STATUS.replace('2026-10-26','2026-11-10'))
        (self.repo/'功能清单.md').write_text(FEATURES.replace('🔄开发中','✅完成').replace('⏳未开始','✅完成'))
        g('commit','-qam','dev');dev=g('rev-parse','HEAD');short=g('rev-parse','--short','HEAD')
        g('update-ref','refs/remotes/origin/development','HEAD');g('checkout','-q','work/old-1106');g('branch','-qD','tmp-dev')
        (self.repo/'docs/避坑.md').write_text('| 日期 | 主题 | 场景 | 坑 | 经验 | 出处 |\n|---|---|---|---|---|---|\n| 2026-11-01 | x | 只在工作区 | 坑 | 经验 | - |\n')
        board.apply(self.s,self.entries)
        result=board.overview(self.s)
        r=result['projects'][0]
        self.assertEqual(r['功能'],{'✅':3,'🔄':0,'⏳':0,'⛔':0,'🚫':0})  # 数字出自开发分支，不是旧分支
        self.assertEqual(r['来源'],'origin/development')
        self.assertEqual(r['文件来源']['功能清单.md'],'origin/development');self.assertEqual(r['文件来源']['PRD.md'],'origin/development')
        self.assertEqual(r['文件来源']['避坑.md'],'工作区（origin/development 上没有这份）')
        self.assertEqual(result['pitfalls'],1)
        src=r['开发进度来源']
        self.assertEqual((src['分支'],src['提交'],src['完整提交'],src['领先主线']),('origin/development',short,dev,True))
        self.assertRegex(src['日期'],r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')
        self.assertEqual((r['运行版本'],r['开发进度未上线']),('待确认',True))
        self.assertIn('origin/development · '+short+' · '+src['日期'],result['page'])
        self.assertIn('甲（⚠ 开发进度未上线）',result['page']);self.assertIn('⚠ 开发进度未上线：待确认',result['page'])
        self.assertIn('开发进度还没上线（看板里标了 ⚠，以「运行版本」为准）：甲。',result['brief'])
        board.overview(self.s,sync=True)
        self.assertIn('✅完成 | 无 | 见',(self.s.root/'看板/甲/功能清单.md').read_text())  # 看板里的副本也出自同一处
        self.assertEqual((self.repo/'功能清单.md').read_text(),FEATURES)  # 工作区没被动
        self.assertEqual(g('rev-parse','--abbrev-ref','HEAD'),'work/old-1106')

    def test_running_version_from_status_and_mismatch(self):
        g=self._g;g('branch','-M','main')
        board.apply(self.s,self.entries)
        r=board.overview(self.s)['projects'][0]
        self.assertEqual((r['运行版本'],r['运行版本出处'],r['开发进度未上线']),('待确认','',False))  # 没记录也没领先主线：不标
        self.assertIn('| 工作区（main） · '+g('rev-parse','--short','HEAD'),board.render([r]))
        self.assertNotIn('（⚠',board.render([r]));self.assertNotIn('⚠ 开发进度未上线：',board.render([r]))
        head=g('rev-parse','--short','HEAD')
        (self.repo/'STATUS.md').write_text(STATUS.replace('卡在:','线上版本: v1.2（提交 '+head+'）\n卡在:'))
        r=board.overview(self.s)['projects'][0]
        self.assertEqual((r['运行版本'],r['运行版本出处'],r['开发进度未上线']),('v1.2（提交 '+head+'）','STATUS.md「线上版本」',False))
        self.assertTrue(r['开发进度来源']['有未提交改动']);self.assertIn('有未提交改动',r['开发进度来源说明'])
        g('commit','-qam','新功能')  # 进度往前走了，线上还是旧提交
        r=board.overview(self.s)['projects'][0]
        self.assertTrue(r['开发进度未上线'])
        self.assertIn('⚠ 开发进度未上线：v1.2（提交 '+head+'）（STATUS.md「线上版本」）',board.render([r]))
        (self.repo/'STATUS.md').write_text(STATUS.replace('卡在:','已部署: v1.3\n卡在:'))
        r=board.overview(self.s)['projects'][0]
        self.assertEqual((r['运行版本'],r['开发进度未上线']),('v1.3',False))  # 只写了版本号、进度没领先主线：不猜

    def test_running_version_of_tool_repo_reads_installed_release(self):
        g=self._g;g('branch','-M','main')
        (self.repo/'portable').mkdir();(self.repo/'portable/protocol.json').write_text('{"version":"0.9.1"}');(self.repo/'portable/board.py').write_text('')
        g('add','.');g('commit','-qm','tool')
        release=self.s.root/'发行.json';release.write_text('{"schema":1,"version":"0.9.0","files":{}}')
        board.apply(self.s,self.entries)
        commit_at=int(g('log','-1','--format=%ct'))
        os.utime(release,(commit_at-3600,)*2)  # 装在最后一次提交之前
        r=board.overview(self.s)['projects'][0]
        self.assertEqual((r['运行版本'],r['运行版本出处'],r['开发进度未上线']),('0.9.0','安装目录 发行.json',True))
        os.utime(release,(commit_at+3600,)*2)
        self.assertFalse(board.overview(self.s)['projects'][0]['开发进度未上线'])

    def test_weekly_prunes_only_missing_unlocked_worktree_registrations(self):
        g=self._g;g('branch','-M','main')
        wt=self.t/'wt'
        for name in ('keep','gone','locked'):g('worktree','add','-q','-b',name,str(wt/name))
        g('worktree','lock',str(wt/'locked'))
        (wt/'gone').rename(self.t/'moved-gone');(wt/'locked').rename(self.t/'moved-locked')  # 目录不在了（挪走，不删）
        listing=lambda:board._worktrees(self.repo)
        before={w['path']:w for w in listing()}
        self.assertTrue(before[str(wt/'gone')].get('prunable'));self.assertTrue(before[str(wt/'locked')].get('locked'))
        self.assertFalse(before[str(wt/'locked')].get('prunable'))
        board.apply(self.s,self.entries)
        today=time.time()
        found=[m for m in board.weekly_check(self.s,today=today) if '工作树登记' in m]
        self.assertEqual(len(found),1);self.assertIn('甲：自动清掉了 1 条目录已经不在的工作树登记（锁住的没动',found[0])
        after={w['path'] for w in listing()}
        self.assertEqual(set(before)-after,{str(wt/'gone')})  # 只少了标着 prunable 的那条
        self.assertIn(str(wt/'locked'),after);self.assertIn(str(wt/'keep'),after);self.assertIn(str(self.repo),after)
        self.assertTrue((wt/'keep/STATUS.md').is_file());self.assertTrue((self.t/'moved-gone/STATUS.md').is_file())
        self.assertEqual(set(g('branch','--format=%(refname:short)').split()),{'main','keep','gone','locked'})  # 分支一个不少
        (wt/'keep').rename(self.t/'moved-keep')
        self.assertFalse(any('工作树登记' in m for m in board.weekly_check(self.s,today=today)))  # 同一周不再清、不再提醒
        self.assertIn(str(wt/'keep'),{w['path'] for w in listing()})
        self.assertTrue(any('工作树登记' in m for m in board.weekly_check(self.s,today=today+7*86400)))  # 下周照常

    def test_prune_reports_any_unexpected_disappearance(self):
        lists=[[{'path':str(self.repo)},{'path':str(self.t/'none'),'prunable':'gitdir file points to non-existent location'},{'path':str(self.t/'b')}],
               [{'path':str(self.repo)}]]
        with patch.object(board,'_worktrees',side_effect=lists):
            count,problem=board.prune_missing_worktrees(self.repo)
        self.assertEqual(count,1);self.assertIn('意外少了登记：'+str(self.t/'b'),problem)
        (self.t/'here').mkdir()
        with patch.object(board,'_worktrees',return_value=[{'path':str(self.repo)},{'path':str(self.t/'here'),'prunable':'x'}]),patch.object(board.subprocess,'run',wraps=subprocess.run) as run:
            self.assertEqual(board.prune_missing_worktrees(self.repo),(0,'有登记标着可清但目录还在，没动'))
        self.assertFalse(any('prune' in c.args[0] for c in run.call_args_list))

    def test_weekly_lists_merged_clean_and_stale_unmerged_work(self):
        g=self._g;g('branch','-M','main');g('update-ref','refs/remotes/gitea/main','HEAD')
        wt=self.t/'wt'
        g('worktree','add','-q','-b','done-1',str(wt/'done-1'));g('worktree','add','-q','-b','dirty',str(wt/'dirty'))
        (wt/'dirty/新文件.txt').write_text('没提交')
        now=time.time();old=time.strftime('%Y-%m-%dT%H:%M:%S',time.localtime(now-30*86400))
        g('checkout','-qb','old-work');(self.repo/'a.txt').write_text('a');g('add','a.txt');g('commit','-qm','old',date=old)
        g('checkout','-qb','fresh-work');(self.repo/'b.txt').write_text('b');g('add','b.txt');g('commit','-qm','fresh')
        g('checkout','-q','main')
        board.apply(self.s,self.entries)
        before=g('worktree','list','--porcelain');heads=g('for-each-ref','refs/heads/')
        msgs=board.weekly_check(self.s,today=now)
        merged=[m for m in msgs if '已经合并' in m];stale=[m for m in msgs if '没合进主线' in m]
        self.assertEqual(len(merged),1);self.assertIn('甲有 1 个工作树已经合并、也没有未提交改动（如 '+str(wt/'done-1')+'）',merged[0])
        self.assertEqual(len(stale),1);self.assertIn('甲有 1 处超过 14 天没动',stale[0]);self.assertIn('old-work',stale[0]);self.assertNotIn('fresh-work',stale[0])
        self.assertEqual((g('worktree','list','--porcelain'),g('for-each-ref','refs/heads/')),(before,heads))  # 只读
        self.assertTrue((wt/'dirty/新文件.txt').is_file())
        self.assertFalse(any('已经合并' in m or '没合进主线' in m for m in board.weekly_check(self.s,today=now)))  # 一周一次

    def test_owner_answered_or_no_rush_is_not_waiting(self):
        row=lambda who:{'项目':'甲','状态':'进行中','阶段':'开发中','等谁':who,'下一步':'x','卡在':'','停了几天':1,'_owner_name':'OWNER（小周）'}
        cases={
            'OWNER——已答先不，等本人明确恢复领星调研；产品经理——第③步结论':(False,True),
            '小周——本人日后登录后补截图（不催）；其余验收按原项目记录':(False,True),
            '两个宿主待验；普通开发不再等小周逐项批准':(False,True),
            '小周——已答先不；小周批准 PR84':(True,False),
            '小周（备份份数看图）；Vps（脚本回执）':(True,False),
            '业务口径只停对应项':(False,False),
        }
        for who,(waiting,quiet) in cases.items():
            with self.subTest(who=who):
                self.assertEqual((board.waiting_on_owner(row(who)),board.owner_quiet(row(who))),(waiting,quiet))
        rows=[row('小周——本人日后补截图（不催）')|{'项目':'乙'},row('小周批准 PR84')]
        text=board.brief(rows)
        self.assertIn('等你拍板的：甲（你答过、不用催的：乙）',text)
        piles=board.attention(rows)
        self.assertEqual(([i['项目'] for i in piles['等你']],[i['项目'] for i in piles['先别动']]),(['甲'],['乙']))

    def test_long_term_goal_shown_and_stall_after_two_days(self):
        st=lambda done:STATUS.replace('阶段:',f'状态: 进行中\n长期目标: 完成 {done} 块 / 共 8 块；本期：报表、导出；卡在：无\n阶段:')
        (self.repo/'STATUS.md').write_text(st(3))
        board.apply(self.s,self.entries)
        day=lambda d:time.mktime(time.strptime(d,'%Y-%m-%d'))+3600
        self.assertEqual(board.goal_numbers('完成 3 块 / 共 8 块；本期：x'),(3,8));self.assertIsNone(board.goal_numbers('无'))
        r=board.overview(self.s,sync=True,today=day('2026-11-10'))
        self.assertEqual(r['长期目标'][0],{'项目':'甲','长期目标':'完成 3 块 / 共 8 块；本期：报表、导出；卡在：无','没前进天数':0})
        self.assertIn('| 甲 | 进行中 | 完成 3 块 / 共 8 块；本期：报表、导出；卡在：无 |',r['page'])
        self.assertNotIn('没前进',r['brief'])
        self.assertEqual(board.overview(self.s,today=day('2026-11-11'))['长期目标'][0]['没前进天数'],1)
        r=board.overview(self.s,sync=True,today=day('2026-11-12'))
        self.assertIn('长期目标两天以上没前进：甲',r['brief']);self.assertIn('（2 天没前进）',r['page'])
        (self.repo/'STATUS.md').write_text(st(4))
        r=board.overview(self.s,sync=True,today=day('2026-11-13'))
        self.assertEqual(r['长期目标'][0]['没前进天数'],0);self.assertNotIn('没前进',r['brief'])
        # 只读查看（不 sync）不写记录；按项目 id 记，不按名字
        self.assertEqual(self.s.get(board.GOAL_LOG)['jia']['since'],'2026-11-13')
        # 审核 11-14：只改总块数不算前进、不重新计时
        (self.repo/'STATUS.md').write_text(st(4).replace('共 8 块','共 9 块'))
        self.assertEqual(board.overview(self.s,sync=True,today=day('2026-11-15'))['长期目标'][0]['没前进天数'],2)
        # 全部完成的不算停滞
        (self.repo/'STATUS.md').write_text(st(8))
        board.overview(self.s,sync=True,today=day('2026-11-16'))
        r=board.overview(self.s,sync=True,today=day('2026-11-20'))
        self.assertIsNone(r['长期目标'][0]['没前进天数']);self.assertNotIn('没前进',r['brief'])
        # 暂缓期间不计：暂缓时记一次，中间后台没刷新，恢复当天从 0 算，第二天 1 天（复审 11-14）
        (self.repo/'STATUS.md').write_text(st(5).replace('状态: 进行中','状态: 暂缓（等客户）'))
        board.overview(self.s,sync=True,today=day('2026-11-21'))
        (self.repo/'STATUS.md').write_text(st(5))
        self.assertEqual(board.overview(self.s,today=day('2026-12-02'))['长期目标'][0]['没前进天数'],0)  # 只读查看也认恢复
        self.assertEqual(board.overview(self.s,sync=True,today=day('2026-12-02'))['长期目标'][0]['没前进天数'],0)
        self.assertEqual(board.overview(self.s,sync=True,today=day('2026-12-03'))['长期目标'][0]['没前进天数'],1)

    def test_wait_brief_pitfalls_and_weekly(self):
        st=STATUS.replace('阶段:','状态: 进行中\n等谁: OWNER（确认报表口径）\n阶段:')
        (self.repo/'STATUS.md').write_text(st)
        (self.repo/'docs/避坑.md').write_text('| 日期 | 主题 | 场景 | 坑 | 经验 | 出处 |\n|---|---|---|---|---|---|\n| 2026-10-26 | 交付维护 | 升级时 | 备份失败没人发现 | 每日看结果 | STATUS |\n| 2026-10-27 | x |  | 缺场景不收 | - | - |\n')
        board.apply(self.s,self.entries)
        today=time.mktime(time.strptime('2026-11-06','%Y-%m-%d'))
        r=board.overview(self.s,sync=True,today=today)
        self.assertEqual(r['projects'][0]['等谁'],'OWNER（确认报表口径）');self.assertEqual(r['projects'][0]['停了几天'],11)
        self.assertIn('等你拍板的：甲',r['brief']);self.assertEqual(r['pitfalls'],1)
        self.assertIn('备份失败没人发现',(self.s.root/'看板/跨项目避坑.md').read_text())
        self.assertIn('等谁',r['page'])
        self.assertEqual(board.weekly_check(self.s,today=today),['甲 等你 11 天了：OWNER（确认报表口径）'])
        self.assertEqual(board.weekly_check(self.s,today=today),[])  # 只提醒一次
        later=today+10*86400
        self.assertEqual(board.weekly_check(self.s,today=later),['甲 已 21 天没更新进度'])
        (self.repo/'STATUS.md').write_text(st.replace('状态: 进行中','状态: 暂缓（等客户）'))
        self.assertIn('1 个暂缓',board.overview(self.s,today=later)['brief'])

    def test_working_memory_keeps_matching_lessons_only(self):
        (self.repo/'STATUS.md').write_text(STATUS.replace('卡在: 无','卡在: 报表口径'))
        (self.repo/'docs/避坑.md').write_text('| 日期 | 主题 | 场景 | 坑 | 经验 | 出处 |\n|---|---|---|---|---|---|\n| 2026-10-26 | 报表 | 做报表 | 口径没人核对 | 先对口径再出数 | 甲 |\n| 2026-10-20 | 打印 | 打印机 | 缺纸没人发现 | 先看纸盒 | 甲 |\n')
        (self.repo/'docs/DECISIONS.md').write_text('# 决定\n\n- 2026-10-26 · 做报表前先把口径写下来。\n- 2026-10-01 · 打印机耗材单独记账。\n')
        (self.repo/'docs/工作法.md').write_text('| 日期 | 场景 | 做法 | 为什么 | 出处 |\n|---|---|---|---|---|\n| 2026-10-26 | 做报表 | 口径变更要重出 | 旧数会混 | 甲 |\n| 2026-10-01 | 打印机 | 换纸先停机 | 免得卡纸 | 甲 |\n')
        other=self.t/'乙';other.mkdir();(other/'STATUS.md').write_text('---\n项目: 乙\n状态: 进行中\n阶段: 开发中\n最后更新: 2026-10-26\n下一步: 写说明\n---\n')
        board.apply(self.s,self.entries+[{'id':'yi','name':'乙','path':str(other),'status_path':str(other/'STATUS.md')}])
        memory=board.overview(self.s)['工作记忆']
        self.assertEqual([item['坑'] for item in memory['要避开']],['口径没人核对'])
        reused=[item['做法'] for item in memory['可复用']]
        self.assertTrue(any('口径变更要重出' in item for item in reused))
        self.assertTrue(any('口径写下来' in item for item in reused))
        self.assertFalse(any('打印' in item['做法'] or '纸' in item['做法'] for item in memory['可复用']))
        self.assertIn('乙', memory['还没记下'])
        self.assertNotIn('甲', memory['还没记下'])
        lone=board.working_memory(
            [{'项目':'甲','状态':'进行中','阶段':'开发中','下一步':'做报表','等谁':'','卡在':'没人核对口径'}],
            [['甲','2026-10-01','人手','','没人值班','先排班']],
            [])
        self.assertEqual(lone['要避开'],[])
        self.assertIn('甲', [item['项目'] for item in memory['在做']])

    def test_knowledge_page_gets_status_card(self):
        board.apply(self.s,self.entries)
        e=board.load(self.s)[0]
        note=self.s.root/'知识库/项目'/(e['id']+'.md');note.parent.mkdir(parents=True,exist_ok=True)
        note.write_text('# 甲\n\n<!-- AUTO BEGIN -->\n## 当前目标\n尚未记录\n<!-- AUTO END -->\n## 手写区\n我的笔记\n')
        board.overview(self.s,sync=True)
        text=note.read_text()
        self.assertIn('## 进度（来自项目的 STATUS.md）',text);self.assertIn('随身开发/看板/全部项目.md',text)
        self.assertLess(text.index('<!-- 进度 BEGIN -->'),text.index('<!-- AUTO BEGIN -->'))
        self.assertIn('我的笔记',text);self.assertIn('尚未记录',text)
        board.overview(self.s,sync=True);self.assertEqual(note.read_text().count('<!-- 进度 BEGIN -->'),1)  # 再跑不重复
        self.assertIn('还没有进度卡',board.knowledge_card({'阶段':'缺STATUS.md'}))
        missing=self.s.root/'知识库/项目/不存在.md';self.assertFalse(missing.exists())

    def test_weekly_awaiting_team_tasks(self):
        today=time.mktime(time.strptime('2026-11-06','%Y-%m-%d'))
        board.apply(self.s,self.entries)
        board.weekly_check(self.s,today=today)
        tasks=[
            {'task_id':'task-old','title':'旧候选','status':'awaiting_acceptance','idle_hours':7*24+1},
            {'task_id':'task-young','title':'刚做完','status':'awaiting_acceptance','idle_hours':6*24},
            {'task_id':'task-stopped','title':'中断的','status':'interrupted','idle_hours':30*24},
        ]
        self.assertEqual(board.weekly_check(self.s,today=today,tasks=tasks),['战队任务「旧候选」做完 7 天了还没人验收'])
        self.assertEqual(board.weekly_check(self.s,today=today,tasks=tasks),[])  # 只提醒一次
        self.assertEqual(board.weekly_check(self.s,today=today,tasks=None),[])

    def test_weekly_flags_stale_prd_once(self):
        board.apply(self.s,self.entries)
        def git(*a,date=None):
            env=dict(os.environ,**({'GIT_COMMITTER_DATE':date,'GIT_AUTHOR_DATE':date} if date else {}))
            subprocess.run(['git','-C',str(self.repo),'-c','user.name=t','-c','user.email=t@t',*a],check=True,env=env,stdout=subprocess.DEVNULL)
        git('commit','-q','--amend','--no-edit',date='2026-09-01T00:00:00')
        today=time.mktime(time.strptime('2026-11-06','%Y-%m-%d'))
        for i in range(19):git('commit','-q','--allow-empty','-m',f'c{i}',date='2026-10-01T00:00:00')
        self.assertFalse(any('需求稿' in m for m in board.weekly_check(self.s,today=today)))  # 才 19 次
        git('commit','-q','--allow-empty','-m','c19',date='2026-10-02T00:00:00')
        found=[m for m in board.weekly_check(self.s,today=today) if '需求稿' in m]
        self.assertEqual(len(found),1);self.assertIn('甲的需求稿 66 天没改，之后仓库有 20 次提交（按工作区）',found[0])
        self.assertFalse(any('需求稿' in m for m in board.weekly_check(self.s,today=today)))  # 只提醒一次
        (self.repo/'docs/PRD.md').write_text('# PRD v2\n');git('commit','-qam','prd',date='2026-11-05T00:00:00')
        self.assertIsNone(board.prd_drift(board.load(self.s)[0],today))  # 改过需求稿就不再提醒
        # 远端开发分支上需求稿更新过，本地没拉：按需求稿最新的那条线算，不误报
        git('reset','-q','--hard','HEAD~1')
        git('update-ref','refs/remotes/origin/development','HEAD')
        (self.repo/'docs/PRD.md').write_text('# PRD v3\n');git('commit','-qam','prd3',date='2026-11-05T00:00:00')
        git('update-ref','refs/remotes/origin/development','HEAD');git('reset','-q','--hard','HEAD~1')
        self.assertIsNone(board.prd_drift(board.load(self.s)[0],today))
        # 复审 11-14：需求稿同一版，本地落后、远端主线已往前走了很多次，按走得最远的那条算
        git('update-ref','-d','refs/remotes/origin/development')
        git('update-ref','refs/remotes/origin/main','HEAD')
        base_head=subprocess.run(['git','-C',str(self.repo),'rev-parse','HEAD'],capture_output=True,text=True).stdout.strip()
        for i in range(25):git('commit','-q','--allow-empty','-m',f'm{i}',date='2026-10-20T00:00:00')
        git('update-ref','refs/remotes/origin/main','HEAD');git('reset','-q','--hard',base_head)
        git('reset','-q','--hard','HEAD~15')  # 本地只剩之后 5 次
        drift=board.prd_drift(board.load(self.s)[0],today)
        self.assertEqual((drift[1],drift[3]),(45,'origin/main'))

    def test_weekly_reminds_old_archive_once_a_week(self):
        board.apply(self.s,self.entries)
        home=self.t/'home';old=home/'归档/2026-09/旧工作目录';old.mkdir(parents=True);(old/'a.bin').write_bytes(b'x'*2048)
        fresh=home/'归档/2026-11/新的';fresh.mkdir(parents=True)
        today=time.mktime(time.strptime('2026-11-06','%Y-%m-%d'))
        os.utime(old,(today-40*86400,)*2);os.utime(fresh,(today-3*86400,)*2)
        with patch.dict(os.environ,{'PORTABLE_HOME':str(home)}):
            found=[m for m in board.weekly_check(self.s,today=today) if '归档' in m]
            self.assertEqual(len(found),1);self.assertIn('1 样东西放满 30 天',found[0]);self.assertIn('确认后才删',found[0])
            self.assertFalse(any('归档' in m for m in board.weekly_check(self.s,today=today)))  # 同一周只提醒一次
            self.assertTrue(any('归档' in m for m in board.weekly_check(self.s,today=today+7*86400)))  # 下周再提醒
        self.assertTrue(old.exists())  # 只提醒，不删

    def test_prune_skips_whole_repo_when_a_missing_worktree_is_on_external_volume(self):
        from unittest.mock import patch as p2
        fake=[{'path':'/Volumes/没插的盘/wt','prunable':True},{'path':str(self.t/'gone'),'prunable':True}]
        with p2.object(board,'_worktrees',return_value=fake),p2('portable.board.subprocess.run') as run:
            count,problem=board.prune_missing_worktrees(self.repo)
        self.assertEqual(count,0);self.assertIn('外接盘',problem);run.assert_not_called()

    def test_doctor_shows_real_retention(self):
        r=self.s.doctor()['retention']
        self.assertEqual((r['daily'],r['weekly'],r['upgrade'],r['permanent_delete']),(7,4,2,True))

    def test_weekly_progress_lag_and_docs_gate_drift(self):
        board.apply(self.s,self.entries)
        def git(*a,date=None):
            env=dict(os.environ,**({'GIT_COMMITTER_DATE':date,'GIT_AUTHOR_DATE':date} if date else {}))
            subprocess.run(['git','-C',str(self.repo),'-c','user.name=t','-c','user.email=t@t',*a],check=True,env=env,stdout=subprocess.DEVNULL)
        today=time.mktime(time.strptime('2026-11-20','%Y-%m-%d'))
        (self.repo/'STATUS.md').write_text(STATUS+'x\n');git('commit','-qam','status',date='2026-11-10T00:00:00')
        (self.repo/'docs/说明.md').write_text('d\n');git('add','docs/说明.md');git('commit','-qm','doc only',date='2026-11-12T00:00:00')
        git('update-ref','refs/remotes/origin/development','HEAD')
        self.assertIsNone(board.progress_lag(board.load(self.s)[0],today))  # 之后只有文档：不算
        (self.repo/'app.py').write_text('x=1\n');git('add','app.py');git('commit','-qm','feature',date='2026-11-13T00:00:00')
        git('update-ref','refs/remotes/origin/development','HEAD')
        self.assertEqual(board.progress_lag(board.load(self.s)[0],today),(1,'origin/development'))
        self.assertTrue(any('进度表还没更新' in m for m in board.weekly_check(self.s,today=today)))
        self.assertFalse(board.docs_gate_drift(board.load(self.s)[0]))  # 没接轻量 CI
        (self.repo/'tools/ci').mkdir(parents=True);(self.repo/'tools/ci/docs_only.py').write_text('print("mode=docs")\n')
        git('add','tools/ci/docs_only.py');git('commit','-qm','gate');git('update-ref','refs/remotes/origin/development','HEAD')
        self.assertTrue(board.docs_gate_drift(board.load(self.s)[0]))
        original=(Path(__file__).resolve().parents[1]/'portable/ci_docs_only.py').read_bytes()
        (self.repo/'tools/ci/docs_only.py').write_bytes(original);git('commit','-qam','same');git('update-ref','refs/remotes/origin/development','HEAD')
        self.assertFalse(board.docs_gate_drift(board.load(self.s)[0]))

    def test_awaiting_team_tasks_reads_registered_entry(self):
        board.apply(self.s,self.entries)
        self.assertEqual(board.awaiting_team_tasks(self.s),[])  # 没有战队入口
        entry=self.repo/'bin/codex-task';entry.parent.mkdir(exist_ok=True)
        entry.write_text('#!/bin/sh\necho \'{"groups":{"c":[{"task_id":"task-a","title":"甲","status":"awaiting_acceptance","idle_hours":200},{"task_id":"task-b","status":"interrupted","idle_hours":300}]}}\'\n')
        entry.chmod(0o755)
        self.assertEqual([t['task_id'] for t in board.awaiting_team_tasks(self.s)],['task-a'])
        entry.write_text('#!/bin/sh\nexit 3\n')
        self.assertIsNone(board.awaiting_team_tasks(self.s))

    def test_pending_desk_shows_fresh_acceptance_and_continuable_work(self):
        (self.repo/'STATUS.md').write_text(STATUS.replace('下一步: 做报表','下一步: 继续做报表').replace('卡在: 无','卡在: 等总管核清单'))
        other=self.t/'乙';other.mkdir();(other/'STATUS.md').write_text('---\n项目: 乙\n状态: 进行中\n阶段: 开发中\n最后更新: 2026-10-26\n下一步: 先不合，等批准\n---\n')
        held=self.t/'丙';held.mkdir();(held/'STATUS.md').write_text('---\n项目: 丙\n状态: 暂缓\n阶段: 暂停\n最后更新: 2026-10-26\n下一步: 继续写说明\n---\n')
        board.apply(self.s,self.entries+[
            {'id':'yi','name':'乙','path':str(other),'status_path':str(other/'STATUS.md')},
            {'id':'bing','name':'丙','path':str(held),'status_path':str(held/'STATUS.md')},
        ])
        tasks=[
            {'task_id':'task-new','title':'刚做完','status':'awaiting_acceptance','idle_hours':0.2,'caller':'custom:supervisor','workspace':str(self.repo)},
            {'task_id':'task-old','title':'旧的','status':'interrupted','idle_hours':200},
        ]
        desk=board.pending_desk(board.overview(self.s)['projects'],tasks,board.load(self.s))
        self.assertEqual(desk['待验收数'],1)
        self.assertEqual(desk['待验收'][0]['任务'],'task-new')
        self.assertEqual(desk['待验收'][0]['项目'],'甲')
        self.assertEqual(desk['待验收'][0]['调用方'],'custom:supervisor')
        self.assertEqual([item['项目'] for item in desk['等总管']],['甲'])
        self.assertIn('甲',[item['项目'] for item in desk['可继续']])
        self.assertNotIn('乙',[item['项目'] for item in desk['可继续']])
        self.assertNotIn('丙',[item['项目'] for item in desk['可继续']])
        owner=board.pending_desk([{'项目':'丁','状态':'进行中','下一步':'继续做导出','等谁':'你','卡在':'','停了几天':0}],[],[])
        self.assertEqual(owner['可继续'],[])
        self.assertTrue(board.overview(self.s)['待处理']['待验收没读到'] is False)
        hours=[200,100,48,24,12,6,3,1,0.2]
        crowded=[{'task_id':'task-'+str(h),'title':str(h),'status':'awaiting_acceptance','idle_hours':h} for h in hours]
        shown=board.pending_desk([],crowded,[])
        self.assertEqual(shown['待验收数'],9)
        self.assertIn('task-0.2',[item['任务'] for item in shown['待验收']])
        self.assertNotIn('task-200',[item['任务'] for item in shown['待验收']])
        mention=board.pending_desk([{'项目':'戊','状态':'进行中','下一步':'0.2.39 总管待处理与工作记忆待 CI 后合并','等谁':'','卡在':'','停了几天':0}],[],[])
        self.assertEqual(mention['等总管'],[])
        self.assertEqual([item['项目'] for item in mention['可继续']],['戊'])

    def test_reply_stays_on_the_desk_until_it_is_read(self):
        board.apply(self.s,self.entries)
        with self.assertRaises(Blocked):board.expect_reply(self.s,'','r1','甲','问提交号')
        board.expect_reply(self.s,'thread-a','r1','甲','问提交号')
        again=board.expect_reply(self.s,'thread-a','r1','甲','另一句')
        self.assertTrue(again['已有']);self.assertEqual(again['问了什么'],'问提交号')
        self.assertEqual([item['请求号'] for item in board.overview(self.s)['待处理']['等回复']],['r1'])
        board.record_reply(self.s,'r1','提交号 abc')
        self.assertEqual(board.overview(self.s)['待处理']['等回复'],[])
        board.expect_reply(self.s,'thread-a','r2','甲','再问一句')
        board.record_reply(self.s,' r2 ','读到了')
        self.assertEqual(board.overview(self.s)['待处理']['等回复'],[])
        with self.assertRaises(Blocked):board.record_reply(self.s,'没有','无')
        from portable.__main__ import main
        self.assertEqual(main(['--root',str(self.s.root),'reply','list']),0)

    def test_wait_check_separates_machine_user_and_independent_work(self):
        held={'编号':'pr-7','目标':'待批准','结束条件':'用户说合','回传位置':'总管线程 d58','pr':'http://127.0.0.1:3000/pr/7','状态':'待批准','问题':'甲项目这批先不合','恢复条件':'用户说合','依赖':[],'已授权':False,'阻塞':True,'跨度':'长','能唤醒':False}
        ci={'编号':'pr-9','目标':'等CI','结束条件':'等合并','回传位置':'总管线程 d58','pr':'http://127.0.0.1:3000/pr/9','状态':'CI通过','依赖':[],'已授权':True,'阻塞':False,'跨度':'短','能唤醒':True,'已读':True}
        blocked={'编号':'报表','目标':'等检查','结束条件':'检查通过','回传位置':'总管线程 d58','状态':'进行中','等谁':'机器','依赖':[],'已授权':True,'阻塞':True,'跨度':'短','已读':False}
        free={'编号':'说明','目标':'写说明','结束条件':'写完','回传位置':'总管线程 d58','状态':'进行中','等谁':'机器','依赖':[],'已授权':True,'阻塞':False,'跨度':'短','已读':False}
        stuck={'编号':'跟着报表','目标':'等报表','结束条件':'报表做完','回传位置':'总管线程 d58','状态':'进行中','等谁':'机器','依赖':['报表'],'已授权':True,'阻塞':False,'跨度':'长','能唤醒':False}
        found=board.assess_waits([held,ci,blocked,free,stuck])
        by={item['编号']:item for item in found['项']}
        self.assertEqual(by['pr-7']['监视'],'解除')
        self.assertTrue(by['pr-7']['保留PR']);self.assertFalse(by['pr-7']['关闭PR'])
        self.assertEqual(by['pr-7']['问题'],'甲项目这批先不合');self.assertEqual(by['pr-7']['恢复条件'],'用户说合')
        self.assertEqual(by['pr-7']['回传'],'未接通');self.assertFalse(by['pr-7']['后台继续']);self.assertFalse(by['pr-7']['用户催算完成'])
        self.assertEqual(by['pr-9']['等谁'],'机器');self.assertEqual(by['pr-9']['结束条件'],'CI 通过')
        self.assertFalse(by['pr-9']['改成等合并']);self.assertEqual(by['pr-9']['监视'],'可收尾');self.assertEqual(by['pr-9']['回传'],'已读回')
        self.assertIn('说明',found['可做']);self.assertNotIn('报表',found['可做']);self.assertNotIn('跟着报表',found['可做']);self.assertNotIn('pr-7',found['可做'])
        self.assertIn('pr-7',found['未接通']);self.assertIn('跟着报表',found['未接通'])
        board.apply(self.s,self.entries)
        path=self.t/'waits.json';path.write_text(json.dumps([held,ci,free],ensure_ascii=False),encoding='utf-8')
        from portable.__main__ import main
        self.assertEqual(main(['--root',str(self.s.root),'wait-check',str(path)]),0)
        self.assertEqual([item['编号'] for item in board.overview(self.s)['待处理']['等待']],['pr-7','pr-9','说明'])
        with self.assertRaises(Blocked):board.assess_waits([{'编号':'缺','目标':'等CI','结束条件':'CI 通过','回传位置':''}])

    def test_attention_sorts_four_piles_and_names_cross_links(self):
        rows=[
            {'项目':'甲','状态':'进行中','下一步':'做报表','等谁':'OWNER','卡在':'','停了几天':3},
            {'项目':'乙','状态':'进行中','下一步':'等口径','等谁':'甲','卡在':'','停了几天':1},
            {'项目':'庚','状态':'进行中','下一步':'继续写接口','等谁':'','卡在':'无','停了几天':4},
            {'项目':'辛','状态':'进行中','下一步':'接庚的接口','等谁':'庚','卡在':'','停了几天':2},
            {'项目':'丙','状态':'进行中','下一步':'继续做导出','等谁':'','卡在':'无','停了几天':0},
            {'项目':'丁','状态':'暂缓','下一步':'写说明','等谁':'','卡在':'','停了几天':9},
            {'项目':'戊','状态':'进行中','下一步':'PR 12 收尾','等谁':'','卡在':'','停了几天':2},
            {'项目':'己','状态':'进行中','下一步':'合 PR12','等谁':'','卡在':'','停了几天':2},
            {'项目':'壬','状态':'进行中','下一步':'等对方','等谁':'癸','卡在':'','停了几天':1},
            {'项目':'癸','状态':'进行中','下一步':'等对方','等谁':'壬','卡在':'','停了几天':1},
        ]
        desk=board.attention(rows,[{'name':'窗一','path':str(self.repo)},{'name':'窗二','path':str(self.repo)}])
        self.assertEqual([item['项目'] for item in desk['等你']],['甲'])
        self.assertIn('庚',[item['项目'] for item in desk['堵住别人']])
        self.assertIn('丙',[item['项目'] for item in desk['现在能做']])
        self.assertIn('丁',[item['项目'] for item in desk['先别动']])
        self.assertIn('辛',[item['项目'] for item in desk['先别动']])
        self.assertIn({'等':'乙','被等':'甲'},desk['谁等谁'])
        self.assertIn(sorted(('壬','癸')),desk['互相等'])
        self.assertIn({'标记':'PR12','项目':['戊','己']},desk['同一件'])
        self.assertEqual(desk['撞车'],[{'项目':['窗一','窗二']}])
        nested=board.attention([
            {'项目':'报表','状态':'进行中','下一步':'继续','等谁':'报表口径组','卡在':'','停了几天':0},
            {'项目':'报表口径','状态':'进行中','下一步':'继续导出','等谁':'','卡在':'无','停了几天':0},
            {'项目':'午','状态':'进行中','下一步':'继续写','等谁':'先不合','卡在':'','停了几天':0},
        ])
        self.assertNotIn({'等':'报表','被等':'报表口径'},nested['谁等谁'])
        self.assertIn('午',[item['项目'] for item in nested['先别动']])
        self.assertNotIn('午',[item['项目'] for item in nested['现在能做']])
        empty=subprocess.CompletedProcess(['git'],0)
        with patch('portable.board.subprocess.run',return_value=empty):
            quiet=board.attention([{'项目':'丙','状态':'进行中','下一步':'继续做导出','等谁':'','卡在':'无','停了几天':0}],[{'name':'窗','path':str(self.repo)}])
        self.assertEqual(quiet['撞车'],[])
        self.assertIn('丙',[item['项目'] for item in quiet['现在能做']])

    def test_weekly_empty_learning_notebook(self):
        today=time.mktime(time.strptime('2026-10-26','%Y-%m-%d'))
        notebook=self.repo/'docs/工作法.md'
        template='# 工作法\n\n> 被纠正时记一笔\n\n| 日期 | 做法 |\n|---|---|\n'
        board.apply(self.s,self.entries)
        for case,days,content,paused in (
            ('missing',14,None,False),
            ('young',13,template,False),
            ('filled',14,template+'| 今天 | 记一笔 |\n',False),
            ('paused',14,template,True),
            ('empty',14,template,False),
        ):
            with self.subTest(case=case):
                (self.repo/'STATUS.md').write_text(STATUS.replace('阶段:',('状态: 暂缓（等客户）\n' if paused else '')+'阶段:'))
                if content is not None:
                    notebook.write_text(content)
                    os.utime(notebook,(today-days*86400,today-days*86400))
                expected=['甲的工作法本子建了 14 天还是空的：被纠正或踩坑时记一笔'] if case=='empty' else []
                self.assertEqual(board.weekly_check(self.s,today=today),expected)
        self.assertIn('甲:learn-empty',self.s.get('weekly:told'))
        self.assertEqual(board.weekly_check(self.s,today=today+86400),[])

    def test_backup_external(self):
        board.apply(self.s,self.entries)
        (self.repo/'STATUS.md').write_text('改了')
        out=self.t/'备份'
        result=board.backup_external(self.s,out)
        dest=Path(result['backup'])
        self.assertTrue((dest/'jia/repo.bundle').is_file())
        self.assertIn('改了',(dest/'jia/未提交改动.patch').read_text())
        self.assertEqual((dest/'jia/未跟踪文件/草稿.txt').read_text(),'未跟踪')
        self.assertEqual((dest/'mother/母体.md').read_text(),'不动')
        subprocess.run(['git','-C',str(self.repo),'bundle','verify',str(dest/'jia/repo.bundle')],check=True,capture_output=True)

    def test_reads_unmerged_branch_when_worktree_lacks_file(self):
        r=str(self.repo);g=['git','-C',r,'-c','user.name=t','-c','user.email=t@t']
        subprocess.run(['git','-C',r,'checkout','-qb','chore/status-prd'],check=True)
        (self.repo/'STATUS.md').write_text(STATUS.replace('40','70'));subprocess.run(g+['commit','-qam','s'],check=True)
        subprocess.run(['git','-C',r,'checkout','-q','-'],check=True)
        subprocess.run(['git','-C',r,'rm','-q','STATUS.md'],check=True);subprocess.run(g+['commit','-qm','rm'],check=True)
        board.apply(self.s,self.entries)
        row=board.overview(self.s)['projects'][0]
        self.assertEqual((row['完成度'],row['来源']),('70','chore/status-prd'))

    def test_rotation_ignores_receipt_files(self):
        from portable.distribution import backup
        out=self.t/'备份';out.mkdir();(out/'upgrade-receipt-x.json').write_text('{}')
        result=backup(self.s,out,'upgrade')
        self.assertTrue(Path(result['backup']).is_dir());self.assertTrue((out/'upgrade-receipt-x.json').is_file())

    def test_menu_daily_five_first(self):
        from portable.onboarding import menu
        with patch('sys.stdin.isatty',return_value=False):
            self.assertEqual(menu(self.s)['choices'],['看所有项目进度','给某项目提需求','看某个需求进展','立刻备份','打开某项目目录','高级'])

if __name__=='__main__':unittest.main()


class PercentTest(unittest.TestCase):
    def test_percent_not_doubled(self):
        base={'项目':'p','状态':'进行中','阶段':'a','下一步':'b','等谁':'c','卡在':'无','最后更新':'2026-11-03','功能':None,'来源':'工作区','停了几天':0}
        page=board.render([dict(base,完成度='50%'),dict(base,项目='q',完成度='78'),dict(base,项目='r',完成度='一期 100%（已验收）')])
        self.assertNotIn('%%',page);self.assertIn('| 50% |',page);self.assertIn('| 78% |',page);self.assertIn('一期 100%（已验收）',page)

    def test_percent_only_appended_to_pure_numbers(self):
        base={'项目':'p','状态':'进行中','阶段':'a','下一步':'b','等谁':'c','卡在':'无','最后更新':'2026-11-03','功能':None,'来源':'工作区','停了几天':0}
        def done(v):return board.render([dict(base,完成度=v)])
        self.assertIn('| 60% |',done('60'))
        self.assertIn('| 60.5% |',done('60.5'))
        self.assertIn('| 60% |',done('60%'));self.assertNotIn('| 60%% |',done('60%'))
        self.assertIn('| 1 / 2 块 |',done('1 / 2 块'))
        self.assertIn('| 完成 3 块 / 共 5 块 |',done('完成 3 块 / 共 5 块'))

class BackupProtectionTest(unittest.TestCase):
    setUp=BoardTest.setUp
    tearDown=BoardTest.tearDown
    def test_isolates_empty_corrupt_and_missing_projects(self):
        board.apply(self.s,self.entries)
        empty=self.t/'empty';empty.mkdir()
        subprocess.run(['git','init','-q',str(empty)],check=True)
        (empty/'draft').write_text('保留')
        broken=self.t/'broken'
        import shutil
        shutil.copytree(self.repo,broken)
        (broken/'.git/HEAD').rename(broken/'saved-head')
        self.s.register('空仓库',str(empty),'empty')
        self.s.register('坏仓库',str(broken),'broken')
        absent=self.t/'absent';absent.mkdir();self.s.register('丢失目录',str(absent),'absent');board.apply(self.s,self.entries+[{'id':'empty','name':'空仓库','path':str(empty)},{'id':'broken','name':'坏仓库','path':str(broken)},{'id':'absent','name':'丢失目录','path':str(absent)}]);absent.rmdir()
        out=self.t/'backups';r=board.backup_external(self.s,out)
        self.assertEqual(r['failed'],['坏仓库','丢失目录'])
        dest=Path(r['backup'])
        self.assertEqual((dest/'empty/draft').read_text(),'保留')
        self.assertTrue((dest/'empty/.git/HEAD').is_file())
        self.assertTrue((dest/'jia/repo.bundle').is_file())
        self.assertEqual(list(out.glob('.外部-*')),[])
        report=json.loads((dest/'外部备份.json').read_text())['projects']
        self.assertEqual([p['项目'] for p in report if p.get('失败')],r['failed'])

    def test_backup_gap_and_pause_reason_lead_the_brief(self):
        # 2026-11-16：关机曾被写成暂停、每日备份停两天没人知道；现在排在摘要和待处理最前面
        import datetime
        from portable import supervisor
        from portable.automation import pause
        board.apply(self.s,self.entries)
        now=time.time();three_days_ago=(datetime.datetime.fromtimestamp(now).date()-datetime.timedelta(days=3)).isoformat()
        with patch.object(supervisor,'service',return_value={'installed':True}):
            self.s.put('runner:config',{'backup_dir':str(self.t/'backup')});self.s.put('runner:heartbeat',{'at':now,'pid':1})
            self.s.put('runner:daily',three_days_ago)
            r=board.overview(self.s,today=now)
            self.assertTrue(r['brief'].startswith('⚠'));self.assertIn('每日备份已 2 天没成功',r['brief'])
            self.assertIn('安全网',r['待处理']);self.assertEqual(list(r['待处理'])[0],'安全网')
            pause(self.s,by='runner stop 命令',reason='测试')
            r=board.overview(self.s,today=now)
            pause_line=[x for x in r['提醒'][:2] if '后台处于暂停' in x];self.assertTrue(pause_line);self.assertIn('runner stop 命令',pause_line[0])

    def test_shutdown_pause_is_auto_resumed_but_explicit_pause_kept(self):
        from portable import supervisor
        from portable.automation import pause,state,save
        v=state(self.s);v['paused']=True;save(self.s,v);self.s.put('runner:stopped',{'at':1})  # 旧版关机留下的：没写原因
        self.assertTrue(supervisor.resume_legacy_shutdown_pause(self.s,100));self.assertFalse(state(self.s)['paused'])
        self.assertIn('自动恢复',self.s.get('runner:auto-resumed')['reason'])
        pause(self.s,by='runner stop 命令',reason='用户手动停')  # 人明确停的：不动
        self.assertFalse(supervisor.resume_legacy_shutdown_pause(self.s,200));self.assertTrue(state(self.s)['paused'])

    def test_reminders_and_healthy_state(self):
        import datetime
        from portable import supervisor
        board.apply(self.s,self.entries)
        now=time.time();today=datetime.datetime.fromtimestamp(now).strftime('%Y-%m-%d')
        with patch.object(supervisor,'service',return_value={'installed':True}):
            self.s.put('runner:config',{'backup_dir':str(self.t/'backup')})
            self.s.put('runner:heartbeat',{'at':now,'pid':123})
            self.s.put('runner:daily',today)
            self.assertEqual(board.overview(self.s,today=now)['提醒'],[])
            self.assertEqual(self.s.doctor()['提醒'],[])
            for key,value,word in [('runner:config',{},'还没设备份目录'),('runner:service',{'running':False},'没在跑'),('runner:heartbeat',{'at':now-601},'十分钟'),('runner:daily','yesterday','今天')]:
                old=self.s.get(key)
                self.s.put(key,value)
                for result in (board.overview(self.s,today=now),self.s.doctor()):
                    self.assertEqual(len(result['提醒']),1)
                    self.assertIn(word,result['提醒'][0])
                self.s.put(key,old if old is not None else {})
            with patch.object(supervisor,'service',return_value={'installed':False}):
                self.assertIn('还没开后台',board.backup_reminders(self.s,now)[0])

    def test_runner_retries_without_completion_and_reports_failure(self):
        from portable import supervisor
        board.apply(self.s,self.entries)
        (self.repo/'.git/HEAD').rename(self.repo/'saved-head')
        self.s.put('runner:config',{'backup_dir':str(self.t/'backup')})
        runner=supervisor.Runner(self.s,clock=lambda:1800000000)
        with patch.object(supervisor,'backup',wraps=supervisor.backup) as backup:
            for _ in range(5):runner.tick()
            self.assertEqual(backup.call_count,2)  # 同一时刻只各试一次（每日+每周）；之后每 30 分钟再试（2026-11-16 改：不再一天 3 次就放弃）
        self.assertIsNone(self.s.get('runner:daily'))
        self.assertIn('甲',(self.s.root/'看板/提醒.md').read_text())
        self.assertTrue(self.s.get('runner:backup-problems'))
        self.assertEqual(list((self.t/'backup').glob('.外部-*')),[])
        (self.repo/'saved-head').rename(self.repo/'.git/HEAD')
        runner.clock=lambda:1800000000+86400
        runner.tick()
        self.assertEqual(self.s.get('runner:daily'),'2027-01-16')
        self.assertEqual(self.s.get('runner:backup-problems'),[])
        self.assertNotIn('项目备份失败', (self.s.root/'看板/提醒.md').read_text())

    def test_partial_project_is_moved_to_recovery(self):
        board.apply(self.s,self.entries)
        original=board._git
        def fail(path,*args,**kwargs):
            if args[0]=='diff':raise OSError('synthetic failure')
            return original(path,*args,**kwargs)
        out=self.t/'backup'
        with patch.object(board,'_git',side_effect=fail):r=board.backup_external(self.s,out)
        self.assertEqual(r['failed'],['甲'])
        self.assertTrue((Path(r['backup'])/'mother/母体.md').is_file())
        self.assertEqual(list(out.glob('.外部-*')),[])
        recovered=list((out/'恢复区').glob('jia-*/repo.bundle'))
        self.assertEqual(len(recovered),1)
        subprocess.run(['git','-C',str(self.repo),'bundle','verify',str(recovered[0])],check=True,capture_output=True)


class BackupCheckScheduleTest(unittest.TestCase):
    setUp=BoardTest.setUp
    tearDown=BoardTest.tearDown

    def test_daily_does_not_run_fsck(self):
        board.apply(self.s,self.entries)
        with patch.object(board,'_git',wraps=board._git) as git:
            result=board.backup_external(self.s,self.t/'daily','daily')
        self.assertEqual(result['failed'],[])
        self.assertNotIn('fsck',[call.args[1] for call in git.call_args_list])
        self.assertIn('for-each-ref',[call.args[1] for call in git.call_args_list])
        self.assertTrue((Path(result['backup'])/'jia/repo.bundle').is_file())

    def test_weekly_runs_fsck_with_timeout(self):
        board.apply(self.s,self.entries)
        with patch.object(board,'_git',wraps=board._git) as git:
            result=board.backup_external(self.s,self.t/'weekly','weekly')
        self.assertEqual(result['failed'],[])
        calls=[call for call in git.call_args_list if call.args[1]=='fsck']
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].args,(self.repo,'fsck','--full'))
        self.assertEqual(calls[0].kwargs,{'timeout':300})

    def test_weekly_timeout_keeps_other_projects(self):
        import shutil
        other=self.t/'healthy';shutil.copytree(self.repo,other)
        entries=[self.entries[0],{'id':'healthy','name':'其他项目','path':str(other)}]
        board.apply(self.s,entries)
        original=board._git
        def timeout(path,*args,**kwargs):
            if Path(path)==self.repo and args[0]=='fsck':
                raise subprocess.TimeoutExpired(['git','fsck','--full'],kwargs['timeout'])
            return original(path,*args,**kwargs)
        with patch.object(board,'_git',side_effect=timeout):
            result=board.backup_external(self.s,self.t/'weekly','weekly')
        self.assertEqual(result['failed'],['甲'])
        failed=result['projects'][0]
        self.assertEqual(failed['方式'],'失败：检查超时，已跳过本周全量检查')
        dest=Path(result['backup'])
        self.assertTrue((dest/'healthy/repo.bundle').is_file())
        report=json.loads((dest/'外部备份.json').read_text())
        self.assertEqual(report['projects'][0]['方式'],failed['方式'])
        self.assertEqual(report['projects'][1]['项目'],'其他项目')
        self.assertFalse(report['projects'][1].get('失败',False))

    def test_git_subprocess_receives_timeout(self):
        with patch.object(board.subprocess,'run') as run:
            board._git(self.repo,'fsck','--full',timeout=300)
        self.assertEqual(run.call_args.kwargs['timeout'],300)

    def test_daily_detects_corrupt_bundle_without_fsck(self):
        import shutil
        broken=self.t/'broken-objects';shutil.copytree(self.repo,broken)
        blob=board._git(broken,'rev-parse','HEAD:STATUS.md').stdout.decode().strip()
        (broken/'.git/objects'/blob[:2]/blob[2:]).rename(self.t/'saved-object')
        board.apply(self.s,[{'id':'broken','name':'坏对象仓库','path':str(broken)},self.entries[0]])
        with patch.object(board,'_git',wraps=board._git) as git:
            result=board.backup_external(self.s,self.t/'daily-corrupt','daily')
        self.assertEqual(result['failed'],['坏对象仓库'])
        calls=[call for call in git.call_args_list if call.args[0]==broken]
        self.assertIn('bundle',[call.args[1] for call in calls])
        self.assertNotIn('fsck',[call.args[1] for call in calls])
        self.assertTrue((Path(result['backup'])/'jia/repo.bundle').is_file())


class BackupReasonFirstDayTest(unittest.TestCase):
    def test_never_succeeded_still_shows_why(self):
        import tempfile
        from portable.core import Store
        from portable import board
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d).resolve()/'root')
            try:
                s.put('runner:config',{'backup_dir':str(Path(d)/'b')})
                s.put('runner:backup-last-error',{'at':1.0,'kind':'daily','type':'Blocked','detail':'发现未知数据库，未备份'})
                s.put('runner:backup-problems',['项目备份失败：rakazo；其他项目已保留，请帮我检查后重试'])
                text=' '.join(board.backup_reminders(s,now=1794800000.0))
                self.assertIn('还没成功过一次',text)
                self.assertIn('发现未知数据库',text)
                self.assertIn('项目备份失败：rakazo',text)
            finally:s.db.close()


class RiskyLocationTest(unittest.TestCase):
    def test_downloads_install_is_flagged(self):
        from portable.wizard import risky_location
        home=Path('/Users/demo')
        self.assertIn('下载',risky_location(home/'Downloads/随身开发',home))
        self.assertIn('桌面',risky_location(home/'Desktop/随身开发',home))
        self.assertEqual(risky_location(home/'随身开发',home),'')

