import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from portable import board,settings,upgrade_channel
from portable.core import Store,Blocked


class RegistrationSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.s=Store(self.root/'包')
        self.addCleanup(self.s.db.close)
        self.project=self.root/'新项目';self.project.mkdir()

    def register_cli(self):
        out=subprocess.run([sys.executable,'-m','portable','--root',str(self.s.root),
                            'register','新项目',str(self.project),'--id','new'],
                           cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
        self.assertEqual(out.returncode,0,out.stderr)
        return json.loads(out.stdout)

    def test_cli_register_adds_project_to_enabled_settings(self):
        old=self.root/'已有项目';old.mkdir()
        existing={'id':'old','name':'原名字','path':str(old),'status_path':str(old/'自定义进度.md')}
        settings.save(self.s,{'projects':[existing],'owner_name':'测试用户'})
        result=self.register_cli()
        self.assertEqual(result,{'id':'new','已写进本机设置':['新项目']})
        self.assertEqual(settings.load(self.s)['projects'],[existing,{
            'id':'new','name':'新项目','path':str(self.project),
            'status_path':str(self.project/'STATUS.md'),
            'features_path':str(self.project/'功能清单.md'),
            'prd_path':str(self.project/'docs/PRD.md')}])
        self.assertEqual(settings.load(self.s)['owner_name'],'测试用户')
        self.assertTrue((self.s.root/settings.RULES).is_file())

    def test_cli_register_does_not_enable_none_list(self):
        settings.save(self.s,{'projects':None})
        before=settings.path(self.s).read_bytes()
        self.assertEqual(self.register_cli(),{'id':'new'})
        self.assertIsNone(settings.load(self.s)['projects'])
        self.assertEqual(settings.path(self.s).read_bytes(),before)
        self.assertEqual(self.s.db.execute('SELECT id FROM projects').fetchone()['id'],'new')

    def test_cli_register_does_not_create_settings_file(self):
        self.assertEqual(self.register_cli(),{'id':'new'})
        self.assertFalse(settings.path(self.s).exists())
        self.assertFalse((self.s.root/settings.RULES).exists())

    def test_keep_local_then_apply_preserves_registry_and_database_projects(self):
        existing=self.root/'原项目';existing.mkdir()
        board.apply(self.s,[{'id':'existing','name':'原项目','path':str(existing),
                            'status_path':str(existing/'自定义进度.md')}])
        settings.save(self.s,{'projects':[]})
        self.s.register('新项目',str(self.project),'new')
        with self.assertRaises(Blocked):board.apply(self.s,None)
        self.assertEqual(settings.keep_local_registrations(self.s),['原项目','新项目'])
        result=board.apply(self.s,None)
        self.assertEqual(result['registered'],['existing','new'])
        self.assertEqual(result['removed_from_registry'],[])
        self.assertEqual(board.load(self.s)[0]['status_path'],str(existing/'自定义进度.md'))
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM projects').fetchone()[0],2)

    def test_existing_entries_are_unchanged_and_not_saved_again(self):
        existing={'id':'new','name':'保留名字','path':str(self.project),
                  'status_path':str(self.project/'自定义进度.md')}
        settings.save(self.s,{'projects':[existing],'extra_rules':['保留规则']})
        self.s.register('数据库名字',str(self.project),'new')
        before=settings.path(self.s).read_bytes()
        with patch('portable.settings.save',wraps=settings.save) as save:
            self.assertEqual(settings.keep_local_registrations(self.s),[])
            self.assertEqual(self.register_cli(),{'id':'new'})
            save.assert_not_called()
        self.assertEqual(settings.path(self.s).read_bytes(),before)
        self.assertEqual(settings.load(self.s)['projects'],[existing])

    def test_restore_keeps_database_only_project_before_registry_alignment(self):
        settings.save(self.s,{'projects':[]})
        self.s.register('新项目',str(self.project),'new')
        done,failed=upgrade_channel._restore(self.s.root,{},[],sys.executable)
        self.assertEqual(failed,[])
        self.assertEqual(done,['新登记的项目已写进本机设置：新项目',
                               '项目登记已对齐本机设置','本机规则已重新生成'])
        self.assertEqual([e['id'] for e in settings.load(self.s)['projects']],['new'])
        self.assertEqual([e['id'] for e in board.load(self.s)],['new'])
        again,failed=upgrade_channel._restore(self.s.root,{},[],sys.executable)
        self.assertEqual(failed,[])
        self.assertEqual(again,['项目登记已对齐本机设置','本机规则已重新生成'])

    def test_restore_reports_settings_save_failure_and_keeps_registry_guard(self):
        settings.save(self.s,{'projects':[]})
        self.s.register('新项目',str(self.project),'new')
        with patch('portable.settings.save',side_effect=Blocked('模拟写入失败')):
            done,failed=upgrade_channel._restore(self.s.root,{},[],sys.executable)
        self.assertEqual(done,['本机规则已重新生成'])
        self.assertEqual(len(failed),2)
        self.assertIn('模拟写入失败',failed[0])
        self.assertIn('以下项目只在本机登记',failed[1])
        self.assertEqual(settings.load(self.s)['projects'],[])
        self.assertEqual(self.s.db.execute('SELECT id FROM projects').fetchone()['id'],'new')


if __name__=='__main__':unittest.main()
