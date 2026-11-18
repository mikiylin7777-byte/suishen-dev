import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from portable.core import Store,Blocked
from portable import candidates
from portable.workflows import repo_fingerprint

ENV={**os.environ,'GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1'}

def g(root,*args):
    return subprocess.run(['git','-c','user.name=t','-c','user.email=t@t','-C',str(root),*args],check=True,capture_output=True,env={**os.environ,'GIT_CONFIG_NOSYSTEM':'1'}).stdout.decode().strip()


class PublishTest(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.t=Path(temp.name).resolve()
        self.s=Store(self.t/'app');self.addCleanup(self.s.db.close)
        self.remote=self.t/'remote.git';subprocess.run(['git','init','-q','--bare','-b','main',str(self.remote)],check=True);g(self.remote,'config','receive.advertisePushOptions','true')
        self.source=self.t/'source';self.source.mkdir();g(self.source,'init','-q','-b','main')
        (self.source/'a.txt').write_text('one\n');g(self.source,'add','.');g(self.source,'commit','-qm','base')
        g(self.source,'remote','add','gitea',str(self.remote));g(self.source,'push','-q','gitea','main')
        # Delivered candidate: detached snapshot history (main) plus a candidate branch.
        self.delivered=self.t/'delivery/完整候选';self.delivered.mkdir(parents=True);g(self.delivered,'init','-q','-b','main')
        (self.delivered/'a.txt').write_text('one\n');g(self.delivered,'add','.');g(self.delivered,'commit','-qm','snapshot')
        g(self.delivered,'checkout','-qb','candidate');(self.delivered/'b.txt').write_text('new\n');g(self.delivered,'add','.');g(self.delivered,'commit','-qm','feat: add b')
        self.request='r'*64
        self.s.put('candidate:engineering:'+self.request,{'project':'p','reception':{'state':'awaiting_external_owner','delivered':str(self.delivered),'delivered_sha256':repo_fingerprint(self.delivered)['sha256'],'source':str(self.source)}})
        from tests.test_remote_publish_undo import bind_remote
        from portable import board
        self.transport,url=bind_remote(self,self.source,self.remote,'gitea')
        board.apply(self.s,[{'id':'p','name':'p','path':str(self.source),'status_path':str(self.source/'STATUS.md'),'features_path':'','prd_path':''}])
        value=self.s.get('candidate:engineering:'+self.request)
        value.update(registered_source=str(self.source),publish_binding={'source':str(self.source),'remote':url,'base':'main','candidate_base':'main','source_head':g(self.source,'rev-parse','HEAD')})
        self.s.put('candidate:engineering:'+self.request,value)


    def test_replays_onto_remote_base_and_records(self):
        # Remote main moved on after the snapshot; the candidate still applies.
        g(self.source,'checkout','-q','main');(self.source/'c.txt').write_text('later\n');g(self.source,'add','.');g(self.source,'commit','-qm','later');g(self.source,'push','-q','gitea','main')
        before=g(self.source,'rev-parse','HEAD')
        result=candidates.publish(self.s,self.request)
        self.assertEqual(result['topic'],'portable-'+self.request[:12]);self.assertFalse(result['production_deployed'])
        ref=g(self.remote,'rev-parse','refs/pull/7/head')
        self.assertEqual(ref,result['head']);self.assertEqual(g(self.remote,'log','-1','--format=%s',ref),'feat: add b')
        self.assertEqual(g(self.remote,'show',ref+':c.txt'),'later')
        self.assertEqual(g(self.source,'rev-parse','HEAD'),before)  # source checkout untouched
        self.assertEqual(candidates.publish(self.s,self.request),result)  # idempotent
        # The PR merges upstream: re-running records reception and releases the project.
        g(self.source,'fetch','-q','gitea','refs/pull/7/head');g(self.source,'merge','-q','--no-edit','FETCH_HEAD');g(self.source,'push','-q','gitea','main')
        merged=candidates.publish(self.s,self.request)
        self.assertEqual(merged['state'],'merged');self.assertEqual(candidates.pending(self.s,'p'),[])

    def test_conflict_blocks_without_push(self):
        (self.source/'b.txt').write_text('other\n');g(self.source,'add','.');g(self.source,'commit','-qm','clash');g(self.source,'push','-q','gitea','main')
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request)
        self.assertNotIn('portable-',g(self.remote,'for-each-ref'))
        self.assertNotIn('publication',self.s.get('candidate:engineering:'+self.request)['reception'])

    def test_already_merged_records_reception_without_push(self):
        (self.source/'b.txt').write_text('new\n');g(self.source,'add','.');g(self.source,'commit','-qm','same');g(self.source,'push','-q','gitea','main')
        result=candidates.publish(self.s,self.request)
        self.assertEqual(result['state'],'already_merged')
        self.assertNotIn('refs/for',g(self.remote,'for-each-ref'))
        self.assertEqual(self.s.get('candidate:engineering:'+self.request)['reception']['state'],'received')
        self.assertEqual(candidates.pending(self.s,'p'),[])

    def test_requires_pending_external_candidate(self):
        with self.assertRaises(Blocked):candidates.publish(self.s,'x'*64)


if __name__=='__main__':unittest.main()
