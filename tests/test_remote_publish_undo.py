import unittest
from portable import remote_publish

class RemoteContract(unittest.TestCase):
    def test_unknown_rejected(self):
        with self.assertRaises(Exception):remote_publish.repository('https://example.org/a/b.git')
    def test_github_identity(self):
        self.assertEqual(remote_publish.repository('git@github.com:a/b.git')['repo'],'a/b')

import os
import json
from pathlib import Path
from unittest.mock import patch
from portable import remote_publish as rp
from portable.core import Blocked

class Synthetic:
    """Only PR HTTP is synthetic; every Git operation uses a temporary bare repo."""
    def __init__(self):self.rows=[];self.calls=[];self.lose=False;self.actor={'login':'builder','id':19};self.actor_calls=0
    def __call__(self,identity,method,path,payload=None):
        if path=='user':self.actor_calls+=1;return self.actor.copy()
        self.calls.append((identity.copy(),method,path,payload))
        if method=='GET':return self.rows
        agit=identity['platform']=='gitea'
        head_ref=self.actor['login']+'/'+payload['head'] if agit else payload['head']
        sha=__import__('tests.test_assist',fromlist=['g']).g(self.remote,'rev-parse','refs/pull/7/head' if agit else 'refs/heads/'+payload['head'])
        row={'number':7,'user':self.actor.copy(),'html_url':identity['origin']+'/'+identity['repo']+('/pull/' if identity['platform']=='github' else '/pulls/')+'7',
             'head':{'ref':head_ref,'sha':sha,'repo':{'full_name':identity['repo']}},
             'base':{'ref':payload['base'],'repo':{'full_name':identity['repo']}}}
        self.rows.append(row)
        if self.lose:raise Blocked('synthetic lost response')
        return row

def bind_remote(test,repo,remote,name='origin',platform='gitea'):
    from tests.test_assist import g
    url='https://github.com/test/shop.git' if platform=='github' else 'http://127.0.0.1:3000/test/shop.git'
    config=remote.parent/'synthetic.gitconfig'
    config.write_text('[url "'+str(remote)+'"]\n\tinsteadOf = '+url+'\n')
    env_patch=patch.dict(os.environ,{'GIT_CONFIG_GLOBAL':str(config),'GIT_CONFIG_NOSYSTEM':'1'})
    env_patch.start();test.addCleanup(env_patch.stop)
    g(repo,'remote','set-url',name,url)
    transport=Synthetic();transport.remote=remote
    original_network=PRODUCTION_NETWORK
    def synthetic_agit(root,*args,**kwargs):
        result=original_network(root,*args,**kwargs)
        refs=[arg for arg in args if isinstance(arg,str) and arg.startswith('HEAD:refs/for/')]
        if refs and not result.returncode:
            target=refs[0].removeprefix('HEAD:refs/for/')
            base,topic=target.rsplit('/',1)
            g(remote,'update-ref','refs/pull/7/head',g(root,'rev-parse','HEAD'))
            transport(rp.repository(url),'AGIT','refs/for/'+base+'/'+topic,{'head':topic,'base':base})
        return result
    net_patch=patch.object(rp,'network',synthetic_agit);net_patch.start();test.addCleanup(net_patch.stop)
    api_patch=patch.object(rp,'api',transport)
    api_patch.start();test.addCleanup(api_patch.stop)
    return transport,url

