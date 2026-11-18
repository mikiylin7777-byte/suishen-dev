"""Independent user journeys for the four explicitly requested missing flows."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch,MagicMock
import zipfile
from portable.core import Store,Blocked,canonical,digest
from portable.context_server import Context,enroll,serve
from portable import knowledge as k,material_assets as assets,session_adapters as sessions,workflows as w

class GapJourneys(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.s=Store(self.root/'app')
        enroll(self.s,'codex');enroll(self.s,'claude');self.a=Context(self.s,'codex');self.b=Context(self.s,'claude')
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def register_repo(self):
        repo=self.root/'real-fixture-repo';repo.mkdir();(repo/'README.md').write_text('Synthetic local notes project\n')
        def git(*args):subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)
        git('init','-q');git('add','README.md');git('-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
        self.s.register('真实合成repo',repo,'demo');return repo
    def ready(self):
        from portable.team import bind_new_tasks
        import sys
        repo=self.register_repo();server=self.root/'synthetic-server.py';server.write_text('# synthetic backend adapter')
        self.s.put('team_config',{'python':sys.executable,'server':str(server),'caller':'portable:'+self.s.device})
        def backend(tool,args):
            if tool=='__capabilities__':return {'tools':['create_task','get_task','get_task_result','lookup_request','list_tasks']}
            if tool=='list_tasks':return {'ok':True,'tasks':[]}
            if tool=='create_task':self.created.append(args);return {'task_id':'fixture-task','caller':'portable:'+self.s.device,'status':'queued'}
            if tool=='get_task':return {'task_id':args['task_id'],'caller':'portable:'+self.s.device,'status':'queued'}
            raise AssertionError(tool)
        self.created=[];self.backend=backend;bind_new_tasks(self.s,backend,'demo');self.a=Context(self.s,'codex',backend);self.b=Context(self.s,'claude',backend);return repo
    def call(self,context,name,args):
        # Actual JSON-RPC wire path: no shell operations or verification CLI.
        req={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}}
        out=io.StringIO();serve(context,io.StringIO(canonical(req)+'\n'),out);response=json.loads(out.getvalue())
        if 'error' in response:raise Blocked(response['error']['message'])
        return json.loads(response['result']['content'][0]['text'])
    def test_original_deleted_source_still_carries_and_recovers_with_portable_links(self):
        self.register_repo();p=self.root/'source.md';p.write_text('排障方法：先核对证据，再复现错误。')
        r=k.ingest(self.s,str(p),['demo']);mid=k.refine(self.s,r['id'],'排障方法','先核对证据','出现错误','依环境判断');p.unlink()
        original=assets.original_path(self.s,r['asset']);self.assertIn('先核对证据',original.read_text())
        note=(self.s.root/'知识库/素材'/(mid+'.md')).read_text();self.assertIn('../../数据/素材原件/',note);self.assertNotIn(str(p),note.split('[携带原件]')[1].split('\n')[0])
        value=assets.load(self.s,r['asset']);assets.retention(self.s,now=value['created']+100*assets.DAY);self.assertEqual(assets.load(self.s,r['asset'])['state'],'active')
        temp=k.ingest(self.s,'临时文字',text='临时收藏外的资料');aid=temp['asset'];created=assets.load(self.s,aid)['created']
        moved=assets.retention(self.s,now=created+31*assets.DAY);self.assertFalse(moved['permanently_deleted']);self.assertEqual(assets.load(self.s,aid)['state'],'recovery')
        assets.retention(self.s,now=created+62*assets.DAY);self.assertTrue(assets.load(self.s,aid)['owner_delete_eligible']);self.assertTrue(assets.original_path(self.s,aid).is_file())
        assets.restore(self.s,aid,now=created+63*assets.DAY);self.assertEqual(assets.load(self.s,aid)['state'],'active');self.assertEqual(assets.original_path(self.s,aid).read_text(),'临时收藏外的资料')
        k.associate(self.s,temp['id'],favorite=True);assets.retention(self.s,now=created+500*assets.DAY);self.assertEqual(assets.load(self.s,aid)['retention'],'long_term')
        import shutil
        self.s.db.execute('PRAGMA wal_checkpoint(FULL)');copied=self.root/'carried-copy';shutil.copytree(self.s.root,copied)
        carried=Store(copied)
        try:
            self.assertEqual(assets.original_path(carried,r['asset']).read_text(),'排障方法：先核对证据，再复现错误。')
            self.assertTrue(k.search(carried,'排障','demo'))
        finally:carried.db.close()
    def test_due_recovery_is_automatic_on_asset_listing_and_keeps_note_links_valid(self):
        source=k.ingest(self.s,'临时灵感',text='可以恢复的普通方法')
        mid=k.refine(self.s,source['id'],'普通方法','先记录来源','灵感阶段','等待确认')
        asset=assets.load(self.s,source['asset']);page=self.s.root/'知识库/素材'/(mid+'.md')
        with patch('portable.material_assets.time.time',return_value=asset['created']+31*assets.DAY):
            listing=self.a.call('assets',{})
        current=next(x for x in listing if x['id']==asset['id']);self.assertEqual(current['state'],'recovery')
        self.assertIn('../../数据/素材原件/恢复区/',page.read_text())
        self.a.call('asset_restore',{'asset':asset['id'],'instruction_id':'restore'})
        self.assertIn('../../数据/素材原件/内容/',page.read_text())
        path=assets.original_path(self.s,asset['id']);path.write_bytes(b'changed bytes')
        with self.assertRaises(Blocked):assets.restore(self.s,asset['id'])

    def test_sensitive_and_unread_screenshot_originals_are_isolated_not_fake_redacted_originals(self):
        secret='sk-proj-'+'SYNTHETICONLY1234567890'*2;p=self.root/'private.txt';p.write_text('api_key='+secret)
        r=k.ingest(self.s,str(p),favorite=True);p.unlink();value=assets.load(self.s,r['asset'])
        self.assertTrue(value['private']);self.assertTrue(r['original_complete']);self.assertFalse(value['encrypted'])
        with self.assertRaises(Blocked):assets.original_path(self.s,r['asset'])
        raw=assets.original_path(self.s,r['asset'],local_private=True);self.assertEqual(raw.stat().st_mode&0o777,0o600);self.assertIn(secret,raw.read_text());self.assertNotIn(secret,canonical(r))
        picture=self.root/'selected.png';picture.write_bytes(b'synthetic PNG source, not a real photo')
        with patch('portable.knowledge.shutil.which',return_value=None):image=k.ingest(self.s,str(picture))
        picture.unlink();self.assertEqual(image['status'],'unread');self.assertTrue(assets.load(self.s,image['asset'])['private']);self.assertTrue(assets.original_path(self.s,image['asset'],local_private=True).exists())
    def test_public_response_original_snapshot_survives_network_source_disappearance(self):
        from email.message import Message
        headers=Message();headers['Content-Type']='text/html; charset=utf-8';connection=MagicMock();r=connection.getresponse.return_value;r.status=200;r.headers=headers;r.read.return_value=b'<h1>Captured method</h1><p>Selected public response.</p>'
        with patch('portable.knowledge.public_url',return_value='https://example.invalid'),patch('portable.knowledge.PublicHTTPSConnection',return_value=connection):
            value=self.a.call('source_link',{'url':'https://example.invalid','favorite':True,'instruction_id':'link'})
        self.assertEqual(assets.original_path(self.s,value['asset']).read_bytes(),r.read.return_value);self.assertIn('bounded-response',value['original_scope'])
    def test_generic_material_to_unbound_new_project_idea_then_real_project_association(self):
        r=self.call(self.a,'source_text',{'source':'灵感','text':'资料整理方法：先明确目标，再建立索引。','instruction_id':'idea'})
        mid=self.call(self.a,'refine',{'reading':r['id'],'summary':'资料整理方法','method':'先明确目标','conditions':'资料分散时','uncertainties':'取决于规模','instruction_id':'refine'})['id']
        self.assertTrue(k.search(self.s,'资料如何整理'));self.assertEqual(self.s.db.execute('SELECT count(*) FROM projects').fetchone()[0],0)
        req=self.call(self.b,'material_requirement',{'reading':r['id'],'instruction_id':'new-project-idea'});self.assertEqual(req['state'],'awaiting_project')
        with self.assertRaises(Blocked):self.call(self.b,'plan',{'id':req['id'],'instruction_id':'no-fake-repo'})
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        self.register_repo();linked=self.call(self.b,'requirement_project',{'id':req['id'],'project':'demo','expected':req['revision'],'instruction_id':'real-project'})
        self.assertEqual(linked['project'],'demo');self.assertFalse(linked['intent'])
        self.call(self.a,'material_associate',{'reading':r['id'],'project':'demo','instruction_id':'associate'})
        found=k.search(self.s,'整理','demo');self.assertTrue(any(x['id']==mid for x in found));self.assertEqual(assets.load(self.s,r['asset'])['retention'],'long_term')
    def complete_interview(self):
        req=self.call(self.a,'requirement_start',{'project':'demo','idea':'整理需求','instruction_id':'start'})['id']
        for field,value in dict(user='本人',problem='整理需求',flow='输入再检索',scope='本地索引',acceptance='隔离负例通过',implement=True).items():
            revision=self.call(self.b,'requirement_status',{'id':req})['revision'];self.call(self.b,'requirement_answer',{'id':req,'field':field,'value':value,'expected':revision,'instruction_id':'answer-'+field})
        return req
    def test_mcp_only_interview_real_repo_review_original_intent_existing_team_submit(self):
        self.ready();rid=self.complete_interview()
        inspected=self.call(self.a,'engineering_context',{'id':rid,'files':['README.md'],'instruction_id':'inspect'})
        self.assertIn('Synthetic',inspected['content']['README.md'])
        planned=self.call(self.b,'engineering_review',{'id':rid,'inspection':inspected['inspection'],'revision':inspected['revision'],'plan':'在已读repo中实现本地索引并运行隔离测试','commands':['python3 -m unittest discover -s tests'],'confirmed':True,'instruction_id':'engineer-review'})
        self.assertEqual(len(self.created),0);self.assertEqual(self.s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
        out=self.call(self.a,'request_submit',{'request':planned['request'],'instruction_id':'submit'});self.assertEqual(out['team']['task_id'],'fixture-task')
        again=self.call(self.b,'request_submit',{'request':planned['request'],'instruction_id':'resume-same-request'});self.assertEqual(again['team']['task_id'],'fixture-task');self.assertEqual(len(self.created),1)
        self.assertEqual(self.created[0]['acceptance_criteria'],['python3 -m unittest discover -s tests'])
        # Public caller ownership comes from get_task, even if supplied legacy fields lie.
        ref=self.call(self.a,'task_reference',{'task':'fixture-task','project':'demo','origin_caller':'wrong','origin_device':'wrong','instruction_id':'reference'})
        self.assertFalse(ref['read_only']);self.call(self.b,'task_update',{'reference':ref['id'],'summary':'共享上下文','instruction_id':'update'})
    def test_engineering_stale_repo_mvp_binding_unknown_agent_and_material_cannot_authorize(self):
        repo=self.ready();rid=self.complete_interview();inspected=self.a.call('engineering_context',{'id':rid,'files':['README.md'],'instruction_id':'inspect'})
        args={'id':rid,'inspection':inspected['inspection'],'revision':inspected['revision'],'plan':'真实工程计划','commands':['python3 -m unittest'],'confirmed':True,'instruction_id':'review'}
        with self.assertRaises(Blocked):Context(self.s,'unknown').call('engineering_review',args)
        with self.assertRaises(Blocked):self.a.call('engineering_review',{**args,'inspection':'ordinary-material','instruction_id':'material'})
        (repo/'README.md').write_text('changed without committing')
        with self.assertRaises(Blocked):self.a.call('engineering_review',args)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        fresh=self.a.call('engineering_context',{'id':rid,'files':['README.md'],'instruction_id':'fresh'})
        planned=self.b.call('engineering_review',{**args,'inspection':fresh['inspection'],'instruction_id':'new-review'})
        current=w.status(self.s,rid);w.answer(self.s,rid,'scope','changed goal',current['revision'])
        with self.assertRaises(Blocked):self.a.call('request_submit',{'request':planned['request'],'instruction_id':'stale-submit'})
        self.assertEqual(len(self.created),0)
    def test_authoritative_foreign_or_unavailable_owner_stays_read_only_despite_local_claim(self):
        self.register_repo()
        for case,backend in [('foreign',lambda t,a:{'task_id':a['task_id'],'caller':'external:old','status':'completed'}),('unknown',lambda t,a:{'task_id':a['task_id'],'status':'queued'})]:
            ctx=Context(self.s,'codex',backend);r=ctx.call('task_reference',{'task':'task','project':'demo','origin_caller':'portable:'+self.s.device,'origin_device':self.s.device,'instruction_id':case})
            self.assertTrue(r['read_only'])
            with self.assertRaises(Blocked):ctx.call('task_update',{'reference':r['id'],'summary':'not authorized','instruction_id':'edit-'+case})
    def test_month_compressed_normalized_conversation_retrieval_dedup_without_original_source(self):
        secret='sk-proj-'+'SYNTHETICONLY1234567890'*2;p=self.root/'selected.jsonl';p.write_text(canonical({'id':'one','role':'user','text':'排障顺序以及 api_key='+secret})+'\n')
        selected=sessions.select(self.s,'t3',p,'native-selected')['selection'];batch=sessions.read_batch(self.s,selected);receipt=sessions.commit(self.s,batch['batch'],'确定排障顺序')
        self.assertEqual(sessions.commit(self.s,batch['batch'],'确定排障顺序'),receipt);self.assertEqual(sessions.read_batch(self.s,selected)['messages'],[]);p.unlink()
        # Search uses only the small index, not eager unpacking on startup.
        with patch('zipfile.ZipFile',side_effect=AssertionError('search must not decompress')):
            found=self.call(self.b,'session_archive_search',{'query':'排障'})
        self.assertEqual(len(found),1);restored=self.call(self.a,'session_archive_get',{'archive':found[0]['id']})
        self.assertTrue(restored['privacy']['redacted']);self.assertNotIn(secret,canonical(restored));self.assertIn('排障顺序',restored['messages'][0]['text']);self.assertIn('not complete native',restored['fidelity'])
        index=self.s.get('session_archive_index:'+receipt['compressed_archive']);path=self.s.root/index['path'];self.assertEqual(path.suffix,'.zip')
        with zipfile.ZipFile(path) as z:self.assertEqual(len(z.namelist()),1)
        self.assertFalse(any('排障顺序以及' in q.read_text() for q in (self.s.root/'知识库').rglob('*.md')))
        path.write_bytes(b'corrupt retained archive')
        with self.assertRaises(Blocked):sessions.archive_get(self.s,index['id'])
        self.assertEqual(len(sessions.archive_search(self.s,'排障')),1)

if __name__=='__main__':unittest.main()
