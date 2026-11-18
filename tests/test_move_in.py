"""项目搬家：扫描、原地登记、不覆盖、可撤回（全部在临时目录）。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from portable.core import Store,Blocked
from portable import move_in,board

ENV={**os.environ,'GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1'}

def g(root,*args):
    subprocess.run(['git','-c','user.name=t','-c','user.email=t@t','-C',str(root),*args],check=True,capture_output=True,env=ENV)

def repo(path,files):
    path.mkdir(parents=True);g(path,'init','-q','-b','main')
    for k,v in files.items():(path/k).parent.mkdir(parents=True,exist_ok=True);(path/k).write_text(v)
    g(path,'add','.');g(path,'commit','-qm','feat: first')
    return path


class MoveInTest(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.home=Path(temp.name).resolve()
        patcher=patch.dict(os.environ,{'PORTABLE_HOME':str(self.home)});patcher.start();self.addCleanup(patcher.stop)
        self.s=Store(self.home/'随身开发');self.addCleanup(self.s.db.close)
        self.shop=repo(self.home/'code/shop',{'app.py':'print(1)\n','AGENTS.md':'# 自己的规则\n'})
        self.tmp=repo(self.home/'code/tmp-20261001',{'x':'1'})
        self.claude=repo(self.home/'elsewhere/notes',{'n.md':'1'})
        sess=self.home/'.claude/projects/p';sess.mkdir(parents=True)
        (sess/'a.jsonl').write_text(json.dumps({'type':'user','cwd':str(self.claude)})+'\n')

    def adopt(self,path,**kw):
        move_in.scan(self.s,[Path(path).parent])
        return move_in.adopt(self.s,path,confirmed='搬这个',**kw)

    def test_adopt_needs_user_confirmation_and_a_scan_first(self):
        with self.assertRaisesRegex(Blocked,'还没让用户确认'):move_in.adopt(self.s,self.shop)
        with self.assertRaisesRegex(Blocked,'不在最近一次扫描清单'):move_in.adopt(self.s,self.shop,confirmed='搬 shop')
        self.assertEqual(board.load(self.s),[])
        self.assertFalse((self.shop/'STATUS.md').exists())

    def test_plain_project_listed_and_only_gets_git_with_consent(self):
        plain=self.home/'code/rakazo';plain.mkdir(parents=True)
        (plain/'docker-compose.yml').write_text('services: {}\n');(plain/'.env').write_text('PASSWORD=x\n')
        rows={r['名字']:r for r in move_in.scan(self.s,[self.home/'code'])}
        self.assertFalse(rows['rakazo']['是Git']);self.assertFalse(rows['rakazo']['建议勾选']);self.assertIn('先问用户',rows['rakazo']['搬之前'])
        self.assertTrue(rows['shop']['是Git'])
        with self.assertRaisesRegex(Blocked,'--init-git'):move_in.adopt(self.s,plain,confirmed='搬 rakazo')
        self.assertFalse((plain/'.git').exists())
        r=move_in.adopt(self.s,plain,confirmed='搬 rakazo，可以建 Git',init_git=True)
        self.assertTrue(r['新建了Git']);self.assertTrue((plain/'.git').is_dir())
        self.assertIn('.env',(plain/'.gitignore').read_text())
        self.assertEqual(subprocess.run(['git','-C',str(plain),'rev-list','--all'],capture_output=True,text=True,env=ENV).stdout,'')  # 不替用户提交
        self.assertEqual((plain/'.env').read_text(),'PASSWORD=x\n')
        u=move_in.undo(self.s,r['撤回号'])
        self.assertIn('.git',u['移到归档']);self.assertIn('.gitignore',u['移到归档'])
        self.assertFalse((plain/'.git').exists());self.assertTrue((plain/'docker-compose.yml').exists())

    def test_adopt_opens_a_project_window(self):
        r=self.adopt(self.shop)
        self.assertIn('新开对话',r['项目窗口']);self.assertIn(str(self.shop/'AGENTS.md'),r['启动语'])
        class FakeT3:
            def __init__(self):self.calls=[]
            def add_project(self,path,title):self.calls.append((str(path),title));return True
        fake=FakeT3();other=self.home/'elsewhere/notes'
        move_in.scan(self.s,[self.home/'elsewhere'])
        r=move_in.adopt(self.s,other,confirmed='搬 notes',t3=fake)
        self.assertEqual(fake.calls,[(str(other),'notes')]);self.assertIn('已加进 T3',r['项目窗口'])

    def test_adopt_creates_workflow_and_first_line_entry(self):
        self.adopt(self.tmp)
        text=(self.tmp/'docs/工作法.md').read_text()
        self.assertIn('# tmp-20261001 工作法',text)
        self.assertIn('| 日期 | 场景 | 做法 | 为什么 | 出处 |',text)
        self.assertIn('docs/工作法.md',(self.tmp/'AGENTS.md').read_text().splitlines()[0])
        self.assertIn('STATUS.md',(self.tmp/'AGENTS.md').read_text().splitlines()[0])
        self.assertIn('已经排队的继续做完',(self.tmp/'AGENTS.md').read_text().splitlines()[0])
        self.assertIn('按风险分级',(self.tmp/'AGENTS.md').read_text().splitlines()[0])
        desk=board.overview(self.s)['分档']
        self.assertIn('tmp-20261001',[item['项目'] for item in desk['先别动']])

    def test_scan_lists_repos_and_agent_traces(self):
        rows={r['名字']:r for r in move_in.scan(self.s,[self.home/'code'])}
        self.assertTrue(rows['shop']['建议勾选']);self.assertEqual(rows['shop']['已有说明'],['AGENTS.md'])
        self.assertTrue(rows['tmp-20261001']['像临时目录']);self.assertFalse(rows['tmp-20261001']['建议勾选'])
        self.assertEqual(rows['notes']['在用的AI'],['Claude Code'])  # found only through the session trace

    def test_adopt_registers_in_place_without_overwriting_and_undo(self):
        before=(self.shop/'AGENTS.md').read_text()
        r=self.adopt(self.shop)
        self.assertEqual((self.shop/'AGENTS.md').read_text(),before)
        self.assertIn('STATUS.md',r['新建']);self.assertNotIn('AGENTS.md',r['新建']);self.assertTrue(r['未改'])
        status=(self.shop/'STATUS.md').read_text()
        self.assertIn('等谁: 待确认',status);self.assertIn('feat: first',status)
        self.assertTrue(Path(r['备份']).is_file())
        self.assertEqual([e['id'] for e in board.load(self.s)],['shop'])
        self.assertTrue(move_in.scan(self.s,[self.home/'code'])[-1]['已登记'] or any(x['已登记'] for x in move_in.scan(self.s,[self.home/'code'])))
        with self.assertRaises(Blocked):self.adopt(self.shop)
        u=move_in.undo(self.s,r['撤回号'])
        self.assertFalse((self.shop/'STATUS.md').exists());self.assertTrue((self.shop/'app.py').exists())
        self.assertTrue((Path(u['归档位置'])/'STATUS.md').is_file());self.assertFalse(u['永久删除'])
        self.assertEqual(board.load(self.s),[])

    def test_undo_keeps_files_already_committed(self):
        r=self.adopt(self.shop)
        g(self.shop,'add','STATUS.md');g(self.shop,'commit','-qm','status')
        u=move_in.undo(self.s,r['撤回号'])
        self.assertTrue((self.shop/'STATUS.md').exists());self.assertIn('STATUS.md（已被提交，留着）',u['保留'])

    def test_adopt_and_undo_empty_repo(self):
        empty=self.home/'code/empty';empty.mkdir(parents=True);g(empty,'init','-q','-b','main')
        r=self.adopt(empty)
        self.assertIn('空仓库',r['备份']);self.assertTrue((empty/'STATUS.md').is_file())
        self.assertIn(r['编号'],[e['id'] for e in board.load(self.s)])
        u=move_in.undo(self.s,r['撤回号'])
        self.assertIn('STATUS.md',u['移到归档']);self.assertFalse((empty/'STATUS.md').exists())

    def test_rejects_non_git(self):
        (self.home/'plain').mkdir()
        with self.assertRaises(Blocked):self.adopt(self.home/'plain')


if __name__=='__main__':unittest.main()


class RejoinTest(unittest.TestCase):
    """换电脑接着做（SD-33）：旧电脑每日备份 → 新电脑 rejoin。"""
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);t=Path(temp.name).resolve()
        self.a=t/'old';self.b=t/'new';self.a.mkdir();self.b.mkdir()
    def _old_machine(self):
        from portable import distribution
        with patch.dict(os.environ,{'PORTABLE_HOME':str(self.a)}):
            s=Store(self.a/'随身开发')
            try:
                s.put('runner:config',{'backup_dir':str(self.a/'随身开发备份')})
                shop=repo(self.a/'shop',{'app.py':'v1\n','.gitignore':'.env\n'})
                (shop/'app.py').write_text('v1\nwip\n');(shop/'notes.txt').write_text('note\n');(shop/'.env').write_text('PASSWORD=x\n')
                bare=self.a/'remote.git';subprocess.run(['git','init','-q','--bare',str(bare)],check=True)
                web=repo(self.a/'web',{'index.html':'hi\n'})
                g(web,'remote','add','origin',str(bare));g(web,'push','-q','origin','main')
                move_in.scan(s,[self.a])
                move_in.adopt(s,shop,confirmed='搬 shop');move_in.adopt(s,web,confirmed='搬 web')
                daily=distribution.backup(s,str(self.a/'随身开发备份'),'daily')
                self.assertFalse(daily['external']['failed'])
            finally:s.db.close()
        return self.a/'随身开发备份'
    def test_rejoin_plan_then_restore_and_relink(self):
        backup=self._old_machine()
        with patch.dict(os.environ,{'PORTABLE_HOME':str(self.b)}):
            s=Store(self.b/'随身开发')
            try:
                plan=move_in.rejoin(s,backup)
                self.assertEqual({r['做法'] for r in plan['计划']},{'接回'});self.assertEqual(board.load(s),[])
                (self.b/'web').mkdir()  # 同名文件夹已存在：跳过、不覆盖
                r=move_in.rejoin(s,backup,confirmed='接回来')
                rows={x['编号']:x for x in r['结果']}
                self.assertEqual(rows['shop']['结果'],'已接回');self.assertIn('跳过',rows['web']['做法'])
                shop=self.b/'shop'
                self.assertIn('wip',(shop/'app.py').read_text());self.assertEqual((shop/'notes.txt').read_text(),'note\n')
                self.assertFalse((shop/'.env').exists());self.assertIn('.env',rows['shop']['没带过来（被 .gitignore 忽略，要你自己拷或重新生成）'])
                self.assertIn(str(self.b/'随身开发'),(shop/'AGENTS.md').read_text().splitlines()[0])
                self.assertNotIn(str(self.a),(shop/'AGENTS.md').read_text().splitlines()[0])
                self.assertEqual([e['path'] for e in board.load(s)],[str(shop)])
                self.assertTrue(list(Path(r['改前备份']).glob('shop-AGENTS.md')))
                self.assertEqual(os.listdir(self.b/'web'),[])
                (self.b/'web').rmdir()
                again=move_in.rejoin(s,backup,confirmed='web 也接回')
                rows={x['编号']:x for x in again['结果']}
                self.assertIn('不动',rows['shop']['做法']);self.assertEqual(rows['web']['结果'],'已接回',rows['web'])
                self.assertEqual((self.b/'web/index.html').read_text(),'hi\n')
            finally:s.db.close()
    def test_missing_backup_folder_is_reported(self):
        with patch.dict(os.environ,{'PORTABLE_HOME':str(self.b)}):
            s=Store(self.b/'随身开发')
            try:
                with self.assertRaisesRegex(Blocked,'找不到旧电脑的备份文件夹'):move_in.rejoin(s,self.b/'nope')
                (self.b/'empty').mkdir()
                with self.assertRaisesRegex(Blocked,'没有每日备份'):move_in.rejoin(s,self.b/'empty')
            finally:s.db.close()