class UndoMatrix(unittest.TestCase):
    def setUp(self):
        from tests.test_assist import AssistTest
        AssistTest.setUp(self)
    def test_platforms_defaults_and_preservation(self):
        from portable import assist
        from tests.test_assist import g
        for branch in ('main','master','release/current'):
            for platform in ('gitea','github'):
                with self.subTest(branch=branch,platform=platform):
                    g(self.repo,'branch','-M',branch);g(self.repo,'push','-q',str(self.remote),branch)
                    g(self.remote,'symbolic-ref','HEAD','refs/heads/'+branch)
                    t,url=bind_remote(self,self.repo,self.remote,platform=platform)
                    (self.repo/'a.txt').write_text('dirty\n')
                    result=assist.undo_last(self.s,'shop',transport=t)
                    self.assertEqual(result['base'],branch)
                    self.assertIn('/pull/' if platform=='github' else '/pulls/',result['PR'])
                    self.assertEqual((self.repo/'a.txt').read_text(),'dirty\n')
                    self.assertEqual(g(self.remote,'show',('refs/pull/7/head' if platform=='gitea' else 'undo-'+result['提交'])+':a.txt'),'one')
                    self.assertEqual(t.calls[0][1],'GET')
                    self.assertEqual(next(c for c in t.calls if c[1] in ('POST','AGIT'))[3]['base'],branch)
                    # Isolate deterministic operation identity between subcases.
                    self.s.db.execute("DELETE FROM meta WHERE key LIKE 'undo:%'")
                    t.rows=[]
    def test_lost_response_queries_before_retry(self):
        from portable import assist
        t,_=bind_remote(self,self.repo,self.remote,platform='github');t.lose=True
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        result=assist.undo_last(self.s,'shop',transport=t)
        self.assertTrue(result['PR'].endswith('/pull/7'))
        self.assertEqual([c[1] for c in t.calls],['GET','POST','GET'])
    def test_unknown_default_conflict_and_unknown_platform(self):
        from portable import assist
        from tests.test_assist import g
        t,_=bind_remote(self,self.repo,self.remote)
        g(self.remote,'symbolic-ref','HEAD','refs/heads/missing')
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        g(self.remote,'symbolic-ref','HEAD','refs/heads/main')
        g(self.repo,'symbolic-ref','refs/remotes/origin/HEAD','refs/remotes/origin/master')
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        g(self.repo,'remote','set-url','origin','https://unknown.example/a/b')
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        self.assertEqual(t.calls,[])
    def test_local_custom_branch(self):
        from portable import assist
        from tests.test_assist import g
        g(self.repo,'remote','remove','origin');g(self.repo,'branch','-M','trunk')
        result=assist.undo_last(self.s,'shop')
        self.assertEqual(g(self.repo,'show',result['本地分支']+':a.txt'),'one')
    def test_cli_actual_entry(self):
        import subprocess,sys
        from tests.test_assist import g
        g(self.repo,'remote','remove','origin');g(self.repo,'branch','-M','trunk')
        p=subprocess.run([sys.executable,'-m','portable','--root',str(self.s.root),'assist','undo-last','shop','--base','trunk'],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr+p.stdout)
        self.assertIn('本地分支',p.stdout)

class PublishMatrix(unittest.TestCase):
    def setUp(self):
        from tests.test_publish import PublishTest
        from portable import board
        from tests.test_assist import g
        PublishTest.setUp(self)
        self.transport,self.url=bind_remote(self,self.source,self.remote,'gitea','github')
        board.apply(self.s,[{'id':'p','name':'project','path':str(self.source),'status_path':str(self.source/'STATUS.md'),'features_path':'','prd_path':''}])
        value=self.s.get('candidate:engineering:'+self.request)
        value.update(registered_source=str(self.source),publish_binding={'source':str(self.source),'remote':self.url,'base':'main','candidate_base':'main','source_head':g(self.source,'rev-parse','HEAD')})
        self.s.put('candidate:engineering:'+self.request,value)
    def test_publish_retry_and_binding(self):
        from portable import candidates
        self.transport.lose=True
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=self.transport)
        result=candidates.publish(self.s,self.request,transport=self.transport)
        self.assertEqual(result['state'],'pr_opened')
        self.assertEqual([c[1] for c in self.transport.calls],['GET','POST','GET'])
        self.assertEqual(candidates.publish(self.s,self.request),result)
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,'master')
    def test_tampered_source_or_remote(self):
        from portable import candidates
        from tests.test_assist import g
        g(self.source,'remote','set-url','gitea','https://github.com/test/other.git')
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request)
        self.assertEqual(self.transport.calls,[])
    def test_explicit_candidate_base_preserved(self):
        from portable import candidates
        from tests.test_assist import g
        for base in ('master','custom'):
            g(self.source,'push','-q',str(self.remote),'HEAD:refs/heads/'+base)
            g(self.remote,'symbolic-ref','HEAD','refs/heads/'+base)
            value=self.s.get('candidate:engineering:'+self.request);value['publish_binding']['base']=base
            self.s.put('candidate:engineering:'+self.request,value)
            result=candidates.publish(self.s,self.request,transport=self.transport)
            self.assertEqual(result['base'],base)
            # Each invocation has a distinct operation topic.
            self.request=base*16
            value['reception'].pop('publication',None)
            self.s.put('candidate:engineering:'+self.request,value)

