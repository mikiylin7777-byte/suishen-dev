import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from portable.core import Blocked, Store
from portable import ci


class FakeApi:
    def __init__(self,pr,statuses,platform='gitea'):
        self.pr=pr;self.statuses=statuses;self.calls=[];self.platform=platform;self.commit_time='2026-11-14T00:00:00+08:00';self.tasks=None;self.files=[{'filename':'src/a.py','status':'changed'}];self.branches=set();self.branch_error=404
    def __call__(self,idn,method,path,payload=None):
        self.calls.append((method,path))
        if '/pulls/' in path and method=='GET' and '/files' not in path:return self.pr() if callable(self.pr) else self.pr
        if path.endswith('/status'):
            v=self.statuses() if callable(self.statuses) else self.statuses
            return {'state':'success','statuses':v}
        if 'check-runs' in path:return {'check_runs':[]}
        if '/actions/tasks' in path and self.tasks is not None:return {'workflow_runs':self.tasks}
        if '/contents/tools/ci/docs_only.py' in path:
            from portable.remote_publish import DeliveryError
            if getattr(self,'docs_gate',False):return {'name':'docs_only.py'}
            raise DeliveryError('rejected','404',404)
        if '/branches/' in path:
            from portable.remote_publish import DeliveryError
            if path.rsplit('/',1)[1] in self.branches:return {'name':path.rsplit('/',1)[1]}
            raise DeliveryError('rejected','404',self.branch_error)
        if '/files' in path:return self.files[(int(path.rsplit('page=',1)[1])-1)*50:int(path.rsplit('page=',1)[1])*50]
        if '/git/commits/' in path:return {'created':self.commit_time,'commit':{'committer':{'date':self.commit_time}}}
        raise AssertionError(path)


def pr(state='open',merged=False,mergeable=True,sha='abc123',base='main',at='2026-11-14T00:00:00+08:00'):
    return {'state':state,'merged':merged,'mergeable':mergeable,'head':{'sha':sha},'base':{'ref':base},'html_url':'http://x/pr/1','created_at':at,'updated_at':at}


def st(*states):
    return [{'context':f'CI / job{i}','status':s,'description':s,'target_url':f'/r/actions/runs/{i}'} for i,s in enumerate(states)]

T0=1794585600.0  # 2026-11-14 00:00 +08:00
def at(minutes):return lambda:T0+minutes*60


class CiTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.t=Path(self.tmp.name).resolve();self.s=Store(self.t/'root')
        self.repo=self.t/'repo';self.repo.mkdir()
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'remote','add','origin','http://127.0.0.1:3000/test/demo.git'],check=True)
        from portable import board
        board.apply(self.s,[{'id':'demo','name':'演示','path':str(self.repo),'status_path':str(self.repo/'STATUS.md')}])
    def tearDown(self):self.s.db.close();self.tmp.cleanup()

    def test_manual_runs_are_not_counted(self):
        # 审核 11-14：手动运行分不清同一轮、全部任务，会把不同工作流、不同轮次拼成「通过」，所以不认
        skipped=[{'context':'CI / checks (pull_request)','status':'skipped','description':'','target_url':''}]
        api=FakeApi(pr(base='development'),skipped)
        api.tasks=[{'id':2,'name':'checks','head_sha':'abc123','event':'workflow_dispatch','status':'success','url':'http://x/runs/2'}]
        self.assertEqual(ci.check(self.s,'demo',1,api,now=at(1))['结论'],'被跳过')
        self.assertFalse(any('actions' in path for _,path in api.calls))

    def test_only_every_success_passes(self):
        cases=[
            (pr(),st('success','success'),'通过'),
            (pr(),st('skipped'),'被跳过'),          # Gitea 汇总会写 success，这里不认
            (pr(),st('success','skipped'),'被跳过'),
            (pr(),st('success','pending'),'没跑完'),
            (pr(),st('running'),'没跑完'),
            (pr(),st('success','failure'),'没通过'),
            (pr(),st('pending','failure'),'没通过'),
            (pr(),st('warning'),'没通过'),
            (pr(),[],'没有检查'),
            (pr(),[dict(st('success')[0],target_url='')],'没有执行记录'),
            (pr(mergeable=False),st('success'),'有冲突'),
            (pr(state='closed',merged=True),st('success'),'已合并'),
            (pr(state='closed'),st('success'),'已关闭'),
        ]
        for p,s,want in cases:
            with self.subTest(want=want,s=[x['status'] for x in s]):
                r=ci.check(self.s,'demo',1,api=FakeApi(p,s),now=at(3))
                self.assertEqual(r['结论'],want);self.assertEqual(r['能合并'],want=='通过')
        r=ci.check(self.s,'演示',1,api=FakeApi(pr(state='closed',merged=True),st('success','pending')),now=at(1))
        self.assertIn('合并时检查并没有全部通过',r['说明'])

    def test_wait_until_finished_not_until_merge(self):
        seq=iter([st('pending'),[],st('success','pending'),st('success','success')])
        slept=[]
        r=ci.wait(self.s,'demo',1,timeout=600,interval=5,api=FakeApi(pr(),lambda:next(seq)),sleep=slept.append,clock=lambda:0,now=at(1))
        self.assertEqual((r['结论'],len(slept)),('通过',3))
        now=[0]
        def tick(x):now[0]+=x
        r=ci.wait(self.s,'demo',1,timeout=30,interval=10,api=FakeApi(pr(),st('pending')),sleep=tick,clock=lambda:now[0])
        self.assertTrue(r['超时']);self.assertEqual(r['结论'],'没跑完')

    def test_no_run_after_minutes_is_reported_not_waited(self):
        # NewBI PR165：分支切自旧 main，CI 一直没来，空等了 4 小时。从第一次看到「没有检查」起计时（审核 11-14：
        # 提交时间可能很旧或在未来，PR 更新时间随便一改就重置，都不可靠）
        r=ci.check(self.s,'demo',1,api=FakeApi(pr(),[]),now=at(0))
        self.assertEqual(r['结论'],'没有检查');self.assertIn('0 分钟',r['说明'])
        self.assertEqual(ci.check(self.s,'demo',1,api=FakeApi(pr(),[]),now=at(9))['结论'],'没有检查')
        r=ci.check(self.s,'demo',1,api=FakeApi(pr(),[]),now=at(11))
        self.assertEqual(r['结论'],'没触发');self.assertIn('把目标分支合进这个分支',r['说明']);self.assertIn('11 分钟',r['说明'])
        # 新推的头提交重新计时；提交时间在未来也不会算出负数
        future=FakeApi(pr(sha='new1'),[]);future.commit_time='2027-01-01T00:00:00+08:00'
        r=ci.check(self.s,'demo',1,api=future,now=at(11));self.assertEqual(r['结论'],'没有检查');self.assertIn(' 0 分钟',r['说明'])
        self.assertEqual(ci.check(self.s,'demo',1,api=FakeApi(pr(sha='new1'),[]),now=at(30),no_run_minutes=30)['结论'],'没有检查')
        ticks=[0]
        def tick(x):ticks[0]+=x
        clock=lambda:ticks[0]
        r=ci.wait(self.s,'demo',1,timeout=3600,interval=60,api=FakeApi(pr(sha='w1'),[]),sleep=tick,clock=clock,now=lambda:T0+ticks[0])
        self.assertEqual(r['结论'],'没触发');self.assertLess(ticks[0],15*60)
        # 复审 11-14：不同站点上同名仓库、同号 PR、同头提交，各算各的
        other=FakeApi(pr(sha='abc123'),[])
        from unittest.mock import patch
        real=ci._identity
        with patch.object(ci,'_identity',lambda path:dict(real(path),origin='https://other.example')):
            r=ci.check(self.s,'demo',1,api=other,now=at(11))
        self.assertEqual(r['结论'],'没有检查');self.assertIn(' 0 分钟',r['说明'])
        # 0 秒的步骤正常：只看结果和执行记录，不看耗时
        zero=[{'context':'CI / lint','status':'success','description':'Successful in 0s','target_url':'/r/actions/runs/9'}]
        self.assertEqual(ci.check(self.s,'demo',1,api=FakeApi(pr(),zero),now=at(1))['结论'],'通过')

    def test_merge_needs_confirm_and_passing_checks(self):
        with self.assertRaises(Blocked):ci.merge(self.s,'demo',1,api=FakeApi(pr(),st('success')))
        with self.assertRaises(Blocked):ci.merge(self.s,'demo',1,confirm=True,api=FakeApi(pr(),st('success')))  # 没写风险档
        with self.assertRaises(Blocked):ci.merge(self.s,'demo',1,confirm=True,risk='高',api=FakeApi(pr(),st('success')))  # 高风险没写用户点头
        merged=[]
        r=ci.merge(self.s,'demo',1,confirm=True,risk='低',api=FakeApi(pr(),st('skipped')),merger=lambda *a:merged.append(a))
        self.assertEqual((r['结论'],r['本次合并'],merged),('被跳过',False,[]))
        state={'merged':False}
        def current():
            return pr(state='closed',merged=True)|{'merge_commit_sha':'m1'} if state['merged'] else pr(sha='abc123')
        def do(idn,number,sha):
            self.assertEqual((idn['repo'],number,sha),('test/demo',1,'abc123'));state['merged']=True
        r=ci.merge(self.s,'demo',1,confirm=True,risk='低',api=FakeApi(current,st('success','success')),merger=do)
        self.assertEqual((r['本次合并'],r['合并提交']),(True,'m1'))
        self.assertEqual((self.s.get(ci.LOG)[-1]['风险'],self.s.get(ci.LOG)[-1]['用户点头']),('低',''))
        log=self.s.get(ci.LOG)
        self.assertEqual((log[-1]['PR'],log[-1]['头提交'],log[-1]['检查']),(1,'abc123',['CI / job0=success','CI / job1=success']))

    def test_docs_only_rule(self):
        from portable.ci_docs_only import classify
        self.assertEqual(classify([('docs/说明.md','modified','')])[0],'docs')
        self.assertEqual(classify([('STATUS.md','modified',''),('功能清单.md','modified','')])[0],'docs')
        for files in ([('docs/x.py','added','')],[('portable/a.py','modified','')],[('docs/a.json','modified','')],
                      [('docs/new.md','renamed','portable/old.py')],[],[('docs/../x.md','added','')]):
            with self.subTest(files=files):self.assertEqual(classify(files)[0],'full')
        self.assertEqual(classify([('docs/data.csv','modified','')],exclude=['docs/data.csv'])[0],'full')  # 人工名单
        self.assertEqual(classify([('docs/data.csv','modified','')],referenced=lambda p:True)[0],'full')  # 被程序或测试引用
        self.assertEqual(classify([('docs/避坑.md','modified','')],referenced=lambda p:True)[0],'docs')  # 项目记忆文件不做引用搜索
        self.assertEqual(classify([('docs/x.md','modified','')],referenced=lambda p:1/0)[0],'full')  # 搜索出错按全量
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        self.assertEqual((root/'portable/ci_docs_only.py').read_bytes(),(root/'tools/ci/docs_only.py').read_bytes())  # 原件和 CI 用的那份字面一致

    def test_docs_only_cli_on_real_git(self):
        import os,sys,subprocess,tempfile
        from pathlib import Path
        script=Path(__file__).resolve().parents[1]/'tools/ci/docs_only.py'
        with tempfile.TemporaryDirectory() as d:
            g=lambda *a:subprocess.run(['git','-C',d,'-c','user.name=t','-c','user.email=t@t',*a],check=True,capture_output=True)
            g('init','-q','-b','main');Path(d,'src').mkdir();Path(d,'src/x.py').write_text("open('docs/data.csv')\n")
            Path(d,'docs').mkdir();Path(d,'docs/data.csv').write_text('a\n');Path(d,'docs/说明.md').write_text('a\n')
            g('add','.');g('commit','-qm','base');g('branch','base')
            run=lambda *a:subprocess.run([sys.executable,str(script),*a],cwd=d,capture_output=True,text=True).stdout.strip()
            Path(d,'docs/说明.md').write_text('b\n');g('commit','-qam','doc')
            self.assertEqual(run('--base','base'),'mode=docs')
            Path(d,'docs/data.csv').write_text('b\n');g('commit','-qam','csv')
            self.assertEqual(run('--base','base'),'mode=full')  # data.csv 被 src/x.py 读：走全量
            self.assertEqual(run(),'mode=full')  # 不是 PR（没基线）：全量
            self.assertEqual(run('--base','不存在的分支'),'mode=full')  # 读不到改动：全量

    def test_merge_reconciles_ci_mode_when_repo_has_docs_gate(self):
        def merged_api(files):
            state={'merged':False}
            api=FakeApi(lambda:pr(state='closed',merged=True) if state['merged'] else pr(),st('success'))
            api.files=files;api.docs_gate=True
            return api,state
        code=[{'filename':'src/a.py','status':'changed'}];docs=[{'filename':'docs/a.md','status':'changed'}]
        for files,log,ok in ((code,'随身开发CI模式=docs',False),(code,'',False),(code,'随身开发CI模式=full',True),(docs,'随身开发CI模式=docs',True),(docs,'随身开发CI模式=full',True)):
            with self.subTest(files=files[0]['filename'],log=log):
                api,state=merged_api(files)
                run=lambda:ci.merge(self.s,'demo',1,confirm=True,risk='低',api=api,merger=lambda *a:state.update(merged=True),log_reader=lambda idn,link:log)
                if ok:
                    r=run();self.assertTrue(r['本次合并'])
                    self.assertEqual(r['类别'],'代码类' if files is code else '文档类')
                    self.assertEqual('提醒' in r,files is code)
                else:
                    with self.assertRaises(Blocked):run()
                    self.assertFalse(state['merged'])
        # 真实日志格式：先回显命令（带引号），再是输出行；只算输出那一行（11-16 验证 PR108 实测格式）
        real_log='2026-11-16T00:00:01Z ##[group]Run echo "随身开发CI模式=full"\n2026-11-16T00:00:01Z echo "随身开发CI模式=${{ steps.mode.outputs.mode || \'full\' }}"\n2026-11-16T00:00:01Z 随身开发CI模式=full\n'
        api,state=merged_api(code)
        r=ci.merge(self.s,'demo',1,confirm=True,risk='低',api=api,merger=lambda *a:state.update(merged=True),log_reader=lambda idn,link:real_log)
        self.assertTrue(r['本次合并']);self.assertEqual(r['CI模式'],'full')
        # 没接轻量 CI 的仓库：不读日志，照旧合
        api=FakeApi(lambda:pr(state='closed',merged=True) if api.done else pr(),st('success'));api.done=False;api.files=code
        r=ci.merge(self.s,'demo',1,confirm=True,risk='低',api=api,merger=lambda *a:setattr(api,'done',True),log_reader=lambda idn,link:self.fail('不该读日志'))
        self.assertEqual(r['CI模式'],'未接轻量CI')
        # 改判定脚本的 PR 不能按低风险合
        api=FakeApi(pr(),st('success'));api.files=[{'filename':'tools/ci/docs_only.py','status':'changed'}]
        with self.assertRaises(Blocked) as e:ci.merge(self.s,'demo',1,confirm=True,risk='低',api=api,merger=lambda *a:None)
        self.assertIn('CI 判定脚本',str(e.exception))

    def _release_fixture(self, mixed_source=False, moved_progress=False, merged_at='2026-11-20T11:08:37+08:00'):
        def git(*args, committer_date=None):
            return subprocess.run(
                ['git', '-C', str(self.repo), '-c', 'user.name=test', '-c', 'user.email=test@example.com', *args],
                capture_output=True, text=True, check=True,
                env=dict(os.environ, GIT_COMMITTER_DATE=committer_date) if committer_date else None,
            ).stdout.strip()

        git('checkout', '-qb', 'main')
        progress = self.repo / 'STATUS.md'
        initial_progress = progress
        if moved_progress:
            initial_progress = self.repo / 'docs' / 'progress.md'
            initial_progress.parent.mkdir()
        initial_progress.write_text('Before the release')
        git('add', '.')
        git('commit', '-qm', 'Previous release')
        git('branch', 'development')
        git('commit', '--allow-empty', '-qm', 'Previous release merge on main')
        base = git('rev-parse', 'HEAD')
        records = {}
        repo = {'full_name': 'test/demo'}

        def source_pr(number, branch, edit):
            git('checkout', '-qb', branch, 'development')
            edit()
            git('add', '.')
            git('commit', '-qm', branch)
            source = git('rev-parse', 'HEAD')
            git('checkout', '-q', 'development')
            git('merge', '--no-ff', '-qm',
                f"Merge pull request '{branch}' (#{number}) from test/{branch} into development",
                source, committer_date=merged_at)
            records[number] = {'merged': True, 'merge_commit_sha': git('rev-parse', 'HEAD'),
                               'base': {'ref': 'development', 'repo': repo}, 'head': {'sha': source},
                               'merged_at': merged_at}

        def feature():
            (self.repo / 'app.py').write_text('print(1)')
            if mixed_source:
                progress.write_text('Feature progress')
            if moved_progress:
                initial_progress.rename(progress)
        source_pr(2, 'feature', feature)
        source_pr(3, 'progress', lambda: progress.write_text('Release progress'))
        head = git('rev-parse', 'HEAD')
        git('commit', '--allow-empty', '-qm', 'Later work on development')
        development = git('rev-parse', 'HEAD')
        release = dict(pr(sha=head), base={'ref': 'main', 'sha': base, 'repo': repo})
        files = [{'filename': 'app.py', 'status': 'added'}, {'filename': 'STATUS.md', 'status': 'changed'}]
        merged_sha = 'f' * 40
        checks = st('success', 'success')
        for job_id, check_row in enumerate(checks, 20):
            check_row['target_url'] = f'/test/demo/actions/runs/10/jobs/{job_id}'

        assert_equal = self.assertEqual

        class ReleaseApi(FakeApi):
            def __call__(self, idn, method, path, payload=None):
                assert_equal(method, 'GET')
                if path.endswith('/branches/development'):
                    return {'commit': {'id': self.development}}
                if path.endswith(f'/git/commits/{merged_sha}'):
                    return {'parents': [{'sha': parent} for parent in self.merged_parents]}
                if path.endswith('/actions/runs/10'):
                    return {'head_sha': head, 'event': 'pull_request', 'status': 'completed', 'conclusion': 'success',
                            'pull_requests': [{'number': self.ci_pr, 'base': {'ref': 'main'}}]}
                if path.endswith('/actions/runs/10/jobs?limit=100'):
                    return {'jobs': [{'id': job_id, 'run_id': 10, 'head_sha': head,
                                      'status': 'completed', 'conclusion': 'success'} for job_id in (20, 21)]}
                if '/pulls/' in path and '/files' not in path:
                    number = int(path.rsplit('/', 1)[1])
                    if number != 1:
                        return records[number]
                    self.release_reads += 1
                    if self.move_base and self.release_reads >= 3:
                        return dict(self.pr, base=dict(self.pr['base'], sha=development))
                return super().__call__(idn, method, path, payload)

        api = ReleaseApi(release, checks)
        api.ci_pr = 1
        api.merged_sha = merged_sha
        api.merged_parents = [base, head]
        api.files = files
        api.docs_gate = True
        api.development = development
        api.release_reads = 0
        api.move_base = False
        return api, head, records

    def test_release_batch_checks_constituents_at_pinned_head(self):
        api, head, records = self._release_fixture()
        records[2]['head']['sha'] = 'e' * 40  # A merged PR's source branch can move.
        log_reader = lambda *_: ci.MODE_MARK + 'full'
        calls = []

        def merge_stub(idn, number, sha):
            calls.append((number, sha))
            api.pr.update(state='closed', merged=True, merge_commit_sha=api.merged_sha)

        options = dict(confirm=True, risk='高', approval='Approved fixed head', api=api,
                       log_reader=log_reader, merger=merge_stub)
        with self.assertRaisesRegex(Blocked, '同时改了代码和进度表'):
            ci.merge(self.s, 'demo', 1, **options)
        self.assertEqual(calls, [])
        preview = ci.release_check(self.s, 'demo', 1, head, api, log_reader)
        self.assertEqual((preview['来源PR'], preview['CI模式']), ([2, 3], 'full'))
        self.assertEqual(self.s.get(ci.LOG, []), [])
        result = ci.merge(self.s, 'demo', 1, release_head=head, **options)
        self.assertEqual((result['来源PR'], calls), ([2, 3], [(1, head)]))
        self.assertEqual(self.s.get(ci.LOG)[-1]['来源PR'], [2, 3])

    def test_release_batch_keeps_confirm_approval_pin_and_full_ci(self):
        api, head, _ = self._release_fixture()
        log_reader = lambda *_: ci.MODE_MARK + 'full'
        options = dict(confirm=True, risk='高', approval='Approved fixed head',
                       api=api, release_head=head, log_reader=log_reader,
                       merger=lambda *args: self.fail('invalid release attempted to merge'))
        for changed, error in (
            ({'confirm': False}, '--confirm'),
            ({'risk': '低'}, '--risk 高'),
            ({'approval': ''}, '--approval'),
            ({'release_head': head[:8]}, '完整'),
            ({'release_head': 'a' * 40}, '头提交'),
            ({'log_reader': lambda *_: ci.MODE_MARK + 'docs'}, '全量 CI'),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(Blocked, error):
                ci.merge(self.s, 'demo', 1, **(options | changed))
        api.ci_pr = 2
        with self.assertRaisesRegex(Blocked, '固定头的成功 CI'):
            ci.merge(self.s, 'demo', 1, **options)
        api.statuses = st('success', 'skipped')
        self.assertEqual(ci.merge(self.s, 'demo', 1, **options)['结论'], '被跳过')

    def test_release_batch_refuses_invalid_provenance_and_moving_base(self):
        api, head, records = self._release_fixture()
        log_reader = lambda *_: ci.MODE_MARK + 'full'
        development = api.development
        api.development = records[2]['merge_commit_sha']
        with self.assertRaisesRegex(Blocked, '第一父链'):
            ci.release_check(self.s, 'demo', 1, head, api, log_reader)
        api.development = development
        records[3]['merged'] = False
        with self.assertRaisesRegex(Blocked, '合并记录'):
            ci.release_check(self.s, 'demo', 1, head, api, log_reader)
        records[3]['merged'] = True
        api.pr['base']['ref'] = 'master'
        with self.assertRaisesRegex(Blocked, '本仓库 main'):
            ci.release_check(self.s, 'demo', 1, head, api, log_reader)
        api.pr['base']['ref'] = 'main'
        api.release_reads = 0
        api.move_base = True
        with self.assertRaisesRegex(Blocked, '目标分支已更新'):
            ci.release_check(self.s, 'demo', 1, head, api, log_reader)

    def test_release_batch_refuses_mixed_source_pr(self):
        api, head, _ = self._release_fixture(mixed_source=True)
        with self.assertRaisesRegex(Blocked, '来源 PR #2'):
            ci.release_check(self.s, 'demo', 1, head, api, lambda *_: ci.MODE_MARK + 'full')

    def test_release_batch_lists_only_mixed_sources_merged_before_rule(self):
        api, head, records = self._release_fixture(mixed_source=True, merged_at='2026-11-20T11:08:35+08:00')
        records[2]['merged_at'] = '2026-11-20T03:08:35Z'
        result = ci.release_check(self.s, 'demo', 1, head, api, lambda *_: ci.MODE_MARK + 'full')
        self.assertEqual(result['结论'], ci.PASS)
        self.assertEqual(result['来源PR'], [2, 3])
        self.assertEqual(result['规则生效前合入未查'], [2])

    def test_release_batch_requires_server_time_before_rule_even_with_backdated_commit(self):
        api, head, records = self._release_fixture(mixed_source=True, merged_at='2026-11-20T03:08:35Z')
        log_reader = lambda *_: ci.MODE_MARK + 'full'
        del records[2]['merged_at']
        with self.assertRaisesRegex(Blocked, '来源 PR #2 未通过进度表检查'):
            ci.release_check(self.s, 'demo', 1, head, api, log_reader)
        for merged_at in ('2026-11-20T03:08:37Z', '2026-11-20T03:08:36Z', 'invalid', '2026-11-19T19:53:15'):
            records[2]['merged_at'] = merged_at
            with self.subTest(merged_at=merged_at), self.assertRaisesRegex(Blocked, '来源 PR #2 未通过进度表检查'):
                ci.release_check(self.s, 'demo', 1, head, api, log_reader)

    def test_release_batch_refuses_exemption_without_valid_cutoff(self):
        api, head, _ = self._release_fixture(mixed_source=True, merged_at='2026-11-20T03:08:35Z')
        for protocol in ({}, {'release_batch': {}}, {'release_batch': {'progress_rule_effective_at': 'invalid'}}):
            with self.subTest(protocol=protocol), patch.object(ci, 'PROTOCOL', protocol):
                with self.assertRaisesRegex(Blocked, '来源 PR #2 未通过进度表检查'):
                    ci.release_check(self.s, 'demo', 1, head, api, lambda *_: ci.MODE_MARK + 'full')

    def test_progress_with_docs_only_is_not_mixed(self):
        files = [('STATUS.md', 'modified', ''), ('功能清单.md', 'modified', ''),
                 ('docs/避坑.md', 'modified', ''), ('docs/appendix.csv', 'added', '')]
        ci.check_mixed_progress_pr(files)
        with self.assertRaisesRegex(Blocked, '同时改了代码和进度表'):
            ci.check_mixed_progress_pr(files + [('docs/example.py', 'added', '')])

    def test_release_batch_rename_is_not_the_first_progress_file_exception(self):
        api, head, records = self._release_fixture(moved_progress=True, merged_at='2026-11-20T03:08:36+00:00')
        records[2]['merged_at'] = '2026-11-20T03:08:35Z'
        with self.assertRaisesRegex(Blocked, '来源 PR #2'):
            ci.release_check(self.s, 'demo', 1, head, api, lambda *_: ci.MODE_MARK + 'full')

    def test_release_merge_readback_refuses_unexpected_target_commit(self):
        api, head, _ = self._release_fixture()

        def merger(idn, number, sha):
            api.pr.update(state='closed', merged=True, merge_commit_sha=api.merged_sha)
            api.merged_parents = ['e' * 40, sha]

        with self.assertRaisesRegex(Blocked, '合并父提交与核验时不同'):
            ci.merge(self.s, 'demo', 1, confirm=True, risk='高', approval='Approved fixed head',
                     api=api, release_head=head, log_reader=lambda *_: ci.MODE_MARK + 'full', merger=merger)
        self.assertEqual(self.s.get(ci.LOG, []), [])

    def test_merge_not_confirmed_by_readback_is_reported(self):
        with self.assertRaises(Blocked) as e:
            ci.merge(self.s,'demo',1,confirm=True,risk='低',api=FakeApi(pr(),st('success')),merger=lambda *a:None)
        self.assertIn('不要直接重发',str(e.exception))

    def test_low_risk_merge_refuses_ci_rule_gate_and_test_deletion(self):
        # 审核 11-14：低风险自己合时，程序再查一遍改动文件，碰 CI、规矩、合并把关或删测试的不让按低风险合
        cases=[('.gitea/workflows/ci.yml','changed','CI 配置'),('portable/protocol.json','changed','规矩'),('tools/gate/test_census.py','changed','合并把关'),
               ('tests/test_x.py','deleted','删测试文件'),('src/tests/old_test.go','removed','删测试文件')]
        for name,status,what in cases:
            api=FakeApi(pr(),st('success'));api.files=[{'filename':'src/a.py','status':'changed'},{'filename':name,'status':status}]
            merged=[]
            with self.assertRaises(Blocked) as e:ci.merge(self.s,'demo',1,confirm=True,risk='低',api=api,merger=lambda *a:merged.append(a))
            self.assertIn(what,str(e.exception));self.assertEqual(merged,[])
        # 文件多于一页也要查到
        api=FakeApi(pr(),st('success'));api.files=[{'filename':f'src/f{i}.py','status':'changed'} for i in range(60)]+[{'filename':'.github/workflows/x.yml','status':'added'}]
        with self.assertRaises(Blocked):ci.merge(self.s,'demo',1,confirm=True,risk='低',api=api,merger=lambda *a:None)
        # 复审 11-14：改名把高风险文件挪走、把测试文件改成别的，都要拦
        for old,new,what in (('.github/workflows/ci.yml','archive/ci.yml','CI 配置'),('tests/test_auth.py','docs/example.py','测试文件改名'),
                             ('tests/test_auth.py','tests/auth_test.py','测试文件改名'),('tests/test_auth.py','docs/test_auth.py','测试文件改名')):
            api=FakeApi(pr(),st('success'));api.files=[{'filename':new,'status':'renamed','previous_filename':old}]
            with self.assertRaises(Blocked) as e:ci.merge(self.s,'demo',1,confirm=True,risk='低',api=api,merger=lambda *a:None)
            self.assertIn(what,str(e.exception))
        # 复审 11-14：翻到页数上限还没读完，不拿半份清单放行
        from unittest.mock import patch
        api=FakeApi(pr(),st('success'));api.files=[{'filename':f'src/f{i}.py','status':'changed'} for i in range(100)]
        with patch.object(ci,'MAX_FILE_PAGES',2):
            with self.assertRaises(Blocked) as e:ci.merge(self.s,'demo',1,confirm=True,risk='低',api=api,merger=lambda *a:None)
        self.assertIn('没法确认全部读完',str(e.exception))
        # 复审 11-14：仓库有 development 分支时，合进 main 不能报低风险
        api=FakeApi(pr(base='main'),st('success'));api.branches={'development'}
        with self.assertRaises(Blocked) as e:ci.merge(self.s,'demo',1,confirm=True,risk='低',api=api,merger=lambda *a:None)
        self.assertIn('development 分支',str(e.exception))
        # 第三轮复审：认证、权限、限流失败不能当成「没有 development 分支」
        for code in (401,403,429,None):
            api=FakeApi(pr(base='main'),st('success'));api.branch_error=code;merged=[]
            with self.assertRaises(Blocked) as e:ci.merge(self.s,'demo',1,confirm=True,risk='低',api=api,merger=lambda *a:merged.append(a))
            self.assertIn('查不到这个仓库有没有',str(e.exception));self.assertEqual(merged,[])
        # 明确 404（真没有 development）才按普通低风险继续
        state3={'merged':False}
        api=FakeApi(lambda:pr(state='closed',merged=True) if state3['merged'] else pr(base='main'),st('success'))
        self.assertTrue(ci.merge(self.s,'demo',1,confirm=True,risk='低',api=api,merger=lambda *a:state3.update(merged=True))['本次合并'])
        state2={'merged':False}
        api=FakeApi(lambda:pr(state='closed',merged=True) if state2['merged'] else pr(base='development'),st('success'));api.branches={'development'}
        self.assertTrue(ci.merge(self.s,'demo',1,confirm=True,risk='低',api=api,merger=lambda *a:state2.update(merged=True))['本次合并'])
        # 高风险 + 用户点头出处：不查文件也能合，并记下出处
        state={'merged':False}
        api=FakeApi(lambda:pr(state='closed',merged=True) if state['merged'] else pr(),st('success'))
        api.files=[{'filename':'portable/protocol.json','status':'changed'}]
        r=ci.merge(self.s,'demo',1,confirm=True,risk='高',approval='用户 11-14「合」',api=api,merger=lambda *a:state.update(merged=True))
        self.assertTrue(r['本次合并']);self.assertEqual(self.s.get(ci.LOG)[-1]['用户点头'],'用户 11-14「合」')

    def test_merge_redacts_approval_and_existing_ledger(self):
        sample="password='abc12345'"
        self.s.put(ci.LOG,[{'时间':'2026-11-01','用户点头':sample,'风险':'低'}])
        state={'merged':False}
        api=FakeApi(lambda:pr(state='closed',merged=True) if state['merged'] else pr(),st('success'))
        r=ci.merge(self.s,'demo',1,confirm=True,risk='低',approval='用户点头 '+sample,api=api,merger=lambda *a:state.update(merged=True))
        self.assertTrue(r['本次合并'])
        text=json.dumps(self.s.get(ci.LOG),ensure_ascii=False)
        self.assertIn('[凭据已脱敏]',text)
        self.assertNotIn('abc12345',text)
        self.assertEqual(len(self.s.get(ci.LOG)),2)

    def test_missing_pr_says_so_and_lists_recent(self):
        from portable.remote_publish import DeliveryError
        def api(idn,method,path,payload=None):
            if path.endswith('/pulls/999'):raise DeliveryError('rejected','既有 Gitea API 查询不可用',404)
            if '/pulls?' in path:return [{'number':n} for n in (115,116,114)]
            raise AssertionError(path)
        with self.assertRaises(Blocked) as e:ci.check(self.s,'demo',999,api=api)
        self.assertIn('没有这个 PR',str(e.exception));self.assertIn('116、115、114',str(e.exception))
        def api_down(idn,method,path,payload=None):raise DeliveryError('sent_unknown','既有 Gitea API 查询不可用',502)
        with self.assertRaises(DeliveryError):ci.check(self.s,'demo',999,api=api_down)  # 不是 404 的照旧报原错，不误说成「没有这个 PR」

    def test_project_without_remote_or_unknown(self):
        with self.assertRaises(Blocked):ci.check(self.s,'没有这个',1,api=FakeApi(pr(),st('success')))
        subprocess.run(['git','-C',str(self.repo),'remote','remove','origin'],check=True)
        with self.assertRaises(Blocked) as e:ci.check(self.s,'demo',1,api=FakeApi(pr(),st('success')))
        self.assertIn('本地全量测试',str(e.exception))

    def test_cli_requires_project_and_pr(self):
        import sys
        out=subprocess.run([sys.executable,'-m','portable','--root',str(self.s.root),'ci','check'],capture_output=True,text=True,cwd=Path(__file__).resolve().parents[1])
        self.assertIn('要写项目和 PR 号',out.stdout+out.stderr)

if __name__=='__main__':unittest.main()
