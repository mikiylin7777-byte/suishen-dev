"""撤回上一个改动、求助包、示例项目、截图（全部在临时目录）。"""
import os
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from portable.core import Store,Blocked
from portable import assist,board
from portable.move_in import DRAFTS

ENV={**os.environ,'GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1'}
def g(root,*args):
    return subprocess.run(['git','-c','user.name=t','-c','user.email=t@t','-C',str(root),*args],check=True,capture_output=True,env=ENV).stdout.decode().strip()


class AssistTest(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.home=Path(temp.name).resolve()
        patcher=patch.dict(os.environ,{'PORTABLE_HOME':str(self.home)});patcher.start();self.addCleanup(patcher.stop)
        self.s=Store(self.home/'随身开发');self.addCleanup(self.s.db.close)
        self.remote=self.home/'remote.git';subprocess.run(['git','init','-q','--bare','-b','main',str(self.remote)],check=True);g(self.remote,'config','receive.advertisePushOptions','true')
        self.repo=self.home/'shop';self.repo.mkdir();g(self.repo,'init','-q','-b','main')
        (self.repo/'a.txt').write_text('one\n');g(self.repo,'add','.');g(self.repo,'commit','-qm','base')
        (self.repo/'a.txt').write_text('two\n');g(self.repo,'commit','-qam','feat: 改成 two')
        g(self.repo,'remote','add','origin',str(self.remote));g(self.repo,'push','-q','origin','main')
        board.apply(self.s,[{'id':'shop','name':'商店','path':str(self.repo),'status_path':str(self.repo/'STATUS.md'),'features_path':'','prd_path':''}])

    def test_undo_last_pushes_revert_without_touching_checkout(self):
        from tests.test_remote_publish_undo import bind_remote
        bind_remote(self,self.repo,self.remote)
        r=assist.undo_last(self.s,'商店')
        self.assertEqual(r['撤回的改动'],'feat: 改成 two');self.assertFalse(r['改历史'])
        ref='refs/pull/7/head'
        self.assertEqual(g(self.remote,'show',ref+':a.txt'),'one')
        self.assertTrue(g(self.remote,'log','-1','--format=%s',ref).startswith('撤回：'))
        self.assertEqual((self.repo/'a.txt').read_text(),'two\n')

    def test_undo_last_without_remote_leaves_local_branch(self):
        g(self.repo,'remote','remove','origin')
        r=assist.undo_last(self.s,'shop')
        self.assertEqual(g(self.repo,'show',r['本地分支']+':a.txt'),'one');self.assertEqual(g(self.repo,'branch','--show-current'),'main')

    def test_help_bundle_has_no_secrets(self):
        self.s.put('runner:config',{'note':'password=hunter2-very-secret-value'})
        r=assist.help_bundle(self.s,self.home/'out')
        with zipfile.ZipFile(r['求助包']) as z:
            text=''.join(z.read(n).decode() for n in z.namelist())
        self.assertNotIn('hunter2-very-secret-value',text);self.assertIn('说明.txt',r['内容'])

    def test_example_project(self):
        r=assist.example(self.s)
        p=Path(r['示例项目'])
        self.assertIn('docs/工作法.md',(p/'AGENTS.md').read_text().splitlines()[0])
        self.assertIn('已经排队的继续做完',(p/'AGENTS.md').read_text().splitlines()[0])
        self.assertIn('按风险分级',(p/'AGENTS.md').read_text().splitlines()[0])
        self.assertIn('示例·我的待办',[item['项目'] for item in board.overview(self.s)['分档']['等你']])
        self.assertEqual((p/'docs/工作法.md').read_text(),DRAFTS['docs/工作法.md']('示例·我的待办'))
        self.assertTrue((p/'index.html').is_file());self.assertEqual(g(p,'log','--format=%s'),'建好示例项目')
        self.assertIn('example-todo',[e['id'] for e in board.load(self.s)])
        self.assertIn('等谁: 你',(p/'STATUS.md').read_text())
        with self.assertRaises(Blocked):assist.example(self.s)

    def test_screenshot_reports_missing_browser(self):
        with patch.object(assist,'_chrome',return_value=None),self.assertRaises(Blocked):assist.screenshot(self.s,'file:///x')

    def test_screenshot_width_and_height(self):
        seen=[]
        def fake(cmd,**kw):
            seen.append(cmd);out=Path(next(a for a in cmd if a.startswith('--screenshot='))[len('--screenshot='):]);out.write_bytes(b'x'*2000)
            return subprocess.CompletedProcess(cmd,0,'','')
        with patch.object(assist,'_chrome',return_value='/bin/chrome'),patch.object(assist.subprocess,'run',side_effect=fake):
            r=assist.screenshot(self.s,'file:///x','默认')
            self.assertIn('--window-size=1280,900',seen[-1]);self.assertEqual((r['宽'],r['高']),(1280,900))
            r=assist.screenshot(self.s,'file:///x','平板',600,800)
            self.assertIn('--window-size=600,800',seen[-1]);self.assertEqual((r['宽'],r['高']),(600,800))
            narrow=lambda browser,url,out,w,h,profile:out.write_bytes(b'x'*2000)
            with patch.object(assist,'_shoot_narrow',side_effect=narrow) as shoot:
                r=assist.screenshot(self.s,'file:///x','手机',390,844)  # Chrome 窗口最窄 500，更窄的走 DevTools 管道
            self.assertEqual(shoot.call_args.args[3:5],(390,844));self.assertEqual((r['宽'],r['高']),(390,844))
            self.assertEqual(len(seen),2)
            with self.assertRaises(Blocked):assist.screenshot(self.s,'file:///x','坏',20,900)
        import argparse
        parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='c');assist.register(sub)
        a=parser.parse_args(['assist','screenshot','file:///x','--width','390'])
        self.assertEqual((a.width,a.height),(390,900))
        a=parser.parse_args(['assist','screenshot','file:///x'])
        self.assertEqual((a.width,a.height),(1280,900))

    @unittest.skipUnless(os.environ.get('PORTABLE_REAL_BROWSER_VERIFY')=='1' and assist._chrome(),'BLOCKED: real browser verification requires external authorization')
    def test_screenshot_real_page(self):
        page=self.home/'p.html';page.write_text('<h1 style="font-size:80px">你好</h1>')
        r=assist.screenshot(self.s,page.as_uri(),'测试')
        self.assertTrue(Path(r['截图']).stat().st_size>1000)
        phone=self.home/'phone.html';phone.write_text('<meta name=viewport content="width=device-width"><h1 id=w></h1><script>w.textContent=innerWidth</script>')
        r=assist.screenshot(self.s,phone.as_uri(),'手机',390,844)
        png=Path(r['截图']).read_bytes()
        self.assertEqual((int.from_bytes(png[16:20],'big'),int.from_bytes(png[20:24],'big')),(390,844))  # PNG 头里的宽高
        with self.assertRaises(Blocked):assist.screenshot(self.s,(self.home/'没有.html').as_uri(),'坏',390,844)