class AdapterFailureTests(unittest.TestCase):
    setUp=UndoMatrix.setUp
    # Reuse the real Git fixture, without inheriting its matrix cases.
    def test_unknown_response_never_reposts(self):
        from portable import assist
        t,_=bind_remote(self,self.repo,self.remote,platform='github')
        def unknown(identity,method,path,payload=None):
            t.calls.append((identity,method,path,payload))
            return [] if method=='GET' else {}
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=unknown)
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=unknown)
        self.assertEqual([c[1] for c in t.calls],['GET','POST','GET'])
    def test_branch_collision_never_overwrites(self):
        from portable import assist
        from tests.test_assist import g
        t,_=bind_remote(self,self.repo,self.remote,platform='github')
        sha=g(self.repo,'rev-parse','HEAD')
        g(self.repo,'push','-q',str(self.remote),'HEAD:refs/heads/undo-'+sha[:12])
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        self.assertEqual(g(self.remote,'rev-parse','undo-'+sha[:12]),sha)
        self.assertEqual([c[1] for c in t.calls],['GET'])
    def test_unknown_query_cannot_create(self):
        from portable import assist
        bind_remote(self,self.repo,self.remote)
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=lambda *a: {})
    def test_remote_change_during_clone_blocks_write(self):
        from portable import assist
        from tests.test_assist import g
        t,_=bind_remote(self,self.repo,self.remote)
        original=rp.network
        def change(root,*args,**kwargs):
            result=original(root,*args,**kwargs)
            if args[0]=='clone':g(self.repo,'remote','set-url','origin','https://github.com/test/other.git')
            return result
        with patch.object(rp,'network',change),self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        self.assertEqual(t.calls,[])
    def test_closed_pr_is_not_recreated(self):
        from portable import assist
        t,_=bind_remote(self,self.repo,self.remote,platform='github');t.lose=True
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        t.rows[0]['state']='closed'
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        self.assertEqual([c[1] for c in t.calls],['GET','POST','GET'])
    def test_query_exhausts_pages_before_creation(self):
        from portable import assist
        t,_=bind_remote(self,self.repo,self.remote)
        def paged(identity,method,path,payload=None):
            if method=='GET' and 'page=1&' in path:
                t.calls.append((identity,method,path,payload));return [{} for _ in range(100)]
            return t(identity,method,path,payload)
        assist.undo_last(self.s,'shop',transport=paged)
        self.assertEqual([c[1] for c in t.calls],['GET','GET','AGIT','GET','GET'])
        self.assertIn('page=2',t.calls[1][2])
    def test_missing_tool_cannot_push(self):
        from portable import assist
        from tests.test_assist import g
        bind_remote(self,self.repo,self.remote,platform='github')
        with patch.object(rp,'api',PRODUCTION_API),patch.object(rp.shutil,'which',return_value=None):
            with self.assertRaises(Blocked):assist.undo_last(self.s,'shop')
        self.assertNotIn('undo-',g(self.remote,'for-each-ref'))

PRODUCTION_API=rp.api
PRODUCTION_NETWORK=rp.network

class APIContract(unittest.TestCase):
    def test_gh_exact_arguments(self):
        identity=rp.repository('https://github.com/test/shop.git')
        from types import SimpleNamespace
        with patch.object(rp.shutil,'which',return_value='/synthetic/gh'),patch.object(rp.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'[]')) as run:
            PRODUCTION_API(identity,'GET','repos/test/shop/pulls?state=open')
            self.assertEqual(run.call_args.args[0],['gh','api','--hostname','github.com','--method','GET','repos/test/shop/pulls?state=open'])
            PRODUCTION_API(identity,'POST','repos/test/shop/pulls',{'head':'topic','base':'master'})
            self.assertEqual(run.call_args.args[0][-2:],['--input','-'])
            self.assertEqual(json.loads(run.call_args.kwargs['input']),{'head':'topic','base':'master'})
    def test_gitea_method_path_and_payload(self):
        identity=rp.repository('http://127.0.0.1:3000/test/shop.git')
        from unittest.mock import MagicMock
        response=MagicMock();response.__enter__.return_value.read.return_value=b'{}'
        with patch.dict(os.environ,{'GITEA_TOKEN':'synthetic-only'}),patch.object(rp,'urlopen',return_value=response) as call:
            PRODUCTION_API(identity,'GET','repos/test/shop/pulls')
            req=call.call_args.args[0]
            self.assertEqual(req.full_url,'http://127.0.0.1:3000/api/v1/repos/test/shop/pulls')
            self.assertEqual(req.method,'GET');self.assertIsNone(req.data)
    def test_credentials_and_wrong_receipts_rejected(self):
        for url in ('https://token@github.com/a/b','https://github.com/a/b?token=x','file:///tmp/repo'):
            with self.assertRaises(Blocked):rp.repository(url)
        with self.assertRaises(Blocked):rp.receipt(rp.repository('https://github.com/a/b'),{'html_url':'https://github.com/other/repo/pull/1'})

