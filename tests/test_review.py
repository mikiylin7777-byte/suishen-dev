"""复盘工具 review：白名单、两道密钥拦截、无工具复盘方、格式作废、锁、超时、存档留底和每周体检。
全部用临时目录和本地 git；不调真实 AI，也不调真实 gitleaks（都换成注入的替身）。"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from portable.core import Blocked, Store
from portable import board, privacy, review

FAKE_TOKEN='ghp_'+'Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0FeDcBa'
FAKE_DB='postgres://app_admin:'+'Tr0ub4dor-Horse'+'@db.internal:5432/orders'


def git(repo, *args, date=None):
    env = dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date) if date else None
    subprocess.run(['git','-C',str(repo),'-c','user.name=t','-c','user.email=t@t',*args],check=True,capture_output=True,env=env)


def diff_block(path, lines):
    header = f'diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n'
    return header + ''.join(f'+{line}\n' for line in lines)


def answer(**over):
    value={'把握分':42,'原因':[{'说明':'边界没处理','位置':'src/app.py:3'}],'建议做法':'先补边界测试','严重问题':[],'避坑':'改边界时｜漏了空列表｜先写空列表测试'}
    value.update(over);return value


def stream(result,tools=(),mcp=(),model='claude-sonnet-5-5',is_error=False):
    return '\n'.join([json.dumps({'type':'system','subtype':'init','tools':list(tools),'mcp_servers':list(mcp),'model':model}),
                      json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'…'}]}}),
                      json.dumps({'type':'result','subtype':'success','is_error':is_error,'result':result})])+'\n'


class FakeRunner:
    def __init__(self,stdout='',code=0,error=None):
        self.stdout=stdout;self.code=code;self.error=error;self.calls=[]
    def __call__(self,argv,text,cwd,timeout):
        self.calls.append({'argv':argv,'text':text,'cwd':cwd,'timeout':timeout,'cwd_files':sorted(Path(cwd).iterdir())})
        if self.error:raise self.error
        return self.code,self.stdout,''


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.t=Path(self.tmp.name).resolve()
        home=patch.dict(os.environ,{'PORTABLE_HOME':str(self.t/'home')});home.start();self.addCleanup(home.stop)  # 不读真实的 ~/归档
        self.s=Store(self.t/'随身开发')
        self.repo=self.t/'项目';self.repo.mkdir()
        git(self.repo,'init','-q','-b','main')
        (self.repo/'STATUS.md').write_text('---\n项目: 玩具\n长期目标: 完成 1 块 / 共 2 块；本期：求和；卡在：无\n下一步: 修空列表\n卡在: 无\n---\n')
        (self.repo/'src').mkdir();(self.repo/'src/app.py').write_text('def total(xs):\n    return sum(xs)\n')
        # 每周体检按提交时间计算分支年龄，必须与测试传入的日期一起固定。
        commit_date = '2026-11-15T00:00:00+00:00'
        git(self.repo,'add','.');git(self.repo,'commit','-qm','init',date=commit_date)
        git(self.repo,'switch','-qc','feat')
        (self.repo/'src/app.py').write_text('def total(xs):\n    return xs[0]+sum(xs[1:])\n')
        git(self.repo,'commit','-qam','change',date=commit_date)
        self.note=self.t/'说明.md';self.note.write_text('目标：求和。试过：两轮。卡在：空列表报错。\n')
        self.good=FakeRunner(stream(json.dumps(answer(),ensure_ascii=False)))
    def tearDown(self):self.tmp.cleanup()

    def run_review(self,runner=None,kind='卡住',gitleaks=lambda d:[],**kw):
        kw.setdefault('base','main')
        return review.review(self.s,str(self.repo),kind,str(self.note),runner=runner or self.good,gitleaks=gitleaks,claude='/bin/claude-fake',now=kw.pop('now',time.time),**kw)

    def archives(self):
        base=self.s.root/review.ARCHIVE
        return sorted(d for d in base.iterdir() if not d.name.startswith('.')) if base.is_dir() else []

    def test_whitelist_keeps_only_code_and_docs(self):
        for path,reason in (('src/app.py',None),('README.md',None),('docs/说明.txt',None),('Makefile',None),('db/migrate/001_add.sql',None),
                            ('.env',"环境配置"),('.env.production','环境配置'),('config/app.toml','配置文件'),('setup.ini','配置文件'),
                            ('certs/server.pem','密钥文件'),('deploy/id.key','密钥文件'),('secrets/prod.py','密钥类路径'),('app/credentials.md','密钥类路径'),
                            ('backup/orders_dump.sql','数据库导出'),('data/app.sqlite3','数据库导出'),('logs/run.txt','日志或聊天记录'),
                            ('package.json','不是代码或文档'),('settings.yaml','不是代码或文档')):
            with self.subTest(path=path):self.assertEqual(review.classify(path),reason)
        self.assertEqual(review.classify('assets/logo.md',binary=True),'二进制')
        self.assertEqual(review.classify('db/big.sql',added=5000),'数据库导出')
        # 实际改动：白名单以外的文件进不了包，也不出现在发出的文字里
        (self.repo/'.env').write_text('DEBUG=1\n');(self.repo/'app.toml').write_text('name="x"\n')
        (self.repo/'secrets').mkdir();(self.repo/'secrets/k.py').write_text('x=1\n')
        (self.repo/'logo.png').write_bytes(bytes(range(256)))
        (self.repo/'orders_dump.sql').write_text('INSERT INTO t VALUES (1);\n')
        (self.repo/'README.md').write_text('# 玩具\n')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','more')
        result=self.run_review()
        excluded={e['文件']:e['原因'] for e in result['没进包的文件']}
        self.assertEqual(excluded,{'.env':'环境配置','app.toml':'配置文件','secrets/k.py':'密钥类路径','logo.png':'二进制','orders_dump.sql':'数据库导出'})
        sent=self.good.calls[0]['text']
        self.assertIn('src/app.py',sent);self.assertIn('README.md',sent)
        for name in ('.env','app.toml','secrets/k.py','logo.png','orders_dump.sql','DEBUG=1','INSERT INTO'):self.assertNotIn(name,sent)
        record=json.loads((Path(result['存档'])/'发送记录.json').read_text())
        self.assertEqual(record['进包的改动文件'],['README.md','src/app.py'])

    def test_package_has_status_goals_and_test_log_tail(self):
        log=self.t/'测试.txt';log.write_text(''.join(f'line {i}\n' for i in range(500)))
        self.run_review(test_log=str(log))
        sent=self.good.calls[0]['text']
        self.assertIn('长期目标：完成 1 块 / 共 2 块',sent);self.assertIn('下一步：修空列表',sent);self.assertNotIn('目标：（没写）',sent)
        self.assertIn('line 499',sent);self.assertIn('line 300',sent);self.assertNotIn('line 299\n',sent)
        self.assertIn('只取最后 200 行',sent)

    def assert_refused(self,gitleaks,where):
        runner=FakeRunner(stream(json.dumps(answer(),ensure_ascii=False)))
        with self.assertRaises(Blocked) as caught:self.run_review(runner=runner,gitleaks=gitleaks)
        message=str(caught.exception)
        self.assertIn('整包不发',message);self.assertIn(where,message)
        self.assertNotIn(FAKE_TOKEN,message);self.assertNotIn('Tr0ub4dor',message)
        self.assertEqual(runner.calls,[]);self.assertEqual(self.archives(),[])  # 什么都没发，也不留含密钥的包
        return message

    def test_fake_token_refuses_whole_package(self):
        (self.repo/'src/cfg.py').write_text('x = 1\nGITHUB = "'+FAKE_TOKEN+'"\n')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','token')
        def leaks(directory):
            text=(Path(directory)/'改动.diff').read_text()
            return [('改动.diff',n,'github-pat') for n,line in enumerate(text.splitlines(),1) if FAKE_TOKEN in line]
        message=self.assert_refused(leaks,'改动.diff 第')
        self.assertIn('来自 src/cfg.py',message);self.assertIn('gitleaks github-pat',message);self.assertIn('自带去密钥规则',message)

    def test_review_accepts_source_references_and_short_test_value(self):
        lines = (
            "    authorization = load_authorization(path, mode=mode, operation='migrate', database=database, report_name_mismatch=True)",
            "wizard.run_wizard(self.s,{'password':'x'})",
            "authorization = settings.authorization_token",
            "token = token_from_configuration",
            'authorization = "load_authorization"',
        )
        (self.repo/'src/cfg.py').write_text('\n'.join(lines)+'\n')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','credential references')
        self.run_review()
        self.assertEqual(len(self.good.calls),1)
        for line in lines:
            self.assertIn(line,self.good.calls[0]['text'])

    def test_diff_keeps_credential_checks_when_gitleaks_misses(self):
        lines = (
            'Authorization: Bearer abc123',
            'Authorization: Basic dTpw',
            'authorization="sk-'+'Zx9Yw8Vu7Ts6Rq5Po4Nm"',
            'authorization="ZXy42aN83Mqi5VoL"',
            "password='S3cr3t!x9'",
            'token = "'+FAKE_TOKEN+'"',
        )
        patch_text = diff_block('src/config.py',lines)
        hits = review.scan(self.t,{'改动.diff':patch_text},gitleaks=lambda _:[])
        self.assertEqual([h['行'] for h in hits],list(range(5,len(lines)+5)))

    def test_bare_credentials_are_blocked_in_source_and_config_blocks(self):
        lines = (
            'PASSWORD=hunter2hunter2',
            'PASSWORD = hunter2hunter2',
            'password: hunter2hunter2',
            'api_key=abcdefghijklmnop1234',
            '密码：abc123456',
            'cookie: sessionid=abcdef123456',
            'authorization=ZXy42aN83Mqi5VoLzz',
        )
        patch_text = diff_block('src/config.py',lines) + diff_block('.env',lines)
        hits = review.scan(self.t,{'改动.diff':patch_text},gitleaks=lambda _:[])
        self.assertEqual(
            [(hit['来自'],hit['行']) for hit in hits],
            [('src/config.py',n) for n in range(5,12)] + [('.env',n) for n in range(16,23)],
        )

    def test_source_exemptions_are_scoped_to_supported_diff_blocks(self):
        line = "wizard.run_wizard(self.s,{'password':'x'})"
        sources = ('example.py','example.js','example.ts')
        strict = ('.env','example.yaml','example.yml','example.json','example.ini',
                  'example.toml','example.md','example.sh')
        patch_text = ''.join(diff_block(path,[line]) for path in sources + strict)
        patch_text += 'diff --git a/.env b/pretend.py\n@@ -0,0 +1 @@\n+' + line + '\n'
        hits = review.scan(self.t,{'改动.diff':patch_text},gitleaks=lambda _:[])
        self.assertEqual([hit.get('来自') for hit in hits],list(strict)+[None])

    def test_source_call_does_not_hide_another_assignment_on_same_line(self):
        line = "authorization = load_authorization(password='S3cr3t!x9')"
        patch_text = diff_block('example.py',[line])
        hits = review.scan(self.t,{'改动.diff':patch_text},gitleaks=lambda _:[])
        self.assertEqual([hit['行'] for hit in hits],[5])

    def test_token_scheme_and_prefixed_keys_are_scanned_and_redacted(self):
        cases = (
            ('Authorization','Token zxY92aV84bQ76mn5'),
            ('aws_secret_access_key','aB1cD2eF3gH4iJ5k'),
            ('DB_PASSWORD','s0m3D8Credential'),
        )
        scanner = privacy.CredentialContent()
        for key,value in cases:
            with self.subTest(key=key):
                line = f'{key}: {value}'
                self.assertTrue(scanner.search(line))
                self.assertTrue(scanner.search(line,source_code=True))
                self.assertNotIn(value.split()[-1],privacy.redact_text(line))
                self.assertEqual(privacy.sanitize({key:value}),{key:privacy.REDACTED})

    def test_notes_and_strict_scanner_still_flag_short_passwords(self):
        text = "{'password':'x'}"
        self.assertTrue(privacy.CredentialContent().search(text))
        self.note.write_text(text)
        self.assert_refused(lambda _:[],'说明.md')

    def test_empty_quoted_password_is_not_a_secret(self):
        scanner = privacy.CredentialContent()
        for text in ('ERP_DB_PASSWORD=""', '"password": ""', "password=''"):
            with self.subTest(text=text):
                self.assertFalse(scanner.search(text))
        for text in ('ERP_DB_PASSWORD="abc12345"', "password='x'"):
            with self.subTest(text=text):
                self.assertTrue(scanner.search(text))

    def test_db_connection_string_refuses_even_if_gitleaks_misses_it(self):
        (self.repo/'src/db.py').write_text('URL = "'+FAKE_DB+'"\n')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','db')
        message=self.assert_refused(lambda d:[],'改动.diff 第')
        self.assertIn('来自 src/db.py',message);self.assertNotIn('gitleaks',message)

    def test_secret_in_note_refuses_too(self):
        self.note.write_text('连不上库，连接串是 '+FAKE_DB+'\n')
        self.assertIn('说明.md 第 1 行',self.assert_refused(lambda d:[],'说明.md'))

    def test_lock_file_removed_after_run(self):
        from portable.review import _Lock
        path=self.t/'锁目录'/'x.lock'
        with _Lock(path):self.assertTrue(path.exists())
        self.assertFalse(path.exists())  # 跑完删掉，免得备份把它当成活动锁

    def test_gitleaks_missing_is_blocked_not_skipped(self):
        runner=FakeRunner(stream('{}'))
        with patch.object(review.shutil,'which',return_value=None),patch.object(review,'GITLEAKS_PATHS',(str(self.t/'没有'),)):
            with self.assertRaises(Blocked) as caught:
                review.review(self.s,str(self.repo),'卡住',str(self.note),base='main',runner=runner,claude='/bin/claude-fake')
        self.assertIn('没装 gitleaks',str(caught.exception));self.assertEqual(runner.calls,[]);self.assertEqual(self.archives(),[])

    def test_reviewer_is_toolless_claude_with_different_model(self):
        model,flags=review.reviewer('claude');self.assertEqual(model,'sonnet')
        self.assertEqual(flags[flags.index('--tools')+1],'')
        for flag in ('--strict-mcp-config','--disable-slash-commands','--safe-mode','--no-session-persistence'):self.assertIn(flag,flags)
        self.assertEqual(json.loads(flags[flags.index('--mcp-config')+1]),{'mcpServers':{}})
        self.assertEqual(review.reviewer('claude','claude-sonnet-5-5')[0],'opus')
        self.assertEqual(review.reviewer('codex')[0],'opus')
        with self.assertRaises(Blocked):review.reviewer('deepseek')
        self.run_review()
        call=self.good.calls[0]
        self.assertEqual(call['argv'][:2],['/bin/claude-fake','-p']);self.assertEqual(call['argv'][2:],flags)
        self.assertEqual(call['timeout'],600);self.assertEqual(call['cwd_files'],[])  # 空的临时目录
        self.assertNotEqual(Path(call['cwd']).resolve(),self.repo);self.assertFalse(str(self.repo) in ' '.join(call['argv']))
        self.assertIn('return xs[0]+sum(xs[1:])',call['text'])  # 材料只当提问文字给

    def test_invalid_answers_are_voided_with_raw_text(self):
        cases=(('这不是 JSON','不是 JSON'),(json.dumps({**answer(),'多余':1}),'字段不对'),(json.dumps(answer(把握分=120)),'0 到 100'),
               (json.dumps(answer(把握分=True)),'0 到 100'),(json.dumps(answer(原因=[{'说明':'x'}])),'原因要是列表'),
               (json.dumps(answer(原因=[])),'1 到 3'),(json.dumps(answer(严重问题='无')),'严重问题'))
        for raw,why in cases:
            with self.subTest(why=why):
                result=self.run_review(runner=FakeRunner(stream(raw)))
                target=Path(result['存档'])
                self.assertEqual(result['结论'],'作废');self.assertIn(why,result['作废原因'])
                self.assertFalse((target/'回答.json').exists());self.assertIn(raw,(target/'作废.txt').read_text())
                self.assertEqual(json.loads((target/'发送记录.json').read_text())['结果'],'作废')
        result=self.run_review(runner=FakeRunner(stream(json.dumps(answer(避坑='没写清')))),kind='事后')
        self.assertIn('场景｜坑｜经验',result['作废原因'])
        result=self.run_review(runner=FakeRunner(stream('```json\n'+json.dumps(answer())+'\n```')))
        self.assertEqual(result['结论'],'有回答')

    def test_reviewer_with_tools_or_failure_is_voided(self):
        for runner,why in ((FakeRunner(stream(json.dumps(answer()),tools=['Read'])),'没有工具'),
                           (FakeRunner(stream(json.dumps(answer()),mcp=[{'name':'x'}])),'没有工具'),
                           (FakeRunner(stream('出错',is_error=True)),'没正常结束'),(FakeRunner('',code=1),'没正常结束'),
                           (FakeRunner(error=FileNotFoundError()),'没启动起来')):
            with self.subTest(why=why):
                result=self.run_review(runner=runner)
                self.assertEqual(result['结论'],'作废');self.assertIn(why,result['作废原因'])

    def test_one_review_per_project_at_a_time(self):
        lock=review._Lock(self.s.root/review.ARCHIVE/'.锁'/(review._slug(self.repo.name)+'.lock'))
        runner=FakeRunner(stream(json.dumps(answer())))
        with lock:
            with self.assertRaises(Blocked) as caught:self.run_review(runner=runner)
        self.assertIn('正在复盘',str(caught.exception));self.assertEqual(runner.calls,[])
        self.assertEqual(self.run_review(runner=runner)['结论'],'有回答')  # 锁放开后能跑

    def test_timeout_voids_and_real_runner_kills_child(self):
        result=self.run_review(runner=FakeRunner(error=subprocess.TimeoutExpired(['claude'],600,output=b'half')))
        self.assertEqual(result['结论'],'作废');self.assertIn('10 分钟',result['作废原因'])
        self.assertIn('half',(Path(result['存档'])/'作废.txt').read_text())
        started=time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):review.run_cli(['sleep','30'],'',str(self.t),0.5)
        self.assertLess(time.monotonic()-started,10)

    def test_archive_and_send_record(self):
        when=time.mktime(time.strptime('2026-11-14 10:20:30','%Y-%m-%d %H:%M:%S'))
        result=self.run_review(now=lambda:when)
        target=Path(result['存档'])
        self.assertEqual(target.parent,self.s.root/'数据/复盘');self.assertEqual(target.name,'20261114-102030-'+self.repo.name+'-卡住')
        self.assertEqual(sorted(p.name for p in (target/'包').iterdir()),['提问.txt','改动.diff','目标.md','说明.md'])
        self.assertEqual((target/'包/提问.txt').read_text(),self.good.calls[0]['text'])  # 留的就是发出去的原文
        self.assertEqual(json.loads((target/'回答.json').read_text()),answer())
        record=json.loads((target/'发送记录.json').read_text())
        self.assertEqual(record['接收方'],{'厂商':'Anthropic','经由':'claude -p（关掉全部工具）','模型':'sonnet','实际模型':'claude-sonnet-5-5'})
        self.assertEqual(record['复盘方启动时的工具'],{'工具':[],'MCP':[]})
        self.assertEqual(record['时间'],'2026-11-14 10:20:30');self.assertEqual(record['结果'],'有回答');self.assertEqual(record['把握分'],42)
        self.assertEqual([f['名称'] for f in record['文件']],['说明.md','目标.md','改动.diff'])
        self.assertTrue(all(f['字节']>0 for f in record['文件']));self.assertEqual(record['提问字节'],len(self.good.calls[0]['text'].encode()))
        self.assertEqual(record['基准'],'main');self.assertEqual(record['命令'][0],'claude-fake')
        self.assertIn('把握分低于 60',result['说明'])

    def test_default_base_prefers_development(self):
        self.assertRaises(Blocked,review.default_base,self.repo)
        git(self.repo,'update-ref','refs/remotes/origin/main','main')
        self.assertEqual(review.default_base(self.repo),'origin/main')
        git(self.repo,'update-ref','refs/remotes/gitea/development','main')
        self.assertEqual(review.default_base(self.repo),'gitea/development')

    def test_weekly_line_once_per_week(self):
        today=time.mktime(time.strptime('2026-11-16 08:00','%Y-%m-%d %H:%M'))
        board.apply(self.s,[{'id':'toy','name':'玩具','path':str(self.repo)}])
        self.assertEqual(board.weekly_check(self.s,today=today),[])
        self.run_review(now=lambda:today-86400)
        self.run_review(now=lambda:today-2*86400,runner=FakeRunner(stream(json.dumps(answer(把握分=80)))))
        self.run_review(now=lambda:today-3*86400,runner=FakeRunner(stream('乱写')))
        self.run_review(now=lambda:today-9*86400)  # 7 天以前的不算
        self.assertEqual(board.weekly_check(self.s,today=today),['本周复盘 3 次，其中 1 次把握分低于 60（玩具），1 次作废'])
        self.assertEqual(board.weekly_check(self.s,today=today),[])  # 同一周只提醒一次
        later=board.weekly_check(self.s,today=today+20*86400)
        self.assertEqual([m for m in later if '复盘' in m],[])  # 那一周没有复盘就不写
        self.assertEqual([m for m in later if '复盘' not in m],['玩具有 1 处超过 14 天没动、也没合进主线或开发分支（如 feat）：还要不要，回一句即可'])  # 0.2.45 起列出久没动又没合并的分支

    def test_weekly_line_resets_at_local_monday_in_singapore(self):
        self.addCleanup(time.tzset)
        zone = patch.dict(os.environ, {'TZ': 'Asia/Singapore'})
        zone.start()
        self.addCleanup(zone.stop)
        time.tzset()

        # 新加坡跨入周一时，UTC 仍是周日。
        sunday = datetime(2026, 11, 15, 15, 59, 59, tzinfo=timezone.utc).timestamp()
        monday = sunday + 1
        board.apply(self.s, [{'id': 'toy', 'name': '玩具', 'path': str(self.repo)}])
        self.run_review(now=lambda: sunday - 60)
        expected = ['本周复盘 1 次，其中 1 次把握分低于 60（玩具）']
        self.assertEqual(board.weekly_check(self.s, today=sunday), expected)
        self.assertEqual(board.weekly_check(self.s, today=sunday), [])
        self.assertEqual(board.weekly_check(self.s, today=monday), expected)
        self.assertEqual(board.weekly_check(self.s, today=monday), [])

    def test_cli_registered(self):
        from portable.__main__ import main
        with patch.object(review,'run_cli',FakeRunner(stream(json.dumps(answer())))) as runner,\
             patch.object(review,'run_gitleaks',lambda d:[]),patch.object(review.shutil,'which',return_value='/bin/claude-fake'):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(['--root',str(self.s.root),'review',str(self.repo),'--kind','卡住','--note',str(self.note),'--base','main']),0)
        self.assertEqual(len(runner.calls),1);self.assertEqual(json.loads(out.getvalue())['结论'],'有回答')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['--root',str(self.s.root),'review',str(self.t/'不存在'),'--kind','卡住','--note',str(self.note)]),2)
        self.assertIn('找不到项目',err.getvalue())


    def verify_payload(self,verdict='真',**over):
        evidence=[{'来源':'python -m unittest 的命令输出','级别':'原始输出'},{'来源':'说明里转述的测试结论','级别':'二手转述'}]
        if verdict=='待确认':evidence=[{'来源':'没有读到命令输出','级别':'无'}]
        item={'说法':'空列表求和会报错','判词':verdict,'证据及级别':evidence,
              '四关':{'时间':'命令输出晚于这次改动','对照':'和预期的报错一致','量级':'1 处，数量说得通','矛盾':'没有看到反例'},
              '改判条件':'若原始输出显示空列表不报错，就改判为假'}
        item.update(over)
        return {'条目':[item],'总判词':verdict}

    def codex_candidate(self):
        return {'kind':'codex','厂商':'OpenAI','经由':'codex exec（只读沙盒）','模型':'codex-readonly',
                'argv':['/bin/codex-fake','exec','--sandbox','read-only','--skip-git-repo-check','-'],'mode':'plain'}

    def claude_candidate(self):
        return {'kind':'claude','厂商':'Anthropic','经由':'claude -p（关掉全部工具）','模型':'opus',
                'argv':['/bin/claude-fake','-p','--tools',''],'mode':'claude_stream'}

    def run_verify(self,runner,writer='claude',candidates=None,now=None):
        return review.review(self.s,str(self.repo),'核实',str(self.note),writer=writer,base='main',runner=runner,
                             gitleaks=lambda d:[],candidates=candidates if candidates is not None else [self.codex_candidate()],
                             now=now or time.time)

    def test_verify_kind_is_readonly_and_archives_under_review(self):
        before=subprocess.check_output(['git','-C',str(self.repo),'status','--porcelain'])
        when=time.mktime(time.strptime('2026-11-23 12:30:45','%Y-%m-%d %H:%M:%S'))
        runner=FakeRunner(json.dumps(self.verify_payload(),ensure_ascii=False))
        result=self.run_verify(runner,now=lambda:when)
        after=subprocess.check_output(['git','-C',str(self.repo),'status','--porcelain'])
        self.assertEqual(before,after)  # 只读：不改项目仓库
        self.assertEqual(result['结论'],'有回答')
        self.assertEqual(result['回答']['总判词'],'真')
        self.assertEqual(result['接收方']['厂商'],'OpenAI')
        call=runner.calls[0]
        self.assertEqual(call['cwd_files'],[])
        self.assertIn('--sandbox',call['argv']);self.assertIn('read-only',call['argv'])
        self.assertIn('只读',call['text']);self.assertIn('不要改文件',call['text'])
        target=Path(result['存档'])
        self.assertEqual(target.parent,self.s.root/'数据/复盘')
        self.assertEqual(target.name,'20261123-123045-'+self.repo.name+'-核实')
        self.assertEqual(json.loads((target/'发送记录.json').read_text())['类型'],'核实')
        self.assertIsNone(review.find_merge_receipt(self.s,str(self.repo)))  # 核实回执不能拿去放行合并
        outside=[p for p in Path(review.__file__).resolve().parent.glob('*.py') if p.name!='review.py']
        for path in outside:
            body=path.read_text(encoding='utf-8')
            for needle in ('--kind 核实',"kind='核实'",'kind="核实"'):
                self.assertNotIn(needle,body,path.name)

    def test_verify_prompt_has_eight_requirements(self):
        text=review.prompt('核实',{'说明.md':'说法：测试全过。\n'})
        for part in ('把关键说法拆成能数的断言','两个来源','至少一个是原始输出','同一来源的拷贝只算一个',
                     '过四关','证据晚于改动','和基线','数字合理','有无反例','证据标级','二手转述',
                     '真、假、一半对、待确认','查不了就写待确认','改判条件','ci check','测到了什么','没测到什么',
                     '结构化 JSON','说法','判词','证据及级别','总判词'):
            with self.subTest(part=part):self.assertIn(part,text)
        self.assertNotIn('把握分',text)

    def test_verify_rejects_verdicts_outside_the_four(self):
        for verdict in ('真','假','一半对','待确认'):
            parsed,problem=review.parse_answer(json.dumps(self.verify_payload(verdict),ensure_ascii=False),'核实')
            self.assertEqual(problem,'',verdict);self.assertEqual(parsed['总判词'],verdict)
        for bad in ('通过','基本真','也许','完成'):
            with self.subTest(bad=bad):
                result=self.run_verify(FakeRunner(json.dumps(self.verify_payload(bad),ensure_ascii=False)))
                self.assertEqual(result['结论'],'作废');self.assertIn('判词',result['作废原因'])
                self.assertFalse((Path(result['存档'])/'回答.json').exists())
        result=self.run_verify(FakeRunner(json.dumps({**self.verify_payload(),'总判词':'大概'},ensure_ascii=False)))
        self.assertEqual(result['结论'],'作废');self.assertIn('总判词',result['作废原因'])
        graded=self.verify_payload();graded['条目'][0]['证据及级别'][0]['级别']='实测'
        result=self.run_verify(FakeRunner(json.dumps(graded,ensure_ascii=False)))
        self.assertEqual(result['结论'],'作废');self.assertIn('级别',result['作废原因'])

    def test_verify_rejects_same_vendor(self):
        runner=FakeRunner(json.dumps(self.verify_payload(),ensure_ascii=False))
        for writer,same in (('claude',self.claude_candidate()),('codex',self.codex_candidate())):
            with self.subTest(writer=writer):
                with self.assertRaises(Blocked) as caught:
                    self.run_verify(runner,writer=writer,candidates=[same])
                self.assertIn('同厂商',str(caught.exception))
        self.assertEqual(runner.calls,[])
        self.assertEqual(self.archives(),[])
        mixed=FakeRunner(json.dumps(self.verify_payload(),ensure_ascii=False))
        result=self.run_verify(mixed,writer='claude',candidates=[self.claude_candidate(),self.codex_candidate()])
        self.assertEqual(result['结论'],'有回答')
        self.assertEqual([Path(call['argv'][0]).name for call in mixed.calls],['codex-fake'])

    def test_verify_help_lists_kind(self):
        import argparse
        parser=review.register(argparse.ArgumentParser().add_subparsers())
        text=parser.format_help()
        self.assertIn('卡住|方案|事后|合并|核实',text)
        self.assertIn('只读',text)
        self.assertIn('另一家厂商',text)
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                from portable.__main__ import main
                main(['review','--help'])
        self.assertEqual(caught.exception.code,0)
        self.assertIn('--kind',out.getvalue());self.assertIn('核实',out.getvalue())


class PrivacyConnectionStringTest(unittest.TestCase):
    def test_db_urls_with_credentials_are_caught(self):
        for url in (FAKE_DB,'postgresql+psycopg://u:p4ssw0rd@h/db','mysql://root:'+'hunter2x'+'@127.0.0.1/x',
                    'mongodb+srv://ops:'+'Sup3rS3cret'+'@cluster0.example.net/app','mongodb://a:'+'b1b2b3'+'@h:27017',
                    'redis://:'+'pw12345'+'@cache:6379/0','rediss://default:'+'pw12345'+'@cache:6380','amqp://guest:'+'guest1'+'@mq:5672//'):
            with self.subTest(url=url):
                self.assertTrue(privacy.CredentialContent().search(url))
                self.assertNotIn(url.split('://',1)[1].split('@')[0].split(':')[-1],privacy.redact_text('db = '+url))
                self.assertEqual(privacy.secret_lines('x = 1\nurl = '+url+'\n'),[2])
        for safe in ('postgres://user@host/db','postgres://${DB_USER}:${DB_PASS}@h/db','postgres://u:<password>@h/db','https://example.com/a:b@c'):
            with self.subTest(safe=safe):self.assertFalse(privacy.CredentialContent().search(safe))


if __name__=='__main__':unittest.main()
