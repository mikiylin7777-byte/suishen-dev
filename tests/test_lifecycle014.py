import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch,Mock

SOURCE=Path(__file__).resolve().parents[1]
from portable.core import Blocked,Store,canonical
from portable.team import Team,RejectedBeforeExecution,rejection_from,transport

class Client014(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.s=Store(self.root/'app')
        self.version={'task_id':'t1','run_id':'r1','updated':'v1','verify':{'passed':True},'status':'awaiting_acceptance'}
        project=self.root/'project';project.mkdir();self.s.register('fixture',project,'fixture')
        server=self.root/'server.py';server.write_text('# synthetic transport')
        self.s.put('team_config',{'python':sys.executable,'server':str(server),'caller':'portable:'+self.s.device})
        self.version.update(workspace=str(project),caller='portable:'+self.s.device)
        self.args={'task_id':'t1'};self.sent=[]
        self.team=Team(self.s,self.call)
        from portable.team import bind_new_tasks
        bind_new_tasks(self.s,self.call,'fixture');self.binding=self.s.get('readiness')
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def call(self,tool,args):
        if tool=='__capabilities__':return {'tools':['create_task','get_task','get_task_result','lookup_request','list_tasks']}
        if tool=='list_tasks':return {'tasks':[]}
        if tool=='get_task':return dict(self.version)
        if tool=='lookup_request':return {'found':False}
        if tool=='rejection_evidence':return {'ok':True,'task_id':'t1','operation':'reverify_task','lookup_found':False,'audit_sha256':args['audit_sha256'],'audit_time':'fixture','error_code':'TASK_BUSY','operator_binding_required':True}
        self.sent.append((tool,args));return {'ok':True,'task_id':'t1'}
    def unknown(self):
        key=self.team.new_instruction('reverify_task',self.args)['instruction_id']
        with self.s.db:self.s.db.execute("UPDATE outbox SET state='unknown' WHERE id=?",(key,))
        return key
    def test_attested_rejection_stored_rejected_never_automatic_retry(self):
        key=self.team.new_instruction('reverify_task',self.args)['instruction_id']
        evidence={'code':'TASK_BUSY','admission':'rejected_before_execution','request_record_created':False,'operation':'reverify_task','task_id':'t1','idempotency_key_sha256':hashlib.sha256(key.encode()).hexdigest()}
        def reject(tool,args):
            if tool=='reverify_task':raise RejectedBeforeExecution(evidence)
            return self.call(tool,args)
        self.team.transport=reject
        with self.assertRaises(RejectedBeforeExecution):self.team.lifecycle('reverify_task',self.args,key)
        row=self.s.db.execute('SELECT * FROM outbox WHERE id=?',(key,)).fetchone();self.assertEqual(row['state'],'rejected');self.assertIn('rejection',json.loads(row['body']))
        with self.assertRaises(Blocked):self.team.lifecycle('reverify_task',self.args,key)
        second=self.team.new_instruction('reverify_task',self.args)['instruction_id'];self.assertNotEqual(key,second)
    def test_unknown_and_found_false_alone_never_unlock(self):
        key=self.unknown();self.team.lifecycle('reverify_task',self.args,key)
        with self.assertRaises(Blocked):self.team.new_instruction('reverify_task',self.args)
        self.assertEqual(self.s.db.execute('SELECT state FROM outbox').fetchone()[0],'unknown');self.assertEqual(self.sent,[])
    def test_review_keeps_old_history_and_requires_current_exact_material(self):
        key=self.unknown();review=self.team.rejection_evidence(key,'a'*64)
        with self.assertRaises(Blocked):self.team.review_rejection(key,review)
        review['legacy_attempt_binding_confirmed']=True;review['review_note']='Operator verified legacy audit belongs to this unique attempt and source hash.'
        tampered=dict(review);tampered['instruction_id']='other'
        with self.assertRaises(Blocked):self.team.review_rejection(key,tampered)
        self.version['updated']='v2'
        with self.assertRaises(Blocked):self.team.review_rejection(key,review)
        self.version['updated']='v1'
        result=self.team.review_rejection(key,review);self.assertFalse(result['resent']);self.assertFalse(result['backend_modified'])
        row=self.s.db.execute('SELECT * FROM outbox WHERE id=?',(key,)).fetchone();self.assertEqual(row['state'],'rejected')
        self.assertEqual(json.loads(row['body'])['rejection_recovery']['previous_state'],'unknown')
        from portable.team import reconcile_outbox
        self.assertEqual(reconcile_outbox(self.s,self.call),{'terminal':0})
        self.team.new_instruction('reverify_task',self.args);self.assertEqual(self.sent,[])
    def test_transport_only_accepts_correlated_structured_rejection(self):
        args={'task_id':'t1','idempotency_key':'i1'}
        response={'ok':False,'error':{'code':'TASK_BUSY','details':{'admission':'rejected_before_execution','request_record_created':False,'operation':'reverify_task','task_id':'t1','idempotency_key_sha256':hashlib.sha256(b'i1').hexdigest()}}}
        self.assertIsNotNone(rejection_from(response,'reverify_task',args))
        self.assertIsNone(rejection_from(response,'reverify_task',{'task_id':'t1','idempotency_key':'wrong'}))
        cfg={'caller':'portable:fixture','python':sys.executable,'server':'fixture'}
        with patch('portable.team.subprocess.run',return_value=subprocess.CompletedProcess([],2,canonical({'ok':False,'response':response}),'')):
            with self.assertRaises(RejectedBeforeExecution):transport(cfg)('reverify_task',args)
        with patch('portable.team.subprocess.run',return_value=subprocess.CompletedProcess([],2,canonical({'ok':False,'response':{'ok':False,'error':{'code':'TASK_BUSY'}}}),'')):
            with self.assertRaises(Blocked) as error:transport(cfg)('reverify_task',args)
            self.assertNotIsInstance(error.exception,RejectedBeforeExecution)

class IsolationRepair014(unittest.TestCase):
    def test_recovery_is_exact_target_only_and_keeps_unknown_paused(self):
        from portable import distribution as d
        from portable.distribution import build
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();old=root/'trial';candidate=root/'candidate'
            build(SOURCE,old);build(SOURCE,candidate)
            manifest=json.loads((old/'发行.json').read_text());manifest['version']='0.1.2';(old/'发行.json').write_text(canonical(manifest))
            s=Store(old)
            s.put('execution','active')
            with s.db:s.db.execute('INSERT INTO outbox VALUES (?,?,?,?)',('legacy',canonical({'operation':'reverify_task','arguments':{'task_id':'fixture'}}),'unknown','fixture'))
            device=s.device;before=dict(s.db.execute('SELECT * FROM outbox').fetchone());s.db.close()
            with self.assertRaises(Blocked):d.atomic_upgrade(old,candidate,root/'backups',isolated_recovery=True)
            with patch.object(d,'ISOLATED_RECOVERY_TARGET',old):
                result=d.atomic_upgrade(old,candidate,root/'backups',isolated_recovery=True)
            self.assertTrue(result['isolated_recovery']);self.assertEqual(result['from'],'0.1.2');self.assertEqual(result['to'],__import__('portable.core',fromlist=['VERSION']).VERSION);new=Store(old)
            self.assertEqual(new.device,device);self.assertEqual(new.get('execution'),'recovery-paused')
            self.assertEqual(dict(new.db.execute('SELECT * FROM outbox').fetchone()),before);new.db.close()