class PublishCLI(unittest.TestCase):
    setUp=PublishMatrix.setUp
    def test_remote_cli_and_idempotence(self):
        import sys,subprocess
        folder=self.t/'bin';folder.mkdir()
        log=self.t/'gh-calls.jsonl';state=self.t/'gh-pr.json'
        fake=folder/'gh'
        fake.write_text('#!'+sys.executable+'\n'+'''import json,sys,os
from pathlib import Path
args=sys.argv[1:];data=json.load(sys.stdin) if '--input' in args else None
with open(os.environ['SYNTHETIC_GH_LOG'],'a') as f:f.write(json.dumps({'args':args,'data':data})+'\\n')
p=Path(os.environ['SYNTHETIC_GH_STATE'])
if 'GET' in args: print('['+p.read_text()+']' if p.exists() else '[]')
else:
 r={'html_url':'https://github.com/test/shop/pull/7','base':{'ref':data['base'],'repo':{'full_name':'test/shop'}},'head':{'ref':data['head'],'sha':__import__('subprocess').check_output(['git','--git-dir',os.environ['SYNTHETIC_BARE'],'rev-parse',data['head']]).decode().strip(),'repo':{'full_name':'test/shop'}}}
 p.write_text(json.dumps(r));print(json.dumps(r))
''');fake.chmod(0o700)
        env={**os.environ,'PATH':str(folder)+os.pathsep+os.environ['PATH'],'SYNTHETIC_GH_LOG':str(log),'SYNTHETIC_GH_STATE':str(state),'SYNTHETIC_BARE':str(self.remote)}
        command=[sys.executable,'-m','portable','--root',str(self.s.root),'candidates','publish',self.request,'--base','main']
        for _ in range(2):
            p=subprocess.run(command,capture_output=True,text=True,env=env)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr);self.assertIn('https://github.com/test/shop/pull/7',p.stdout)
        calls=[json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len([c for c in calls if 'POST' in c['args']]),1)
        self.assertIn('repos/test/shop/pulls?state=all&base=main&head=test%3Aportable-',calls[0]['args'][-1])
        self.assertEqual(calls[1]['data']['head'],'portable-'+self.request[:12])
        # CLI failure must preserve the successful operation rather than switch base.
        p=subprocess.run(command[:-1]+['master'],capture_output=True,text=True,env=env)
        self.assertNotEqual(p.returncode,0)
        undo=[sys.executable,'-m','portable','--root',str(self.s.root),'assist','undo-last','p','--base','main']
        for _ in range(2):
            p=subprocess.run(undo,capture_output=True,text=True,env=env)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr);self.assertIn('/pull/7',p.stdout)
        calls=[json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len([c for c in calls if 'POST' in c['args']]),2)
        self.assertTrue(calls[-1]['data']['head'].startswith('undo-'))


class CandidateLifecycle(unittest.TestCase):
    setUp=PublishMatrix.setUp
    def test_prepare_receive_publish_real_lifecycle(self):
        from portable import candidates
        from portable.workflows import repo_version
        from tests.test_assist import g
        key='lifecycle'
        value=candidates.prepare(self.s,key,'p',repo_version(self.s,'p')['sha256'])
        work=Path(value['workspace'])
        self.assertEqual(value['publish_binding']['candidate_base'],g(work,'rev-parse','HEAD'))
        self.assertIsNone(value['publish_binding']['base'])
        g(work,'checkout','-qb','candidate')
        (work/'c.txt').write_text('lifecycle\n');g(work,'add','.');g(work,'commit','-qm','lifecycle')
        reception=candidates.receive(self.s,key)
        self.assertEqual(reception['state'],'awaiting_external_owner')
        result=candidates.publish(self.s,key,transport=self.transport)
        self.assertEqual(result['base'],'main')
        self.assertEqual(g(self.remote,'show',result['topic']+':c.txt'),'lifecycle')
        self.assertFalse((self.source/'c.txt').exists())

