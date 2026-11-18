import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from portable.core import Store, Blocked, canonical, digest
from portable import knowledge as k, workflows as w, session_adapters as sa, onboarding as ob
from portable.context_server import Context, enroll, serve

class Workflows(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.s=Store(self.root/'app');self.repo=self.root/'repo';self.repo.mkdir();self.s.register('合成项目',self.repo,'demo');self.s.put('selected_project','demo')
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def file(self,name,content):
        p=self.root/name
        if isinstance(content,bytes):p.write_bytes(content)
        else:p.write_text(content)
        return p
    def known(self):return dict(user='本人',problem='整理资料',flow='输入检索续接',scope='本地首版',acceptance='负例测试通过')
    def ready(self):
        rid=w.start(self.s,'demo','资料整理',self.known());w.answer(self.s,rid,'implement',True);return rid
    def test_real_text_html_docx_readers_and_bounds(self):
        for name,content,expected in [('a.md','# 方法\n条件','条件'),('a.html','<h1>方法</h1><script>evil()</script><p>条件</p>','条件')]:
            r=k.ingest(self.s,str(self.file(name,content)),['demo']);self.assertEqual(r['status'],'read');self.assertIn(expected,r['text']);self.assertNotIn('evil()',r['text'])
        p=self.root/'a.docx'
        with zipfile.ZipFile(p,'w') as z:z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:t>真实文档文字</w:t></w:p></w:document>')
        self.assertIn('真实文档文字',k.parse_file(p)['text'])
        bad=self.file('bad.docx',b'not a zip')
        with self.assertRaises(Blocked):k.parse_file(bad)
        with self.assertRaises(Blocked):k.parse_file(self.file('large.txt',b'x'*(k.MAX_BYTES+1)))
        link=self.root/'link.txt';link.symlink_to(self.root/'a.md')
        with self.assertRaises(Blocked):k.parse_file(link)
        with self.assertRaises(Blocked):k.parse_file(self.file('video.mp4',b'x'))
    def test_pdf_parser_executes_actual_bounded_backend(self):
        p=self.file('a.pdf',b'%PDF synthetic')
        with patch('portable.knowledge.shutil.which',return_value=None):
            with self.assertRaises(Blocked):k.parse_file(p)
        actual=self.file('simple.pdf',b'%PDF-1.4\n1 0 obj << /Length 44 >>\nstream\nBT /F1 12 Tf 72 720 Td (Real PDF text) Tj ET\nendstream\nendobj\n%%EOF')
        with patch('portable.knowledge.shutil.which',return_value=None):self.assertEqual(k.parse_file(actual)['text'],'Real PDF text')
        # Real subprocess fixture proves argv/path boundaries, not actual PDF capability.
        tool=self.file('pdf-reader','#!/bin/sh\nprintf "fixture extracted text"\n');tool.chmod(0o700)
        with patch('portable.knowledge.shutil.which',return_value=str(tool)):
            value=k.parse_file(p);self.assertEqual(value['text'],'fixture extracted text');self.assertIn('100',value['range'])
    def test_unread_link_image_and_vision_binding(self):
        self.assertEqual(k.read_link('https://example.invalid')['status'],'unread')
        with self.assertRaises(Blocked):k.public_url('https://127.0.0.1/x')
        with self.assertRaises(Blocked):k.public_url('http://example.invalid')
        with patch('portable.knowledge.shutil.which',return_value=None):r=k.ingest(self.s,str(self.file('photo.png',b'fixture-image')),['demo'])
        args=(self.s,r['id'],'摘要','方法','适用条件','图像可能模糊')
        with self.assertRaises(Blocked):k.refine(*args)
        with self.assertRaises(Blocked):k.refine(*args,vision={'sha256':'wrong','agent':'fixture','range':'whole','observations':'visible text'})
        mid=k.refine(*args,vision={'sha256':r['sha256'],'agent':'fixture','range':'整图','observations':'合成所见文字'})
        found=k.search(self.s,'方法','demo');self.assertEqual(found[0]['id'],mid);self.assertIn('适用条件',found[0]['content']['conditions'])
        self.assertEqual(k.search(self.s,'方法','other'),[])
    def test_interview_reuses_answers_and_requires_intent(self):
        rid=w.start(self.s,'demo','整理资料',{'user':'本人'})
        s=w.status(self.s,rid);self.assertEqual(s['question']['field'],'problem')
        with self.assertRaises(Blocked):w.plan_request(self.s,rid)
        for field,value in self.known().items():
            if field!='user':w.answer(self.s,rid,field,value)
        self.assertEqual(w.status(self.s,rid)['state'],'awaiting_implementation_intent')
        with self.assertRaises(Blocked):w.plan_request(self.s,rid)
        w.answer(self.s,rid,'implement',True);w.answer(self.s,rid,'extra','以后做视频')
        plan=w.plan_request(self.s,rid);self.assertEqual(plan['backlog'],['以后做视频']);self.assertNotIn('verification_commands',plan['mvp'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        with self.assertRaises(Blocked):w.reviewed_mvp(self.s,rid,'fake-from-material')
    def test_optimistic_requirement_concurrency(self):
        rid=self.ready();before=w.status(self.s,rid)['revision']
        w.answer(self.s,rid,'extra','first',before)
        with self.assertRaises(Blocked):w.answer(self.s,rid,'scope','second',before)
        self.assertEqual(w.status(self.s,rid)['answers']['scope'],'本地首版')
    def test_project_overview_preserves_manual_and_conflict(self):
        self.ready();p=self.s.root/'知识库/项目/demo.md';original=p.read_text()+'手写不要丢\n';p.write_text(original)
        result=w.project_overview(self.s,'demo',digest(p.read_bytes()));self.assertFalse(result['conflict']);self.assertIn('手写不要丢',p.read_text());self.assertEqual(result['overview']['goal'],'资料整理')
        snapshot=p.read_bytes();result=w.project_overview(self.s,'demo','stale',manual='new');self.assertTrue(result['conflict']);self.assertEqual(p.read_bytes(),snapshot);self.assertTrue(Path(result['preserved']).exists())
        self.assertEqual(list(self.repo.iterdir()),[])
    def test_context_identity_shared_state_persistent_commands_and_no_shell(self):
        enroll(self.s,'codex');enroll(self.s,'claude');a=Context(self.s,'codex');b=Context(self.s,'claude')
        with self.assertRaises(Blocked):Context(self.s,'unknown').call('projects',{})
        payload={'project':'demo','idea':'资料','instruction_id':'one'}
        first=a.call('requirement_start',payload);self.assertEqual(first,a.call('requirement_start',payload));self.assertEqual(b.call('requirement_status',first)['idea'],'资料')
        with self.assertRaises(Blocked):a.call('requirement_start',{**payload,'idea':'different'})
        state=b.call('requirement_status',first)
        b.call('requirement_answer',{'id':first['id'],'field':'user','value':'本人','expected':state['revision'],'instruction_id':'answer'})
        self.assertEqual(a.call('requirement_status',first)['answers']['user'],'本人')
        with self.assertRaises(Blocked):a.call('shell',{'command':'true'})
        with self.assertRaises(Blocked):a.call('note',{'project':'demo','summary':'a','caller':'old','instruction_id':'x'})
        task=a.call('task_reference',{'task':'foreign','project':'demo','origin_device':'old','origin_caller':'old','instruction_id':'foreign'})
        self.assertTrue(task['read_only'])
        note=a.call('note',{'project':'demo','summary':'ordinary note','instruction_id':'note'})
        self.assertFalse(note['execution']);self.assertEqual(self.s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
    def test_context_file_requires_selection_and_stdio_protocol(self):
        enroll(self.s,'fixture');c=Context(self.s,'fixture');f=self.file('fixture.txt','text')
        with self.assertRaises(Blocked):c.call('source_file',{'path':str(f),'project':'demo','instruction_id':'read'})
        self.s.put('context_source_files',[str(f.resolve())])
        r=c.call('source_file',{'path':str(f),'project':'demo','instruction_id':'read2'});self.assertEqual(r['status'],'read')
        stream=io.StringIO('\n'.join(canonical(x) for x in [{'jsonrpc':'2.0','id':1,'method':'initialize'},{'jsonrpc':'2.0','method':'notifications/initialized'},{'jsonrpc':'2.0','id':2,'method':'tools/list'}])+'\n');out=io.StringIO();serve(c,stream,out)
        rows=[json.loads(x) for x in out.getvalue().splitlines()];self.assertEqual(len(rows),2);self.assertTrue(rows[1]['result']['tools'])
    def test_native_session_adapters_incremental_summary_only(self):
        fixtures={'codex':[{'type':'session_meta','payload':{'id':'session'}},{'type':'response_item','payload':{'type':'message','role':'user','id':'u','content':[{'type':'input_text','text':'raw private fixture'}]}}], 'claude':[{'type':'user','sessionId':'session','uuid':'u','message':{'content':'raw private fixture'}}], 't3':[{'id':'u','role':'user','text':'raw private fixture'}]}
        for tool,rows in fixtures.items():
            p=self.file(tool+'.jsonl',''.join(canonical(x)+'\n' for x in rows));before=p.read_bytes()
            selection=sa.select(self.s,tool,p,'session')['selection'];batch=sa.read_batch(self.s,selection);self.assertEqual(batch['messages'][0]['text'],'raw private fixture')
            receipt=sa.commit(self.s,batch['batch'],'整理出的决定和方法');self.assertEqual(receipt,sa.commit(self.s,batch['batch'],'整理出的决定和方法'))
            self.assertEqual(sa.read_batch(self.s,selection)['bytes_read'],0);self.assertEqual(p.read_bytes(),before)
            with p.open('a') as f:f.write(canonical(rows[-1])+'\n')
            new=sa.read_batch(self.s,selection);self.assertEqual(len(new['messages']),0);self.assertLess(new['bytes_read'],len(p.read_bytes())) if tool=='codex' else None
            self.assertNotIn('raw private fixture',(self.s.root/receipt['archive']).read_text())
        self.assertFalse(any('raw private fixture' in p.read_text() for p in (self.s.root/'知识库').rglob('*.md')))
    def test_session_corruption_partial_and_native_identity(self):
        p=self.file('s.jsonl',canonical({'id':'1','role':'user','text':'one'})+'\n')
        selection=sa.select(self.s,'t3',p,'s')['selection'];batch=sa.read_batch(self.s,selection);sa.commit(self.s,batch['batch'],'summary')
        with p.open('a') as f:f.write('{"partial":')
        self.assertEqual(sa.read_batch(self.s,selection)['bytes_read'],0)
        p.write_text('replaced\n')
        with self.assertRaises(Blocked):sa.read_batch(self.s,selection)
        bad=self.file('c.jsonl',canonical({'type':'session_meta','payload':{'id':'wrong'}})+'\n');sel=sa.select(self.s,'codex',bad,'selected')['selection']
        with self.assertRaises(Blocked):sa.read_batch(self.s,sel)
    def test_preferences_resume_and_revocation(self):
        self.assertEqual(ob.interview(self.s)['question']['field'],'name')
        ob.preference(self.s,'name','小周');self.assertEqual(ob.interview(self.s)['question']['field'],'tools')
        ob.preference(self.s,'tools','T3',persistent=False);self.assertNotIn('tools',self.s.get('onboarding'))
        with self.assertRaises(Blocked):ob.preference(self.s,'important_rules','rule')
        ob.preference(self.s,'important_rules','rule',confirmed=True);ob.preference(self.s,'name',None)
        self.assertNotIn('name',self.s.get('onboarding'));self.assertEqual(ob.interview(self.s)['defaults']['notifications'],'off')
    def test_actual_stdio_process_and_configuration_generation(self):
        cfg=ob.configure_context(self.s,'fixture');self.assertFalse(cfg['global_config_modified']);self.assertFalse(cfg['loaded'])
        self.assertTrue(ob.check_clients(self.s,'fixture')['stdio'])
        with self.assertRaises(Blocked):ob.configure_context(self.s,'../../escape')
        env=ob.isolated_environment(self.root);self.assertNotIn('ANTHROPIC_API_KEY',env);self.assertNotIn('OPENAI_API_KEY',env)
    def test_standalone_cli_is_runnable(self):
        r=subprocess.run([sys.executable,'-m','portable.workflows','--root',str(self.s.root),'workflow','start','demo','CLI idea'],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr);self.assertTrue(json.loads(r.stdout)['id'])


class PrivacyAndIntegration(unittest.TestCase):
    setUp=Workflows.setUp
    tearDown=Workflows.tearDown
    file=Workflows.file

    def test_shared_privacy_at_material_checkpoint_session_output_boundaries(self):
        from portable.privacy import redact_text,sanitize,REDACTED
        secret='sk-proj-'+'SYNTHETICONLY0123456789'*2
        private='-----BEGIN PRIVATE KEY-----\nSYNTHETIC-NOT-A-REAL-KEY\n-----END PRIVATE KEY-----'
        text='方法：先排障再验证。 api_key="'+secret+'" '+private+' token=synthetic_login_token_12345'
        cleaned=redact_text(text)
        self.assertNotIn(secret,cleaned);self.assertNotIn('synthetic_login_token_12345',cleaned);self.assertNotIn('SYNTHETIC-NOT-A-REAL-KEY',cleaned)
        self.assertIn('方法：先排障再验证',cleaned)
        self.assertEqual(redact_text('API_KEY=YOUR_API_KEY'), 'API_KEY=YOUR_API_KEY')
        self.assertEqual(sanitize({'password':'synthetic_secret'})['password'],REDACTED)
        source=k.ingest(self.s,'provided fixture',['demo'],text=text)
        mid=k.refine(self.s,source['id'],'排障方法','检查错误再验证 '+secret,'遇到错误','需要核对环境')
        self.assertNotIn(secret,canonical(k.search(self.s,'应该如何排障','demo')))
        note=(self.s.root/'知识库/素材'/ (mid+'.md')).read_text();self.assertIn('## 方法',note);self.assertNotIn(secret,note)
        self.assertTrue(source['redacted'])
        rows=self.s.db.execute('SELECT body FROM records').fetchall();self.assertNotIn(secret,canonical([r[0] for r in rows]))
        enroll(self.s,'fixture');c=Context(self.s,'fixture')
        payload={field:[] for field in ('goal','decisions','done','remaining','authorization','evidence')};payload['goal']=text;payload['instruction_id']='checkpoint'
        c.call('checkpoint',payload)
        self.assertNotIn(secret,canonical(c.call('checkpoints',{})))
        for p in (self.s.root/'任务记录').glob('*.json'):self.assertNotIn(secret,p.read_text())

    def test_public_link_html_redirect_bound_and_private_ip(self):
        from email.message import Message
        from unittest.mock import MagicMock
        headers=Message();headers['Content-Type']='text/html; charset=utf-8'
        connection=MagicMock();response=connection.getresponse.return_value;response.status=200;response.headers=headers;response.read.return_value=b'<h1>Method</h1><script>bad()</script><p>conditions</p>'
        with patch('portable.knowledge.public_url',return_value='https://example.invalid'),patch('portable.knowledge.PublicHTTPSConnection',return_value=connection):
            r=k.read_link('https://example.invalid',True);self.assertEqual(r['status'],'read');self.assertIn('Method',r['text']);self.assertNotIn('bad()',r['text'])
            response.status=302;self.assertEqual(k.read_link('https://example.invalid',True)['status'],'unread')
            response.status=200;response.read.return_value=b'x'*(k.MAX_BYTES+1)
            with self.assertRaises(Blocked):k.read_link('https://example.invalid',True)
        with patch('portable.knowledge.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(Blocked):k.PublicHTTPSConnection('example.invalid').connect()

    def test_mcp_session_read_does_not_persist_original_messages_in_idempotency_results(self):
        p=self.file('selected.jsonl',canonical({'id':'one','role':'user','text':'raw session text stays ephemeral'})+'\n')
        selected=sa.select(self.s,'t3',p,'session')['selection'];enroll(self.s,'fixture');c=Context(self.s,'fixture')
        batch=c.call('session_read',{'selection':selected})
        self.assertEqual(batch['messages'][0]['text'],'raw session text stays ephemeral')
        values=[r[0] for r in self.s.db.execute('SELECT value FROM meta')]
        self.assertNotIn('raw session text stays ephemeral',canonical(values))
        c.call('session_commit',{'batch':batch['batch'],'summary':'精炼决定','instruction_id':'export'})
        self.assertEqual(c.call('session_read',{'selection':selected})['messages'],[])

    def test_existing_profile_reused_and_revocation_does_not_restore_legacy(self):
        self.s.put('profile',{'user':'本人','tools':'T3','goals':'整理资料','notifications':'飞书/企微，未启用'})
        self.assertEqual(ob.interview(self.s)['preferences']['tools'],'T3')
        self.assertTrue(ob.interview(self.s)['complete'])
        ob.preference(self.s,'tools',None)
        self.assertNotIn('tools',ob.interview(self.s)['preferences'])
        self.assertEqual(ob.interview(self.s)['question']['field'],'tools')
        ob.preference(self.s,'tools','Claude');self.assertEqual(ob.interview(self.s)['preferences']['tools'],'Claude')

    def test_engineer_review_is_bound_to_real_fixture_repo_and_current_mvp(self):
        from portable.team import bind_new_tasks
        from portable.verification import review
        server=self.file('synthetic-server.py','# fixture only')
        self.s.put('team_config',{'python':sys.executable,'server':str(server),'caller':'portable:'+self.s.device})
        def backend(tool,args):
            return {'tools':['create_task','get_task','get_task_result','lookup_request','list_tasks']} if tool=='__capabilities__' else {'ok':True,'tasks':[]}
        bind_new_tasks(self.s,backend,'demo')
        known=Workflows.known(self);rid=w.start(self.s,'demo','工程计划',known);w.answer(self.s,rid,'implement',True)
        review_id=review(self.s,known,['python3 -m unittest discover -s tests'])
        self.assertTrue(w.reviewed_mvp(self.s,rid,review_id)['dispatch_ready'])
        w.answer(self.s,rid,'acceptance','changed')
        with self.assertRaises(Blocked):w.reviewed_mvp(self.s,rid,review_id)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_release_zip_context_remains_runnable_without_source(self):
        from portable.distribution import build,release_verify
        source=Path(__file__).resolve().parents[1];release=self.root/'release'
        build(source,release);release_verify(release)
        code="import sys; sys.path.insert(0,sys.argv[1]); from portable.core import Store; from portable.onboarding import check_clients; s=Store(sys.argv[2]); print(check_clients(s,'release-fixture')['stdio']); s.db.close()"
        r=subprocess.run([sys.executable,'-I','-B','-c',code,str(release/'工具/portable.pyz'),str(release)],cwd=self.root,capture_output=True,text=True,timeout=20)
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout.strip(),'True')
        self.assertFalse((release/'portable').exists())

    def test_cross_tool_local_task_context_foreign_task_cannot_be_relabelled(self):
        # Ownership now comes from the backend's real task-view shape, not outbox claims.
        backend=lambda tool,args: {'task_id':args['task_id'],'caller':'portable:'+self.s.device if args['task_id']=='local' else 'external:history','status':'completed'}
        enroll(self.s,'codex');enroll(self.s,'claude');a=Context(self.s,'codex',backend);b=Context(self.s,'claude',backend)
        with self.s.db:self.s.db.execute('INSERT INTO outbox VALUES (?,?,?,?)',('fixture-task',canonical({'device':self.s.device,'project':'demo','mvp':{'scope':'fixture'}}),'terminal','local'))
        ref=a.call('task_reference',{'task':'local','project':'demo','origin_device':self.s.device,'origin_caller':'portable:'+self.s.device,'instruction_id':'ref'})
        self.assertFalse(ref['read_only']);self.assertFalse(b.call('task_update',{'reference':ref['id'],'summary':'追加上下文','instruction_id':'edit'})['backend_modified'])
        row=self.s.db.execute('SELECT body FROM records WHERE id=?',(ref['id'],)).fetchone();stale=json.loads(row[0]);stale['origin_device']='prior-device'
        with self.s.db:self.s.db.execute('UPDATE records SET body=? WHERE id=?',(canonical(stale),ref['id']))
        with self.assertRaises(Blocked):b.call('task_update',{'reference':ref['id'],'summary':'旧设备记录不能接任','instruction_id':'old-device'})
        foreign=a.call('task_reference',{'task':'historical','project':'demo','origin_device':self.s.device,'origin_caller':'portable:'+self.s.device,'instruction_id':'foreign'})
        self.assertTrue(foreign['read_only'])
        with self.assertRaises(Blocked):b.call('task_update',{'reference':foreign['id'],'summary':'冒用','instruction_id':'reject'})

    def test_pdf_compression_and_unsupported_encoding_fail_closed(self):
        import zlib
        stream=zlib.compress(b'BT [(Hello) -20 ( world)] TJ ET')
        pdf=b'%PDF-1.4\n1 0 obj << /Filter /FlateDecode >>\nstream\n'+stream+b'\nendstream\nendobj\n%%EOF'
        self.assertEqual(k.pdf_text(pdf),'Hello world')
        with self.assertRaises(Blocked):k.pdf_text(pdf.replace(b'/FlateDecode',b'/ASCII85Decode'))
        with self.assertRaises(Blocked):k.pdf_text(pdf+b'/Encrypt')
        with self.assertRaises(Blocked):k.pdf_text(pdf+b'/ToUnicode')


if __name__=='__main__':unittest.main()