if __name__=='__main__':unittest.main()

class BatchTwo(unittest.TestCase):
    setUp=AssistTest.setUp
    def test_restore_and_omissions(self):
        import json
        (self.repo/'a.txt').write_text('uncommitted\n')
        (self.repo/'extra.txt').write_text('extra')
        (self.repo/'.gitignore').write_text('ignored\n')
        (self.repo/'ignored').write_text('not backed')
        with (self.repo/'huge').open('wb') as f:f.truncate(20*1024*1024+1)
        backup=self.home/'backup';backup.mkdir();self.s.put('runner:config',{'backup_dir':str(backup)})
        result=board.backup_external(self.s,backup)
        omitted=result['projects'][0]['没备的']
        self.assertEqual(omitted['忽略文件数'],1);self.assertEqual(omitted['超过20MB跳过的文件'],['huge'])
        target=self.home/'restored'
        assist.restore_project(self.s,'商店',to=target)
        self.assertEqual(g(target,'log','--format=%H'),g(self.repo,'log','--format=%H'))
        self.assertEqual((target/'a.txt').read_bytes(),(self.repo/'a.txt').read_bytes())
        self.assertEqual((target/'extra.txt').read_text(),'extra')
        self.assertFalse((target/'ignored').exists())
        self.assertEqual(g(target,'status','--porcelain'),'\n'.join(line for line in g(self.repo,'status','--porcelain').splitlines() if line!='?? huge'))
        with self.assertRaises(Blocked):assist.restore_project(self.s,'商店',to=target)

    def test_daily_backs_up_only_what_remote_lacks_and_restores(self):
        # 见 DECISIONS 2026-11-14：在线仓库已有的历史不再每天复制；恢复时从在线仓库或每周备份拿完整历史，再叠当天增量
        backup=self.home/'backup';backup.mkdir();self.s.put('runner:config',{'backup_dir':str(backup)})
        r=board.backup_external(self.s,backup)
        d=Path(r['backup'])/r['projects'][0]['编号']
        self.assertTrue((d/'全部已推送.txt').is_file());self.assertFalse((d/'repo.bundle').exists())
        self.assertEqual(r['projects'][0]['在线仓库'],str(self.remote))
        (self.repo/'a.txt').write_text('local only\n');g(self.repo,'commit','-qam','not pushed')
        r=board.backup_external(self.s,backup)
        d=Path(r['backup'])/r['projects'][0]['编号']
        self.assertTrue((d/'未推送提交.bundle').is_file());self.assertIn('1 个没推上去的提交',r['projects'][0]['方式'])
        out=assist.restore_project(self.s,'商店',to=self.home/'r1')
        self.assertEqual(out['完整历史来自'],'在线仓库')
        self.assertEqual(g(self.home/'r1','log','--format=%H'),g(self.repo,'log','--format=%H'))
        # 在线仓库拿不到：用每周备份（完整历史）兜底
        board.backup_external(self.s,backup,'weekly')
        self.remote.rename(self.home/'remote-gone.git')
        out=assist.restore_project(self.s,'商店',to=self.home/'r2')
        self.assertTrue(out['完整历史来自'].startswith('每周备份'))
        self.assertEqual(g(self.home/'r2','log','--format=%H'),g(self.repo,'log','--format=%H'))
        # 带凭据的地址不写进备份
        g(self.repo,'remote','set-url','origin','https://someone:pa55@example.invalid/a/b.git')
        self.assertIsNone(board._safe_remote_url(self.repo))
        g(self.repo,'remote','set-url','origin','https://example.invalid/a/b.git')
        self.assertEqual(board._safe_remote_url(self.repo),'https://example.invalid/a/b.git')

    def test_alias_template_service_config_archive_doctor_missing(self):
        import plistlib,time
        from portable.supervisor import service,configure_local
        from portable.distribution import archive_residue
        from portable.move_in import adopt
        self.s.put('settings',{'owner_name':'OWNER（小周）'})
        # Settings schema is loaded through the same reader as the board.
        with patch('portable.settings.load',return_value={'owner_name':'OWNER（小周）'}):
            for who in ('OWNER（小周）','小周'):
                self.assertTrue(board.waiting_on_owner({'等谁':who},self.s))
        self.assertIn('✅ 完成 / 🔄 进行中 / ⏳ 未开始 / ⛔ 卡住',DRAFTS['功能清单.md']('x'))
        self.assertIn('| ⏳ 未开始 |',DRAFTS['功能清单.md']('x'))
        value=service(self.s.root,self.home/'agents','install');p=Path(value['definition']);old=plistlib.loads(p.read_bytes())
        self.assertEqual(old['ProgramArguments'][0],str(Path(sys.executable).absolute()))
        self.assertEqual(old['StandardOutPath'],str(self.s.root/'数据/后台日志/runner.log'))
        old['ProgramArguments']=['old-python'];p.write_bytes(plistlib.dumps(old));service(self.s.root,self.home/'agents','install')
        self.assertNotEqual(plistlib.loads(p.read_bytes())['ProgramArguments'],['old-python'])
        old['PortableOwner']['root']='elsewhere';p.write_bytes(plistlib.dumps(old))
        with self.assertRaises(Blocked):service(self.s.root,self.home/'agents','install')
        backup=self.home/'external-volume';backup.mkdir();configure_local(self.s,{'backup_dir':str(backup)})
        with self.assertRaises(Blocked):configure_local(self.s,{'backup_dir':str(self.home/'没插的盘'/'备份')})  # 上一级不存在才拒绝
        made=configure_local(self.s,{'backup_dir':str(self.home/'new-backup')});self.assertTrue((self.home/'new-backup').is_dir())
        backup.chmod(0o555)
        try:
            with self.assertRaises(Blocked):configure_local(self.s,{'backup_dir':str(backup)})
        finally:backup.chmod(0o755)
        now=time.time();recovery=backup/'恢复区';recovery.mkdir();oldfile=recovery/'old';oldfile.write_text('keep');os.utime(oldfile,(now-31*86400,)*2)
        fresh=recovery/'fresh';fresh.write_text('fresh')
        for n in range(4):
            f=self.home/('.upgrade-'+str(n));f.mkdir();(f/'file').write_text(str(n));(f/'发行.json').write_text('{}');os.utime(f,(now-n*10,)*2)
        stranger=self.home/'.upgrade-别人的';stranger.mkdir();(stranger/'file').write_text('x');os.utime(stranger,(now-999,)*2)  # 没有 发行.json：不是本程序的，不动
        result=archive_residue(backup,now);self.assertEqual(len(result['删除']),3);self.assertEqual(result['移到归档'],[])  # 程序生成的残留到期直接删，不进归档
        self.assertFalse(oldfile.exists());self.assertTrue(fresh.exists());self.assertEqual(len(list(self.home.glob('.upgrade-*'))),3);self.assertTrue(stranger.exists())
        self.assertFalse((self.home/'归档').exists())
        (self.s.root/'陌生目录').mkdir();self.assertIn('陌生目录',self.s.doctor()['不认识的顶层目录'])
        self.repo.rename(self.home/'gone')
        new=self.home/'new';new.mkdir();g(new,'init','-q')
        from portable.move_in import scan;scan(self.s,[self.home])
        result=adopt(self.s,new,confirmed='搬 new')
        self.assertIn('商店 的目录找不到了',result['提醒'][0])

    def test_custom_name_board_and_weekly(self):
        import datetime
        text='---\n状态: 进行中\n等谁: 小周\n最后更新: 2026-11-01\n---\n'
        (self.repo/'STATUS.md').write_text(text)
        today=datetime.datetime(2026,11,10).timestamp()
        with patch('portable.settings.load',return_value={'owner_name':'OWNER（小周）'}):
            self.assertIn('商店',board.overview(self.s,today=today)['brief'])
            messages=board.weekly_check(self.s,today=today)
            self.assertTrue(any('等你 9 天' in m for m in messages))

    def test_non_git_omissions(self):
        folder=self.home/'plain';folder.mkdir()
        board.apply(self.s,board.load(self.s)+[{'id':'plain','name':'普通目录','path':str(folder)}])
        result=board.backup_external(self.s,self.home/'backup')
        item=next(p for p in result['projects'] if p['项目']=='普通目录')
        self.assertTrue(item['没备的']['非Git项目'])


class WindowTest(unittest.TestCase):
    def test_window_gives_starter_line_for_registered_project(self):
        import tempfile
        from portable.core import Store
        from portable import board,assist
        with tempfile.TemporaryDirectory() as d:
            d=Path(d).resolve();proj=d/'rakazo';proj.mkdir()
            s=Store(d/'root')
            try:
                board.apply(s,[{'id':'rakazo','name':'Rakazo 本机部署','path':str(proj),'status_path':str(proj/'STATUS.md')}])
                r=assist.window(s,'Rakazo 本机部署')
                self.assertIn(str(proj/'AGENTS.md'),r['启动语']);self.assertIn('新对话',r['项目窗口']);self.assertIn('不要贴在当前这个对话',r['项目窗口'])
                self.assertEqual(assist.window(s,'Rakazo')['位置'],str(proj))
                with self.assertRaisesRegex(Blocked,'已登记的有'):assist.window(s,'不存在')
            finally:s.db.close()