class MoreBoundaryCases(unittest.TestCase):
    setUp=PublishMatrix.setUp
    def test_delivered_mutation_and_missing_binding_preserved(self):
        from portable import candidates
        before=(self.source/'a.txt').read_bytes()
        (self.delivered/'b.txt').write_text('tampered')
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=self.transport)
        self.assertEqual(self.transport.calls,[])
        self.assertEqual((self.source/'a.txt').read_bytes(),before)
        value=self.s.get('candidate:engineering:'+self.request);value.pop('publish_binding')
        self.s.put('candidate:engineering:'+self.request,value)
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=self.transport)
        self.assertTrue(self.delivered.exists())
    def test_registry_identity_changed(self):
        from portable import candidates,board
        board.apply(self.s,[{'id':'p','name':'p','path':str(self.delivered),'status_path':str(self.delivered/'STATUS.md'),'features_path':'','prd_path':''}])
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=self.transport)
        self.assertEqual(self.transport.calls,[])
    def test_local_remote_default_disagreement(self):
        from portable import candidates
        from tests.test_assist import g
        g(self.source,'symbolic-ref','refs/remotes/gitea/HEAD','refs/remotes/gitea/master')
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=self.transport)
        self.assertEqual(self.transport.calls,[])
    def test_create_only_lease_rejects_racing_branch(self):
        from portable import candidates
        from tests.test_assist import g
        original=rp.network;calls=[]
        def race(root,*args,**kwargs):
            calls.append(args)
            if args[0]=='push':
                # Another writer creates exactly an ancestor just after our read.
                g(self.source,'push','-q',str(self.remote),'HEAD:refs/heads/portable-'+self.request[:12])
            return original(root,*args,**kwargs)
        with patch.object(rp,'network',race),self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=self.transport)
        self.assertEqual(g(self.remote,'rev-parse','portable-'+self.request[:12]),g(self.source,'rev-parse','HEAD'))
        self.assertEqual([c[1] for c in self.transport.calls],['GET'])
        self.assertIn('--force-with-lease=refs/heads/portable-'+self.request[:12]+':',calls[-1])

class LocalCandidateBoundary(unittest.TestCase):
    setUp=PublishMatrix.setUp
    def test_plain_directory_candidate_does_not_gain_remote_authority(self):
        from portable import board,candidates
        from portable.workflows import repo_version
        plain=self.t/'plain';plain.mkdir();(plain/'README.md').write_text('local project\n')
        board.apply(self.s,board.load(self.s)+[{'id':'plain','name':'plain','path':str(plain),'status_path':str(plain/'STATUS.md'),'features_path':'','prd_path':''}])
        with self.assertRaises(Blocked):candidates.prepare(self.s,'plain-request','plain','unverified')
        self.assertIsNone(self.s.get('candidate:engineering:plain-request'))
    def test_nested_registration_cannot_borrow_parent_remote(self):
        from portable import board,candidates
        from portable.workflows import repo_version
        nested=self.source/'nested';nested.mkdir();(nested/'README.md').write_text('nested project\n')
        board.apply(self.s,board.load(self.s)+[{'id':'nested','name':'nested','path':str(nested),'status_path':str(nested/'STATUS.md'),'features_path':'','prd_path':''}])
        with self.assertRaises(Blocked):candidates.prepare(self.s,'nested-request','nested','unverified')
        self.assertIsNone(self.s.get('candidate:engineering:nested-request'))
        from portable import assist
        with self.assertRaises(Blocked):assist.undo_last(self.s,'nested',transport=self.transport)
        self.assertEqual(self.transport.calls,[])

