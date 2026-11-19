"""后台自动接着做（portable/nudge.py）：全部用临时目录和假的 t3 命令，不碰真实 T3。见 DECISIONS 2026-11-18。"""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.dont_write_bytecode = True
from portable.core import Store, Blocked
from portable import board, nudge, settings

STATUS = ('---\n项目: 甲\n状态: {state}\n阶段: 开发中\n完成度: 30\n最后更新: 2026-11-18\n'
          '长期目标: {goal}\n下一步: {next}\n等谁: {who}\n卡在: 等 CI\n---\n\n## 近期动态\n')
FEATURES = '| 编号 | 功能 | 状态 |\n|---|---|---|\n| A-1 | 登录 | ✅ 完成 |\n| A-2 | 报表 | {doing} |\n'

FAKE = '''#!{python}
import json, sys, pathlib
here = pathlib.Path(__file__).parent
args = sys.argv[1:]
with (here / 'calls.jsonl').open('a') as f:
    f.write(json.dumps(args, ensure_ascii=False) + '\\n')
cfg = json.loads((here / 'cfg.json').read_text())
if args[:2] == ['thread', 'list']:
    if cfg.get('list_rc'):
        print(cfg.get('list_err', ''), file=sys.stderr); sys.exit(cfg['list_rc'])
    print(json.dumps(cfg['threads']))
elif args[:2] == ['thread', 'show']:
    print(json.dumps(cfg['show']))
elif args[:2] in (['thread', 'send'], ['thread', 'start']):
    if cfg.get('send_rc'):
        print('Thread is archived and cannot receive messages.', file=sys.stderr); sys.exit(cfg['send_rc'])
    print(json.dumps({{'threadId': 'new-thread' if args[1] == 'start' else args[3], 'runId': 'r9'}}))
'''


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace('+00:00', 'Z')


class NudgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(self.tmp.name).resolve()
        home = patch.dict(os.environ, {'PORTABLE_HOME': str(self.t / 'home')})
        home.start(); self.addCleanup(home.stop)
        self.s = Store(self.t / '包')
        self.addCleanup(self.s.db.close)
        self.repo = self.t / 'repo'; self.repo.mkdir()
        r = str(self.repo)
        board.apply(self.s, [{'id': 'jia', 'name': '甲', 'path': r, 'status_path': r + '/STATUS.md',
                              'features_path': r + '/功能清单.md', 'prd_path': r + '/docs/PRD.md'}])
        settings.set_value(self.s, 'auto_continue', True)
        self.now = dt.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        self.fake = self.t / 'fake-t3'; self.fake.mkdir()
        self.cli = self.fake / 't3'
        self.cli.write_text(FAKE.format(python=sys.executable)); self.cli.chmod(0o755)
        self.progress()
        self.t3()

    # —— 布置 ——
    def progress(self, *, state='进行中', goal='完成 1 块 / 共 3 块；本期：B；卡在：无', next='做 B', who='无',
                 doing='🔄 进行中', age=3 * 3600, at=None):
        (self.repo / 'STATUS.md').write_text(STATUS.format(state=state, goal=goal, next=next, who=who))
        (self.repo / '功能清单.md').write_text(FEATURES.format(doing=doing))
        when = (self.now if at is None else at) - age
        for name in ('STATUS.md', '功能清单.md'):
            os.utime(self.repo / name, (when, when))

    def t3(self, *, status='completed', active=None, background=(), pending=0, idle=3600, messages=None,
           latest='r1', threads=None, list_rc=0, list_err='', send_rc=0, at=None):
        now = self.now if at is None else at
        if messages is None:
            messages = [{'role': 'user', 'runId': 'r1', 'text': '做 A'}, {'role': 'assistant', 'runId': 'r1', 'text': 'A 做完了'}]
        if threads is None:
            threads = [{'threadId': 'th-1', 'updatedAt': iso(now - idle), 'parentThreadId': None, 'settled': False, 'snoozed': False}]
        show = {'thread': {'threadId': 'th-1', 'status': status, 'activeRunId': active, 'latestRunId': latest,
                           'pendingBackgroundTasks': list(background), 'pendingRequestCount': pending,
                           'archived': False, 'snoozed': False, 'blockedOnThreadWait': False, 'updatedAt': iso(now - idle)},
                'messages': messages}
        (self.fake / 'cfg.json').write_text(json.dumps({'threads': threads, 'show': show, 'list_rc': list_rc,
                                                        'list_err': list_err, 'send_rc': send_rc}))

    def tick(self, at=None, cli=None):
        self.s.put(nudge.CLOCK, {})
        return nudge.tick(self.s, self.now if at is None else at, cli=str(self.cli) if cli is None else cli)

    def calls(self, kind=None):
        path = self.fake / 'calls.jsonl'
        rows = [json.loads(x) for x in path.read_text().splitlines()] if path.is_file() else []
        return [r for r in rows if kind is None or r[:2] == ['thread', kind]]

    def log(self):
        path = self.s.root / nudge.LOG
        return [json.loads(x) for x in path.read_text().splitlines()] if path.is_file() else []

    # —— 该发 ——
    def test_sends_fixed_text_when_everything_holds(self):
        result = self.tick()
        self.assertEqual(result['sent'], ['甲'])
        send = self.calls('send')
        self.assertEqual(len(send), 1)
        self.assertEqual(send[0][3], 'th-1')
        text = send[0][4]
        self.assertTrue(text.startswith('随身开发后台提醒：甲长期目标还差 2 块，进度表 3 小时没更新。'))
        for words in ('只是提醒，不算任何授权', '上线、改真实数据、账号密钥、花钱或对外发消息照旧先问用户',
                      '先查你在等的东西有没有结果', '在 STATUS.md 写清卡在哪、在等谁'):
            self.assertIn(words, text)
        row = self.log()[-1]
        self.assertEqual((row['项目'], row['对话'], row['已发']), ('甲', 'th-1', True))
        self.assertIn('进度表', row['原因'])
        self.assertIn('最近几次叫醒', (self.s.root / '看板/自动接着做.md').read_text())

    def test_fallback_without_block_count_uses_features_and_next_step(self):
        self.progress(goal='无')
        self.assertEqual(self.tick()['sent'], ['甲'])
        self.assertIn('甲功能清单还有进行中的', self.calls('send')[0][4])

    # —— 不该发 ——
    def test_does_not_send_when_any_condition_fails(self):
        cases = {
            '对话在跑': dict(t3=dict(status='running')),
            '有正在跑的回合': dict(t3=dict(active='r2')),
            '有后台任务': dict(t3=dict(background=['task-1'])),
            '有等回答的请求': dict(t3=dict(pending=1)),
            '对话刚停下不到 10 分钟': dict(t3=dict(idle=5 * 60)),
            'AI 说要做但在问用户': dict(progress=dict(age=20 * 60), t3=dict(idle=15 * 60, messages=[
                {'role': 'user', 'runId': 'r1', 'text': '做 A'},
                {'role': 'assistant', 'runId': 'r1', 'text': '接下来我会改三处。要现在开工吗？'}])),
            '等谁是用户': dict(progress=dict(who='用户定方案')),
            '状态暂缓': dict(progress=dict(state='暂缓（等需求）')),
            '已全部完成': dict(progress=dict(goal='完成 3 块 / 共 3 块')),
            '没写块数也没进行中': dict(progress=dict(goal='无', doing='⏳ 未开始')),
            '进度表刚更新': dict(progress=dict(age=3600)),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.setUp()
                progress = case.get('progress', {})
                self.progress(**progress)
                self.t3(**case.get('t3', {}))
                result = self.tick()
                self.assertEqual(result['sent'], [], name)
                self.assertEqual(self.calls('send'), [], name)

    def test_owner_answered_is_not_waiting_on_owner(self):
        self.progress(who='用户（已答，不用催）')
        self.assertEqual(self.tick()['sent'], ['甲'])

    def test_night_and_switch_off(self):
        night = dt.datetime.fromtimestamp(self.now).replace(hour=23, minute=30).timestamp()
        self.progress(at=night); self.t3(at=night)
        self.assertTrue(self.tick(night)['night'])
        early = dt.datetime.fromtimestamp(self.now).replace(hour=7, minute=59).timestamp()
        self.assertTrue(self.tick(early)['night'])
        self.assertEqual(self.calls('send') + self.calls('start'), [])
        (self.fake / 'calls.jsonl').unlink(missing_ok=True)
        settings.set_value(self.s, 'auto_continue', False)
        self.assertTrue(self.tick()['off'])
        self.assertEqual(self.calls(), [])

    def test_night_wakes_thread_when_user_still_awake(self):
        night = dt.datetime.fromtimestamp(self.now).replace(hour=0, minute=51).timestamp()
        stopped = [{'role': 'user', 'runId': 'r1', 'text': '帮我查运单'},
                   {'role': 'assistant', 'runId': 'r1', 'text': '先说结论……【未完成】接下来我会先查线上单号。'}]
        self.progress(at=night, age=20 * 60)
        self.t3(at=night, idle=10 * 60, messages=stopped)
        result = self.tick(night)
        self.assertTrue(result['night'])
        self.assertEqual(result['sent'], ['甲'])
        self.assertIn('上一轮你说了接下来要做的事，但停下来了', self.calls('send')[0][4])
        self.assertEqual(self.log()[-1]['原因'], '说了要做却停了')

    def test_night_leaves_sleeping_user_and_new_threads_alone(self):
        night = dt.datetime.fromtimestamp(self.now).replace(hour=2, minute=0).timestamp()
        self.progress(at=night)
        self.t3(at=night, idle=61 * 60)
        self.assertIn('当作睡了', self.tick(night)['skipped']['甲'])
        mine = [{'role': 'user', 'runId': 'r2', 'text': nudge.message('甲', 2, '3 小时')}]
        self.t3(at=night, idle=11 * 60, messages=mine, latest='r2')  # 后台自己的提醒不算人醒着
        self.assertIn('当作睡了', self.tick(night)['skipped']['甲'])
        self.t3(at=night, threads=[])
        self.assertEqual(self.tick(night)['skipped']['甲'], '夜里不新开对话')
        with patch.object(nudge, 'find_cli', return_value=None):
            self.s.put(nudge.CLOCK, {})
            self.assertIn('看不到用户在不在', nudge.tick(self.s, night)['skipped']['甲'])
        self.assertEqual(self.calls('send') + self.calls('start'), [])

    # —— 刹车 ——
    def test_gap_daily_cap_and_named_after_three_unmoved(self):
        self.assertEqual(self.tick()['sent'], ['甲'])
        one_hour = self.now + 3600
        self.t3(at=one_hour)
        self.assertEqual(self.tick(one_hour)['skipped']['甲'], '离上次提醒不到 2 小时')
        later = self.now + 2 * 3600 + 60
        self.t3(at=later)
        self.assertEqual(self.tick(later)['sent'], ['甲'])
        third = later + 2 * 3600 + 60
        self.t3(at=third)
        self.assertEqual(self.tick(third)['sent'], ['甲'])
        fourth = third + 2 * 3600 + 60
        self.t3(at=fourth)
        result = self.tick(fourth)
        self.assertEqual(result['sent'], [])
        self.assertEqual(len(result['named']), 1)
        self.assertIn('甲：后台叫了 3 次还没动', result['named'][0])
        self.assertIn('卡在：等 CI', result['named'][0])
        self.assertEqual(len(self.calls('send')), 3)
        self.assertIn('后台叫了 3 次还没动', (self.s.root / '看板/提醒.md').read_text())
        self.assertIn('后台叫了 3 次还没动', (self.s.root / '看板/自动接着做.md').read_text())
        self.assertIn('甲：后台叫了 3 次还没动', nudge.board_lines(self.s)[0])
        page = board.overview(self.s)['page']
        self.assertIn('**自动接着做**：甲：后台叫了 3 次', page)
        # 进度表动了：点名撤掉，重新计数
        self.progress(age=3 * 3600, at=fourth + 4 * 3600)
        after = fourth + 4 * 3600
        self.t3(at=after)
        self.tick(after)
        self.assertNotIn('后台叫了', (self.s.root / '看板/提醒.md').read_text())

    def test_daily_cap_three_even_when_progress_moves(self):
        times = [self.now - 3 * 3600 + i * (2 * 3600 + 60) for i in range(4)]  # 09:00 起，同一天
        for i, at in enumerate(times):
            self.progress(at=at)  # 每次之前进度表都动过（3 小时前），不会被点名
            os.utime(self.repo / 'STATUS.md', (at - 3 * 3600 + i, at - 3 * 3600 + i))
            self.t3(at=at)
            result = self.tick(at)
            if i < 3:
                self.assertEqual(result['sent'], ['甲'], i)
            else:
                self.assertEqual(result['skipped']['甲'], '今天已经叫了 3 次')

    # —— 找不到对话 ——
    def test_no_t3_writes_board_line(self):
        with patch.object(nudge, 'find_cli', return_value=None):
            self.s.put(nudge.CLOCK, {})
            result = nudge.tick(self.s, self.now)
        self.assertEqual(result['unreachable'], ['甲：没接 T3，后台叫不醒它'])
        self.assertIn('没接 T3，后台叫不醒它', (self.s.root / '看板/自动接着做.md').read_text())
        self.assertIn('没接 T3', board.overview(self.s)['page'])

    def test_project_not_in_t3(self):
        self.t3(list_rc=1, list_err="No T3 Code project matches '/x'.")
        result = self.tick()
        self.assertIn('T3 里没有这个项目', result['unreachable'][0])
        self.assertEqual(self.calls('send'), [])

    def test_only_archived_threads_starts_a_new_one(self):
        self.t3(threads=[])  # 已归档的不在列表里，也收不到消息（实测）
        result = self.tick()
        self.assertEqual(result['sent'], ['甲'])
        start = self.calls('start')
        self.assertEqual(len(start), 1)
        self.assertEqual(start[0][-2], str(self.repo))
        self.assertTrue(start[0][-1].startswith('接着做甲，先读 STATUS.md。随身开发后台提醒：'))
        self.assertEqual(self.log()[-1]['对话'], 'new-thread')

    def test_send_failure_is_logged_not_counted(self):
        self.t3(send_rc=1)
        result = self.tick()
        self.assertEqual(result['sent'], [])
        self.assertFalse(self.log()[-1]['已发'])
        self.t3()
        self.assertEqual(self.tick(self.now + 120)['sent'], ['甲'])

    # —— 空回合 ——
    def test_empty_turn_after_ten_minutes(self):
        self.progress(age=20 * 60)  # 进度表才 20 分钟没动，但上一轮 AI 没回话
        empty = [{'role': 'user', 'runId': 'r1', 'text': '做 A'}, {'role': 'assistant', 'runId': 'r1', 'text': '派出去了'}]
        self.t3(idle=5 * 60, messages=empty, latest='r2')
        self.assertEqual(self.tick()['skipped']['甲'], '对话刚停下不到 10 分钟')
        self.t3(idle=11 * 60, messages=empty, latest='r2')
        result = self.tick()
        self.assertEqual(result['sent'], ['甲'])
        self.assertIn('上一轮被叫醒后没回话就结束了', self.calls('send')[0][4])
        self.assertIn('空回合', self.log()[-1]['原因'])

    def test_answered_turn_with_fresh_progress_is_left_alone(self):
        self.progress(age=20 * 60)
        self.t3(idle=40 * 60)
        self.assertEqual(self.tick()['skipped']['甲'], '进度表刚更新过')

    def test_our_own_reminder_does_not_count_as_user_speaking(self):
        mine = [{'role': 'user', 'runId': 'r2', 'text': nudge.message('甲', 2, '3 小时')}]
        self.progress(age=20 * 60)
        self.t3(idle=11 * 60, messages=mine, latest='r2')
        self.assertEqual(self.tick()['sent'], ['甲'])

    def test_promise_then_stop_after_ten_minutes(self):
        self.progress(age=20 * 60)  # 进度表刚动过，但 AI 说了接下来要做又停了
        stopped = [{'role': 'user', 'runId': 'r1', 'text': '做 A'},
                   {'role': 'assistant', 'runId': 'r1', 'text': 'A 的结论如下。接下来我会去查线上单号。'}]
        self.t3(idle=5 * 60, messages=stopped)
        self.assertEqual(self.tick()['skipped']['甲'], '对话刚停下不到 10 分钟')
        self.t3(idle=11 * 60, messages=stopped)  # 用户这一轮发过话也照叫：AI 答完了，是用户在等它
        self.assertEqual(self.tick()['sent'], ['甲'])
        self.assertIn('上一轮你说了接下来要做的事，但停下来了', self.calls('send')[0][4])

    def test_promise_words(self):
        for text in ('接下来我会先查。', '【未完成】还差两处', '我现在去跑测试'):
            self.assertTrue(nudge.promised(text), text)
        for text in ('都做完了。', '接下来我会改三处，可以吗？', ''):
            self.assertFalse(nudge.promised(text), text)

    def test_user_answered_turn_does_not_wait_thirty_minutes(self):
        self.t3(idle=11 * 60)  # 这一轮用户发起、AI 已回，进度表 3 小时没动
        self.assertEqual(self.tick()['sent'], ['甲'])

    def test_user_message_in_empty_turn_waits_thirty_minutes(self):
        typed = [{'role': 'user', 'runId': 'r2', 'text': '继续'}]
        self.progress(age=20 * 60)
        self.t3(idle=11 * 60, messages=typed, latest='r2')
        self.assertEqual(self.tick()['skipped']['甲'], '用户发了话、AI 没回，等满 30 分钟')
        self.t3(idle=31 * 60, messages=typed, latest='r2')
        self.assertEqual(self.tick()['sent'], ['甲'])

    # —— 开关和后台 ——
    def test_default_follows_edition(self):
        settings.set_value(self.s, 'auto_continue', None)
        self.assertFalse(settings.auto_continue_on(self.s))
        (self.s.root / '发行.json').write_text(json.dumps({'schema': 1, 'edition': 'friend'}))
        self.assertTrue(settings.auto_continue_on(self.s))
        settings.set_value(self.s, 'auto_continue', False)
        self.assertFalse(settings.auto_continue_on(self.s))
        (self.s.root / '发行.json').write_text(json.dumps({'schema': 1}))
        settings.set_value(self.s, 'auto_continue', None)
        self.assertFalse(settings.auto_continue_on(self.s))
        for bad in ('开', 1):
            with self.assertRaises(Blocked):settings.set_value(self.s, 'auto_continue', bad)
        for bad in (0, -1, True, '2'):
            with self.assertRaises(Blocked):settings.set_value(self.s, 'auto_continue_hours', bad)
        settings.set_value(self.s, 'auto_continue_hours', 3)

    def test_hours_setting_changes_stale_threshold(self):
        settings.set_value(self.s, 'auto_continue_hours', 4)
        self.assertEqual(self.tick()['skipped']['甲'], '进度表刚更新过')

    def test_safe_tick_swallows_errors(self):
        with patch.object(nudge, 'tick', side_effect=RuntimeError('boom')):
            self.assertEqual(nudge.safe_tick(self.s, self.now), {'error': 'RuntimeError'})
        self.assertEqual(self.s.get(nudge.ERROR)['detail'], 'boom')

    def test_runner_keeps_board_and_heartbeat_when_nudge_breaks(self):
        from portable import supervisor
        runner = supervisor.Runner(self.s, clock=lambda: self.now)
        with patch.object(nudge, 'tick', side_effect=RuntimeError('boom')):
            runner.tick()
        self.assertTrue((self.s.root / '看板/全部项目.md').is_file())
        self.assertEqual(self.s.get('runner:heartbeat')['at'], self.now)
        self.assertEqual(self.s.get(nudge.ERROR)['type'], 'RuntimeError')

    def test_runner_calls_nudge_each_round(self):
        from portable import supervisor
        runner = supervisor.Runner(self.s, clock=lambda: self.now)
        with patch.object(nudge, 'find_cli', return_value=str(self.cli)):
            result = runner.tick()
        self.assertEqual(result.get('自动接着做'), ['甲'])

    def test_throttled_to_once_a_minute(self):
        self.s.put(nudge.CLOCK, {'at': self.now - 30})
        self.assertIn('skipped', nudge.tick(self.s, self.now, cli=str(self.cli)))


if __name__ == '__main__':
    unittest.main()


class ProgressTimeIgnoresT3Checkpoints(unittest.TestCase):
    def test_t3_checkpoint_refs_do_not_count_as_progress(self):
        import os,subprocess,tempfile,time
        from pathlib import Path
        from portable import nudge
        with tempfile.TemporaryDirectory() as d:
            repo=Path(d)
            def git(*a,env=None):subprocess.run(['git','-C',str(repo),'-c','user.name=t','-c','user.email=t@example.invalid','-c','commit.gpgsign=false',*a],check=True,capture_output=True,env=env)
            git('init','-q')
            (repo/'STATUS.md').write_text('a\n')
            old=time.time()-5*3600
            os.utime(repo/'STATUS.md',(old,old))
            # 模拟 T3 每轮存的快照：提交到 refs/t3/ 下，不在任何分支上
            git('add','STATUS.md');env=dict(os.environ,GIT_COMMITTER_DATE='@%d +0000'%int(time.time()),GIT_AUTHOR_DATE='@%d +0000'%int(time.time()))
            tree=subprocess.check_output(['git','-C',str(repo),'write-tree'],text=True).strip()
            sha=subprocess.check_output(['git','-C',str(repo),'-c','user.name=t','-c','user.email=t@example.invalid','commit-tree',tree,'-m','t3 checkpoint'],text=True,env=env).strip()
            git('update-ref','refs/t3/checkpoints/x',sha)
            entry={'id':'p','path':str(repo),'status_path':str(repo/'STATUS.md')}
            self.assertLess(nudge.progress_time(entry),time.time()-4*3600)
