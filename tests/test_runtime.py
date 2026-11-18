import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from portable.core import Store,Blocked,canonical
from portable.distribution import build,atomic_upgrade,release_verify,prepare_update
from portable.team import transport

SOURCE=Path(__file__).resolve().parents[1]
SDK=Path(os.environ.get('PORTABLE_SDK_PYTHON','/nonexistent'))
SERVER=Path(os.environ.get('PORTABLE_MCP_SERVER','/nonexistent'))

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
    def tearDown(self):self.tmp.cleanup()
    def test_first_launch_is_unbound_but_copy_still_rotates(self):
        release=self.root/'release';build(SOURCE,release)
        self.assertFalse((release/'数据/state.sqlite').exists())
        s=Store(release);device=s.device
        self.assertEqual(s.get('execution'),'unbound');s.db.close()
        copied=self.root/'copied';shutil.copytree(release,copied)
        s=Store(copied)
        try:self.assertNotEqual(device,s.device);self.assertEqual(s.get('execution'),'copied-readonly')
        finally:s.db.close()
    def test_zipapp_propagates_blocked_exit_status(self):
        release=self.root/'release';build(SOURCE,release);entry=release/'开始使用.command'
        result=subprocess.run([str(entry),'doctor'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0)
        result=subprocess.run([str(entry),'submit','missing-request',str(self.root)],capture_output=True,text=True)
        self.assertEqual(result.returncode,2);self.assertEqual(json.loads(result.stderr)['status'],'BLOCKED')
    def test_real_default_entry_without_sdk_uses_configured_sdk(self):
        if not SDK.is_file() or not SERVER.is_file():self.skipTest('EXTERNAL_REQUIRED: isolated SDK/candidate unavailable')
        release=self.root/'release';build(SOURCE,release)
        binpath=self.root/'bin';binpath.mkdir()
        wrapper=binpath/'python3';wrapper.write_text('#!/bin/bash\nexec '+shutil.which('python3')+' -S "$@"\n');wrapper.chmod(0o755)
        env={**os.environ,'PATH':str(binpath)+os.pathsep+os.environ['PATH'],'PYTHONPATH':''}
        absent=subprocess.run([str(wrapper),'-c','import mcp'],capture_output=True)
        self.assertNotEqual(absent.returncode,0)
        s=Store(release);workspace=self.root/'workspace';workspace.mkdir();s.register('demo',workspace,'demo')
        state=self.root/'state';state.mkdir();(state/'tasks').mkdir()
        cfgpath=self.root/'server-config.json';cfgpath.write_text(canonical({'allowed_workspace_roots':[str(workspace)],'limits':{}}))
        s.put('team_config',{'python':str(SDK),'server':str(SERVER),'caller':'portable:'+s.device,'environment':{'DISPATCH_STATE_ROOT':str(state),'AI_TEAM_MCP_STATE':str(self.root/'mcp'),'AI_TEAM_MCP_CONFIG':str(cfgpath)}})
        before=s.device;s.db.close()
        result=subprocess.run([str(release/'开始使用.command'),'bind-new-tasks','demo'],env=env,cwd=self.root,capture_output=True,text=True,timeout=40)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['bound'])
        s=Store(release)
        try:self.assertEqual(before,s.device);self.assertEqual(s.get('execution'),'active')
        finally:s.db.close()
        self.assertFalse(list((state/'tasks').glob('*.json')))
    def test_bridge_timeout_and_stderr_are_sanitized_no_retry(self):
        cfg={'python':sys.executable,'server':'fixture','caller':'portable:fixture'}
        with patch('portable.team.subprocess.run',side_effect=subprocess.TimeoutExpired('private',90)) as run:
            with self.assertRaises(Blocked) as error:transport(cfg)('create_task',{'spec':'SYNTHETIC_PRIVATE'})
            self.assertEqual(run.call_count,1);self.assertNotIn('SYNTHETIC_PRIVATE',str(error.exception))
            self.assertNotIn('SYNTHETIC_PRIVATE',' '.join(run.call_args.args[0]))
        result=subprocess.CompletedProcess([],2,'{"ok":false}','SYNTHETIC_PRIVATE')
        with patch('portable.team.subprocess.run',return_value=result):
            with self.assertRaises(Blocked) as error:transport(cfg)('get_task',{})
            self.assertNotIn('SYNTHETIC_PRIVATE',str(error.exception))
    def test_prepare_update_checks_remote_tasks_without_stopping_them(self):
        s=Store(self.root/'app');s.put('execution','active');s.put('team_config',{'fixture':True})
        try:
            with patch('portable.team.transport',return_value=lambda *args:{'tasks':[{'status':'running'}]}):
                with self.assertRaises(Blocked):prepare_update(s)
            self.assertEqual(s.get('execution'),'update-paused')
            with patch('portable.team.transport',return_value=lambda *args:{'tasks':[],'next_offset':None}):
                self.assertFalse(prepare_update(s)['services_stopped'])
        finally:s.db.close()
    def test_old_snapshot_schema_remains_readable(self):
        from portable import migration as m
        from portable.core import atomic,digest
        s=Store(self.root/'app');medium=self.root/'medium';medium.mkdir();m.enroll(s,medium)
        source=self.root/'source';source.mkdir();(source/'a').write_text('keep')
        try:
            snapshot=Path(m.snapshot(s,source,medium))
            manifest=json.loads((snapshot/'manifest.json').read_text());manifest['version']='0.1.0'
            atomic(snapshot/'manifest.json',canonical(manifest));atomic(snapshot/'COMPLETE',digest((snapshot/'manifest.json').read_bytes()))
            m.restore(snapshot,self.root/'restore')
            self.assertEqual((self.root/'restore/a').read_text(),'keep')
        finally:s.db.close()
    def test_atomic_upgrade_preserves_assets_and_identity_and_rolls_back(self):
        old=self.root/'installed';new=self.root/'candidate';build(SOURCE,old);build(SOURCE,new)
        manifest=json.loads((old/'发行.json').read_text());manifest['version']='0.1.0';(old/'发行.json').write_text(canonical(manifest))
        s=Store(old);device=s.device;s.put('selected_project','keep')
        (old/'知识库/手写.md').write_text('owner text');(old/'代码/keep').write_text('personal source');s.db.close()
        result=atomic_upgrade(old,new,self.root/'backups');self.assertEqual(result['phase'],'applied')
        self.assertEqual(release_verify(old)['version'],__import__('portable.core',fromlist=['VERSION']).VERSION)
        self.assertEqual((old/'知识库/手写.md').read_text(),'owner text');self.assertEqual((old/'代码/keep').read_text(),'personal source')
        s=Store(old)
        try:self.assertEqual(s.device,device);self.assertEqual(s.get('selected_project'),'keep')
        finally:s.db.close()
        before=(old/'工具/portable.pyz').read_bytes()
        with patch('portable.core.Store.doctor',side_effect=Blocked('fixture readback failure')):
            with self.assertRaises(Blocked):atomic_upgrade(old,new,self.root/'backup2')
        self.assertEqual((old/'工具/portable.pyz').read_bytes(),before)
        self.assertEqual((old/'知识库/手写.md').read_text(),'owner text')