class ReviewRegressions(unittest.TestCase):
    setUp=PublishMatrix.setUp
    def test_wrong_head_receipt_rejected(self):
        identity=rp.repository(self.url)
        row={'html_url':'https://github.com/test/shop/pull/7','base':{'ref':'main','repo':{'full_name':'test/shop'}},'head':{'ref':'topic','sha':'0'*40,'repo':{'full_name':'test/shop'}}}
        with self.assertRaises(Blocked):rp.receipt(identity,row,'main','topic','1'*40)
    def test_publish_rechecks_remote_after_clone(self):
        from portable import candidates
        from tests.test_assist import g
        original=rp.network
        def change(root,*args,**kwargs):
            result=original(root,*args,**kwargs)
            if args[0]=='clone':g(self.source,'remote','set-url','gitea','https://github.com/test/other.git')
            return result
        with patch.object(rp,'network',change),self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=self.transport)
        self.assertFalse(self.transport.rows)
        from tests.test_assist import g
        self.assertNotIn('refs/heads/portable-',g(self.remote,'for-each-ref'))
    def test_not_sent_can_retry(self):
        from portable import candidates
        t=self.transport
        def unsent(identity,method,path,payload=None):
            if method=='POST':raise rp.DeliveryError('not_sent','synthetic process unavailable')
            return t(identity,method,path,payload)
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=unsent)
        self.assertEqual(self.s.get('publish-pending:'+self.request)['state'],'not_sent')
        self.assertEqual(candidates.publish(self.s,self.request,transport=t)['state'],'pr_opened')
    def test_explicit_rejection_can_retry(self):
        from portable import candidates
        t=self.transport
        def rejected(identity,method,path,payload=None):
            if method=='POST':raise rp.DeliveryError('rejected','synthetic 422')
            return t(identity,method,path,payload)
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=rejected)
        self.assertEqual(self.s.get('publish-pending:'+self.request)['state'],'rejected')
        self.assertEqual(candidates.publish(self.s,self.request,transport=t)['state'],'pr_opened')

    def test_publish_rechecks_registered_source_after_clone(self):
        from portable import candidates,board
        original=rp.network
        def change(root,*args,**kwargs):
            result=original(root,*args,**kwargs)
            if args[0]=='clone':board.apply(self.s,[{'id':'p','name':'project','path':str(self.delivered),'status_path':str(self.delivered/'STATUS.md'),'features_path':'','prd_path':''}])
            return result
        with patch.object(rp,'network',change),self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=self.transport)
        self.assertFalse(self.transport.rows)
        from tests.test_assist import g
        self.assertNotIn('refs/heads/portable-',g(self.remote,'for-each-ref'))
    def test_wrong_head_on_post_and_query_remains_unknown(self):
        from portable import candidates
        t=self.transport
        def wrong(identity,method,path,payload=None):
            result=t(identity,method,path,payload)
            if method=='POST':result['head']['sha']='0'*40
            return result
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=wrong)
        self.assertEqual(self.s.get('publish-pending:'+self.request)['state'],'sent_unknown')
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=wrong)
        self.assertEqual([c[1] for c in t.calls],['GET','POST','GET'])
    def test_timeout_then_query_confirms_original_sha_once(self):
        from portable import candidates
        t=self.transport
        def timeout(identity,method,path,payload=None):
            result=t(identity,method,path,payload)
            if method=='POST':raise rp.DeliveryError('sent_unknown','synthetic timeout after dispatch')
            return result
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=timeout)
        self.assertEqual(self.s.get('publish-pending:'+self.request)['state'],'sent_unknown')
        result=candidates.publish(self.s,self.request,transport=t)
        self.assertEqual(result['state'],'pr_opened')
        self.assertEqual(self.s.get('publish-pending:'+self.request)['state'],'confirmed')
        self.assertEqual(candidates.publish(self.s,self.request,transport=t),result)
        self.assertEqual([c[1] for c in t.calls],['GET','POST','GET'])

