import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from portable.core import Store,Blocked,canonical,digest
from portable.team import Team

DEVICE='429fd16a-0d89-440b-8040-a845715a47e4'
KEY='485d8ad6df9e9e317a9b85bd3429622340ba564b3b82f2ca53c6c5be6b3c2058'
TASK='task-016686742c274c84965eb1eea1e5021e'
AUDIT='6c72360583321e0f8553e4c63f69d243aae9043fe636da15bb7df98b297f159a'
BODY='{"arguments":{"idempotency_key":"'+KEY+'","task_id":"'+TASK+'"},"operation":"reverify_task"}'

class Legacy015(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(Path(self.tmp.name).resolve()/'fixture')
        self.s.put('device',DEVICE)
        project=self.s.root.parent/'project';project.mkdir();self.s.register('fixture',project,'fixture');self.project=project
        server=self.s.root.parent/'server.py';server.write_text('# synthetic transport')
        import sys
        self.s.put('team_config',{'python':sys.executable,'server':str(server),'caller':'portable:'+DEVICE})
        self.calls=[];self.audit=AUDIT
        self.team=Team(self.s,self.call)
        from portable.team import bind_new_tasks
        bind_new_tasks(self.s,self.call,'fixture');self.binding=self.s.get('readiness');self.calls=[]
        with self.s.db:self.s.db.execute('INSERT INTO outbox VALUES (?,?,?,NULL)',(KEY,BODY,'unknown'))
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def call(self,tool,args):
        self.calls.append(tool)
        if tool=='__capabilities__':return {'tools':['create_task','get_task','get_task_result','lookup_request','list_tasks']}
        if tool=='list_tasks':return {'tasks':[]}
        if tool=='get_task':return {'workspace':str(self.project),'caller':'portable:'+DEVICE,'task_id':TASK,'run_id':'20261024-051928-6baa08cf82fa','status':'awaiting_acceptance','verify':{'passed':True}}
        if tool=='rejection_evidence':
            if args['audit_sha256']!=self.audit:raise Blocked('audit changed')
            return {'ok':True,'task_id':TASK,'operation':'reverify_task','lookup_found':False,'operator_binding_required':True,'audit_sha256':self.audit,'error_code':'TASK_BUSY'}
        raise AssertionError('must not execute mutation')
    def test_exact_real_legacy_shape_retains_raw_body_and_appendix(self):
        self.assertEqual(digest(canonical({'operation':'reverify_task','arguments':{'task_id':TASK},'device':DEVICE}).encode()),KEY)
        review=self.team.rejection_evidence(KEY,AUDIT)
        self.assertEqual(review['record_format'],'legacy-0.1.2');self.assertEqual(review['binding_epoch'],self.binding['epoch'])
        with self.assertRaises(Blocked):self.team.review_rejection(KEY,review)
        review['legacy_attempt_binding_confirmed']=True;review['review_note']='Explicit synthetic review of exact reported legacy shape.'
        result=self.team.review_rejection(KEY,review)
        row=self.s.db.execute('SELECT * FROM outbox WHERE id=?',(KEY,)).fetchone()
        self.assertEqual(row['state'],'rejected');self.assertEqual(row['body'],BODY);self.assertIsNone(row['reference'])
        appendix=json.loads(self.s.db.execute('SELECT body FROM records WHERE id=?',(result['review_record'],)).fetchone()[0])
        self.assertEqual(appendix['original_row']['body'],BODY);self.assertEqual(appendix['original_row']['state'],'unknown')
        self.assertEqual(appendix['review'],review);self.assertFalse(result['resent']);self.assertFalse(result['backend_modified'])
    def test_other_device_changed_arguments_and_unknown_shapes_rejected_before_transport(self):
        self.s.put('device','other')
        with self.assertRaises(Blocked):self.team.rejection_evidence(KEY,AUDIT)
        self.s.put('device',DEVICE)
        for value in (
            {'operation':'reverify_task','arguments':{'idempotency_key':KEY,'task_id':'other-task'}},
            {'operation':'reverify_task','arguments':{'idempotency_key':'other-key','task_id':TASK}},
            {'operation':'reverify_task','arguments':{'idempotency_key':KEY,'task_id':TASK,'unknown':True}},
            {'operation':'reverify_task','arguments':{'idempotency_key':KEY,'task_id':TASK},'extra':True},
            {'operation':'unknown','arguments':{'idempotency_key':KEY,'task_id':TASK}},
            {'operation':'reverify_task','arguments':[]},
            {'operation':'reverify_task','arguments':{'task_id':TASK},'device':DEVICE,'binding_epoch':'current-epoch'},
            ['unknown-format'],
        ):
            with self.s.db:self.s.db.execute('UPDATE outbox SET body=? WHERE id=?',(canonical(value),KEY))
            with self.assertRaises(Blocked):self.team.rejection_evidence(KEY,AUDIT)
        self.assertEqual(self.calls,[])
    def test_audit_epoch_and_original_bytes_changes_do_not_consume_review(self):
        review=self.team.rejection_evidence(KEY,AUDIT);review['legacy_attempt_binding_confirmed']=True;review['review_note']='reviewed'
        self.audit='changed'
        with self.assertRaises(Blocked):self.team.review_rejection(KEY,review)
        self.audit=AUDIT;self.s.put('readiness',{**self.binding,'epoch':'new-epoch'})
        with self.assertRaises(Blocked):self.team.review_rejection(KEY,review)
        self.s.put('readiness',self.binding)
        with self.s.db:self.s.db.execute('UPDATE outbox SET body=? WHERE id=?',(json.dumps(json.loads(BODY)),KEY))
        with self.assertRaises(Blocked):self.team.review_rejection(KEY,review)
        self.assertEqual(self.s.db.execute('SELECT state FROM outbox').fetchone()[0],'unknown')
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM records WHERE kind='rejection_review'").fetchone()[0],0)