class ReviewDeliveryContracts(unittest.TestCase):
    setUp=UndoMatrix.setUp
    def test_gitea_without_token_keeps_agit_and_queries(self):
        from portable import assist
        t,url=bind_remote(self,self.repo,self.remote)
        original=rp.network;calls=[]
        def capture(root,*args,**kwargs):
            calls.append(args);return original(root,*args,**kwargs)
        with patch.dict(os.environ,{'GITEA_TOKEN':''}),patch.object(rp,'network',capture):
            result=assist.undo_last(self.s,'shop',transport=t)
            self.assertEqual(assist.undo_last(self.s,'shop',transport=t),result)
        self.assertEqual([c[1] for c in t.calls],['GET','AGIT','GET'])
        pushed=next(c for c in calls if c[0]=='push')
        self.assertIn('HEAD:refs/for/main/undo-'+result['提交'],pushed)
        self.assertIn('title=撤回：feat: 改成 two',pushed)
        self.assertFalse(any('force' in c for c in pushed))
    def test_missing_gh_preflight_leaves_no_pending_then_recovers(self):
        from portable import assist
        t,_=bind_remote(self,self.repo,self.remote,platform='github')
        with patch.object(rp,'api',PRODUCTION_API),patch.object(rp.shutil,'which',return_value=None):
            with self.assertRaises(rp.DeliveryError) as error:assist.undo_last(self.s,'shop')
        self.assertEqual(error.exception.state,'not_sent')
        self.assertEqual(list(self.s.db.execute("SELECT key FROM meta WHERE key LIKE 'undo:%'")),[])
        self.assertIn('/pull/7',assist.undo_last(self.s,'shop',transport=t)['PR'])
    def test_missing_token_and_query_denied_can_recover_without_token(self):
        from portable import assist
        from urllib.error import HTTPError
        from types import SimpleNamespace
        t,_=bind_remote(self,self.repo,self.remote)
        with patch.dict(os.environ,{'GITEA_TOKEN':''}),patch.object(rp,'api',PRODUCTION_API),patch.object(rp.subprocess,'run',wraps=rp.subprocess.run) as run:
            # Prevent reading any real helper. Git clone/ls-remote remain real.
            original=run._mock_wraps
            def only_synthetic(*args,**kwargs):
                if args[0]==['git','credential','fill']:return SimpleNamespace(returncode=1,stdout=b'')
                return original(*args,**kwargs)
            run.side_effect=only_synthetic
            with patch.object(rp,'urlopen',side_effect=HTTPError('http://synthetic',401,'denied',{},__import__('io').BytesIO(b''))):
                with self.assertRaises(rp.DeliveryError):assist.undo_last(self.s,'shop')
        self.assertEqual(list(self.s.db.execute("SELECT key FROM meta WHERE key LIKE 'undo:%'")),[])
        with patch.dict(os.environ,{'GITEA_TOKEN':''}):self.assertIn('/pulls/7',assist.undo_last(self.s,'shop',transport=t)['PR'])
    def test_git_helper_read_only_auth_for_query(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        identity=rp.repository('http://127.0.0.1:3000/test/shop.git')
        response=MagicMock();response.__enter__.return_value.read.return_value=b'[]'
        with patch.dict(os.environ,{'GITEA_TOKEN':''}),patch.object(rp.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'username=synthetic\npassword=synthetic-only\n')) as run,patch.object(rp,'urlopen',return_value=response) as opened:
            self.assertEqual(PRODUCTION_API(identity,'GET','repos/test/shop/pulls'),[])
            self.assertEqual(run.call_args.args[0],['git','credential','fill'])
            self.assertEqual(opened.call_args.args[0].get_header('Authorization'),'Basic c3ludGhldGljOnN5bnRoZXRpYy1vbmx5')
    def test_production_gh_unsent_rejected_and_timeout_classification(self):
        from types import SimpleNamespace
        import subprocess
        identity=rp.repository('https://github.com/test/shop.git')
        for outcome,state in ((OSError('synthetic'),'not_sent'),(subprocess.TimeoutExpired('gh',60),'sent_unknown'),(SimpleNamespace(returncode=1,stdout=b'',stderr=b'gh: Validation Failed (HTTP 422)'),'rejected')):
            with self.subTest(state=state),patch.object(rp.shutil,'which',return_value='/synthetic/gh'):
                with patch.object(rp.subprocess,'run',side_effect=outcome if isinstance(outcome,Exception) else None,return_value=outcome):
                    with self.assertRaises(rp.DeliveryError) as error:PRODUCTION_API(identity,'POST','repos/test/shop/pulls',{})
                    self.assertEqual(error.exception.state,state)
    def test_gitea_timeout_queries_without_resending_agit(self):
        from portable import assist
        t,_=bind_remote(self,self.repo,self.remote)
        original=rp.network
        def timeout(root,*args,**kwargs):
            result=original(root,*args,**kwargs)
            if args[0]=='push':raise rp.DeliveryError('sent_unknown','synthetic Git timeout after acceptance')
            return result
        with patch.object(rp,'network',timeout),self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        self.assertIn('/pulls/7',assist.undo_last(self.s,'shop',transport=t)['PR'])
        self.assertEqual([c[1] for c in t.calls],['GET','AGIT','GET'])
    def _gitea_proven_failure_then_retry(self,state):
        from portable import assist
        from types import SimpleNamespace
        t,_=bind_remote(self,self.repo,self.remote);original=rp.network
        def failed(root,*args,**kwargs):
            if args[0]=='push':
                if state=='not_sent':raise rp.DeliveryError(state,'synthetic Git not launched')
                ref=next(arg.removeprefix('HEAD:') for arg in args if arg.startswith('HEAD:refs/for/'))
                return SimpleNamespace(returncode=1,stderr=(' ! [remote rejected] HEAD -> '+ref+' (pre-receive hook declined)\n').encode(),stdout=b'')
            return original(root,*args,**kwargs)
        with patch.object(rp,'network',failed),self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        key=next(row[0] for row in self.s.db.execute("SELECT key FROM meta WHERE key LIKE 'undo:%:pending'"))
        self.assertEqual(self.s.get(key)['state'],state)
        self.assertIn('/pulls/7',assist.undo_last(self.s,'shop',transport=t)['PR'])
        self.assertEqual(self.s.get(key)['state'],'confirmed')
        self.assertEqual([c[1] for c in t.calls],['GET','GET','AGIT','GET'])
    def test_gitea_not_sent_can_retry(self):self._gitea_proven_failure_then_retry('not_sent')
    def test_gitea_explicit_rejection_can_retry(self):self._gitea_proven_failure_then_retry('rejected')

class FinalAGitContract(unittest.TestCase):
    setUp=UndoMatrix.setUp
    def test_username_hidden_ref_without_normal_head(self):
        from portable import assist
        from tests.test_assist import g
        t,_=bind_remote(self,self.repo,self.remote)
        result=assist.undo_last(self.s,'shop',transport=t)
        self.assertEqual(t.rows[0]['head']['ref'],'builder/undo-'+result['提交'])
        self.assertEqual(g(self.remote,'show','refs/pull/7/head:a.txt'),'one')
        self.assertNotIn('refs/heads/undo-',g(self.remote,'for-each-ref'))
        self.assertGreater(t.actor_calls,0)

class FinalRecoveryContract(unittest.TestCase):
    setUp=PublishMatrix.setUp
    def test_base_conflict_recovers_before_clone_or_am(self):
        from portable import candidates
        from tests.test_assist import g
        self.transport.lose=True
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=self.transport)
        original=self.s.get('publish-pending:'+self.request)['head']
        (self.source/'b.txt').write_text('conflict after PR created\n')
        g(self.source,'add','.');g(self.source,'commit','-qm','advance base with conflict');g(self.source,'push','-q',str(self.remote),'main')
        network=rp.network
        def no_clone(root,*args,**kwargs):
            if args[0]=='clone':self.fail('recovery must query before clone/am')
            return network(root,*args,**kwargs)
        with patch.object(rp,'network',no_clone):result=candidates.publish(self.s,self.request,transport=self.transport)
        self.assertEqual(result['head'],original)
        self.assertEqual([c[1] for c in self.transport.calls],['GET','POST','GET'])
    def test_agit_base_conflict_recovers_original_hidden_ref(self):
        from portable import candidates
        from tests.test_assist import g
        t,url=bind_remote(self,self.source,self.remote,'gitea','gitea')
        value=self.s.get('candidate:engineering:'+self.request);value['publish_binding']['remote']=url
        self.s.put('candidate:engineering:'+self.request,value)
        t.lose=True
        with self.assertRaises(Blocked):candidates.publish(self.s,self.request,transport=t)
        original=self.s.get('publish-pending:'+self.request)['head']
        (self.source/'b.txt').write_text('later conflicting base\n')
        g(self.source,'add','.');g(self.source,'commit','-qm','conflicting base');g(self.source,'push','-q',str(self.remote),'main')
        network=rp.network
        def no_rebuild(root,*args,**kwargs):
            if args[0] in ('clone','push'):self.fail('unknown original operation must only query')
            return network(root,*args,**kwargs)
        with patch.object(rp,'network',no_rebuild):result=candidates.publish(self.s,self.request,transport=t)
        self.assertEqual(result['head'],original)
        self.assertEqual(g(self.remote,'rev-parse','refs/pull/7/head'),original)
        self.assertNotIn('refs/heads/portable-',g(self.remote,'for-each-ref'))
        self.assertEqual([c[1] for c in t.calls],['GET','AGIT','GET'])

class FinalAGitRejections(unittest.TestCase):
    setUp=UndoMatrix.setUp
    def test_wrong_author_repo_base_sha_or_hidden_ref_never_resends(self):
        from portable import assist
        from tests.test_assist import g
        import copy
        t,_=bind_remote(self,self.repo,self.remote);t.lose=True
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        t.lose=False;good=copy.deepcopy(t.rows[0]);head=g(self.remote,'rev-parse','refs/pull/7/head')
        mutations=[('user','login','other'),('user','id',20),('head','ref','other/'+good['head']['ref'].split('/',1)[1]),('head','sha','0'*40),('base','ref','master')]
        for section,field,value in mutations:
            with self.subTest(section=section,field=field):
                t.rows=[copy.deepcopy(good)];t.rows[0][section][field]=value
                with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        t.rows=[copy.deepcopy(good)];t.rows[0]['head']['repo']['full_name']='foreign/shop'
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        t.rows=[copy.deepcopy(good)];g(self.remote,'update-ref','refs/pull/7/head',g(self.repo,'rev-parse','HEAD'))
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        g(self.remote,'update-ref','-d','refs/pull/7/head')
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        g(self.remote,'update-ref','refs/pull/7/head',head)
        old_actor=t.actor;t.actor={'login':'other','id':20}
        with self.assertRaises(Blocked):assist.undo_last(self.s,'shop',transport=t)
        t.actor=old_actor
        self.assertIn('/pulls/7',assist.undo_last(self.s,'shop',transport=t)['PR'])
        self.assertEqual(len([c for c in t.calls if c[1]=='AGIT']),1)
