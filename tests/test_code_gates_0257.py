"""0.2.57 代码把关测试：
1. 动态扫描本机可用 AI 池子（多 AI 跨厂家互审、首选限额自动切下一家、单 AI 独立进程、0 命令行 AI 代码硬检查降级）+ 高风险合并凭审查回执放行；
2. 全量测试守门（主干同步检查、本机同一时间只跑一个全量锁、挂了只跑局部、动 5 类核心或主干更新升全量、一批最多 2 次全量熔断）；
3. 4 个老漏洞代码化（代码+STATUS 混装 PR 拦截、STATUS 超 20 条自动归档、安装目录被手改报警、AGENTS.md 超 40 行体检点名）。
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from portable.core import Blocked, Store
from portable import board, ci, distribution, review, test_gate


def _git(repo, *args):
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'commit.gpgsign=false', *args],
                   check=True, capture_output=True)


def _ok_answer(score=85):
    return json.dumps({
        '把握分': score,
        '原因': [{'说明': '检查完备，无安全与数据风险', '位置': 'src/app.py:1'}],
        '建议做法': '可以合并',
        '严重问题': [],
        '避坑': '',
    }, ensure_ascii=False)


class CodeGates0257Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, {'HOME': str(self.t / 'home'), 'PORTABLE_HOME': str(self.t / 'home')})
        env.start()
        self.addCleanup(env.stop)
        self.s = Store(self.t / 'root')
        self.addCleanup(self.s.db.close)
        self.repo = self.t / 'demo'
        self.repo.mkdir()
        _git(self.repo, 'init', '-q', '-b', 'main')
        _git(self.repo, 'remote', 'add', 'origin', 'http://127.0.0.1:3000/test/demo.git')
        (self.repo / 'STATUS.md').write_text('---\n项目: 演示\n状态: 进行中\n长期目标: 无\n下一步: 做 A\n等谁: 无\n卡在: 无\n最后更新: 2026-11-20\n---\n\n## 近期动态\n\n- 2026-11-20 · 初始化\n')
        (self.repo / 'src').mkdir()
        (self.repo / 'src/app.py').write_text('x = 1\n')
        _git(self.repo, 'add', '.')
        _git(self.repo, 'commit', '-qm', 'init')
        _git(self.repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        _git(self.repo, 'switch', '-qc', 'feat')
        (self.repo / 'src/app.py').write_text('x = 2\n')
        _git(self.repo, 'commit', '-qam', 'feat')
        board.apply(self.s, [{'id': 'demo', 'name': '演示', 'path': str(self.repo), 'status_path': str(self.repo / 'STATUS.md')}])
        self.note = self.t / 'note.md'
        self.note.write_text('合并前审查说明\n')

    # —— 第一块：动态 AI 池子审查 + 高风险合并凭审查回执放行 ——

    def test_discover_reviewers_cross_vendor_and_fallback_when_quota_hit(self):
        bins = {'claude': '/usr/bin/claude', 'codex': '/usr/bin/codex'}
        pool_for_claude = review.discover_reviewers('claude', which=lambda name: bins.get(name))
        self.assertEqual([c['kind'] for c in pool_for_claude], ['codex', 'claude'])
        pool_for_codex = review.discover_reviewers('codex', which=lambda name: bins.get(name))
        self.assertEqual([c['kind'] for c in pool_for_codex], ['claude', 'codex'])

        # 首选 AI 限额失败时，自动切到池子里的下一个 AI 完成审查
        calls = []
        def runner(argv, text, cwd, timeout):
            calls.append(argv[0])
            if 'codex' in argv[0]:
                return 1, "You've hit your usage limit", ''
            return 0, _ok_answer(88), ''

        candidates = [
            {'kind': 'codex', '厂商': 'OpenAI', '经由': 'codex exec', '模型': 'codex', 'argv': ['/usr/bin/codex', 'exec'], 'mode': 'plain'},
            {'kind': 'claude', '厂商': 'Anthropic', '经由': 'claude -p', '模型': 'sonnet', 'argv': ['/usr/bin/claude', '-p'], 'mode': 'plain'},
        ]
        res = review.review(self.s, 'demo', '合并', str(self.note), runner=runner, gitleaks=lambda d: [], candidates=candidates)
        self.assertEqual(res['结论'], '有回答')
        self.assertEqual(res['回答']['把握分'], 88)
        self.assertEqual(calls, ['/usr/bin/codex', '/usr/bin/claude'])

        # 若所有可用 AI 均执行失败（非 0 个 AI），必须作废，不能伪装成 80 分通过
        bad_res = review.review(self.s, 'demo', '合并', str(self.note),
                                runner=lambda a, t, c, to: (1, 'bad output', ''), gitleaks=lambda d: [], candidates=candidates)
        self.assertEqual(bad_res['结论'], '作废')

    def test_zero_cli_ai_falls_back_to_code_hard_check_without_blocking(self):
        res = review.review(self.s, 'demo', '合并', str(self.note), gitleaks=lambda d: [], candidates=[])
        self.assertEqual(res['结论'], '代码硬检查放行')
        self.assertGreaterEqual(res['回答']['把握分'], 60)
        self.assertEqual(res['接收方']['模型'], 'code-gate')

    def test_high_risk_merge_passes_with_review_receipt_without_user_approval(self):
        from tests.test_ci import FakeApi, pr, st
        head_sha = subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], text=True).strip()
        state = {'merged': False}
        api = FakeApi(lambda: pr(state='closed', merged=True, sha=head_sha) if state['merged'] else pr(sha=head_sha), st('success'))
        api.files = [{'filename': '.github/workflows/ci.yml', 'status': 'changed'}]

        # 既没传 --approval 也没跑过 review --kind 合并：拦下
        with self.assertRaisesRegex(Blocked, '独立审查'):
            ci.merge(self.s, 'demo', 1, confirm=True, risk='高', api=api, merger=lambda *a: state.update(merged=True))
        self.assertFalse(state['merged'])

        # 只有「方案」类型的旧回执，或头提交 SHA 不匹配的回执，不能放行高风险合并
        review.review(self.s, 'demo', '方案', str(self.note),
                      runner=lambda a, t, c, to: (0, _ok_answer(90), ''), gitleaks=lambda d: [],
                      candidates=[{'kind': 'codex', '厂商': 'OpenAI', '经由': 'codex', '模型': 'codex', 'argv': ['/usr/bin/codex'], 'mode': 'plain'}])
        with self.assertRaisesRegex(Blocked, '独立审查'):
            ci.merge(self.s, 'demo', 1, confirm=True, risk='高', api=api, merger=lambda *a: state.update(merged=True))

        # 跑过当前头提交的 review --kind 合并 后：不传 --approval 直接放行
        review.review(self.s, 'demo', '合并', str(self.note), gitleaks=lambda d: [], candidates=[])
        # 如果 PR 头提交变成另一个 SHA，旧 SHA 的合并回执也不能放行
        other_api = FakeApi(pr(sha='deadbeef' * 5), st('success'))
        other_api.files = api.files
        with self.assertRaisesRegex(Blocked, '找不到当前头提交'):
            ci.merge(self.s, 'demo', 1, confirm=True, risk='高', api=other_api, merger=lambda *a: None)

        out = ci.merge(self.s, 'demo', 1, confirm=True, risk='高', api=api, merger=lambda *a: state.update(merged=True))
        self.assertTrue(out['本次合并'])
        self.assertIn('独立审查通过', self.s.get(ci.LOG)[-1]['用户点头'])

    # —— 第二块：全量测试守门（主干同步、单全量排队锁、局部 vs 核心升全量、两次熔断） ——

    def test_test_gate_requires_trunk_sync_and_limits_one_full_at_a_time(self):
        # 主干领先当前分支时：要求先同步主干
        _git(self.repo, 'switch', '-q', 'main')
        (self.repo / 'src/other.py').write_text('y = 1\n')
        _git(self.repo, 'add', '.')
        _git(self.repo, 'commit', '-qm', 'trunk-ahead')
        _git(self.repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        _git(self.repo, 'switch', '-q', 'feat')
        with self.assertRaisesRegex(Blocked, '先同步主干'):
            test_gate.plan_run(self.s, 'demo')

        # 同步主干后：放行
        _git(self.repo, 'merge', '-q', 'main')
        plan = test_gate.plan_run(self.s, 'demo')
        self.assertEqual(plan['mode'], 'full')

        # 本机同一时间只跑一个全量锁
        lock_file = self.t / 'full.lock'
        with test_gate.FullTestLock('项目甲', lock_path=lock_file, wait=False):
            with self.assertRaisesRegex(Blocked, '同一时间只跑一个全量测试.*项目甲'):
                with test_gate.FullTestLock('项目乙', lock_path=lock_file, wait=False):
                    pass

    def test_test_gate_partial_on_normal_fix_full_on_core_and_max_two_full_runs(self):
        # 第一次全量挂了 test_nudge
        test_gate.record_run(self.s, 'demo', 'full', passed=False, failed_tests=['tests.test_nudge'])

        # 普通修补（没动 5 类核心、主干没变）→ 只跑挂掉的和牵涉到的部分（partial）
        plan = test_gate.plan_run(self.s, 'demo', changed_files=['portable/nudge.py'])
        self.assertEqual(plan['mode'], 'partial')
        self.assertIn('tests.test_nudge', plan['targets'])

        # 修补动到 5 类核心（算钱/权限/状态流程/数据库结构/全站外壳）→ 自动升为全量（第 2 次全量）
        for core_file, category in (
            ('src/billing_service.py', 'billing'),
            ('src/auth_permission.py', 'auth'),
            ('src/order_workflow.py', 'workflow'),
            ('db/002_migration.sql', 'schema'),
            ('portable/protocol.json', 'shell'),
        ):
            p2 = test_gate.plan_run(self.s, 'demo', changed_files=[core_file])
            self.assertEqual(p2['mode'], 'full', core_file)
            self.assertTrue(any(category in h for h in p2['core_hits']), core_file)

        # 第 2 次全量又挂了 → 第 3 次请求全量时触发「一批最多两次」熔断
        test_gate.record_run(self.s, 'demo', 'full', passed=False, failed_tests=['tests.test_nudge'])
        with self.assertRaisesRegex(Blocked, '一批最多两次'):
            test_gate.plan_run(self.s, 'demo', force_full=True)

        # 局部重跑修到全绿后，状态恢复通过，且 full_runs 重置为 0，后续新的全量不再被卡
        state = test_gate.record_run(self.s, 'demo', 'partial', passed=True)
        self.assertTrue(state['passed'])
        self.assertEqual(state['failed_tests'], [])
        self.assertEqual(state['full_runs'], 0)
        next_plan = test_gate.plan_run(self.s, 'demo')
        self.assertEqual(next_plan['mode'], 'full')

    def test_verify_entry_runs_full_then_partial_then_resets(self):
        gate_store = self.t / 'gate-store'
        lock_file = self.t / 'verify.lock'
        ran = []
        def fake_runner(cmd, cwd):
            ran.append(cmd)
            if len(ran) == 1:
                return 1, 'FAIL: test_x (tests.test_nudge.NudgeTest.test_x)\n'
            return 0, 'OK\n'
        rc1, plan1 = test_gate.verify_entry(self.repo, store_root=gate_store, runner=fake_runner, lock_path=lock_file)
        self.assertEqual((rc1, plan1['mode']), (1, 'full'))
        rc2, plan2 = test_gate.verify_entry(self.repo, store_root=gate_store, runner=fake_runner, lock_path=lock_file)
        self.assertEqual((rc2, plan2['mode']), (0, 'partial'))
        self.assertIn('tests.test_nudge', ran[1])

    # —— 第三块：4 个老漏洞代码化 ——

    def test_mixed_code_and_status_pr_is_blocked_on_docs_gate_repos(self):
        from tests.test_ci import FakeApi, pr, st
        state = {'merged': False}
        api = FakeApi(lambda: pr(state='closed', merged=True) if state['merged'] else pr(), st('success'))
        api.docs_gate = True
        api.files = [{'filename': 'src/a.py', 'status': 'changed'}, {'filename': 'STATUS.md', 'status': 'modified'}]
        with self.assertRaisesRegex(Blocked, '同时改了代码和进度表'):
            ci.merge(self.s, 'demo', 1, confirm=True, risk='低', api=api,
                     merger=lambda *a: state.update(merged=True), log_reader=lambda i, l: '随身开发CI模式=full\n')
        self.assertFalse(state['merged'])

        # 项目首次新建 STATUS.md（status='added'）允许同 PR 合入
        api.files = [{'filename': 'src/a.py', 'status': 'added'}, {'filename': 'STATUS.md', 'status': 'added'}]
        out = ci.merge(self.s, 'demo', 1, confirm=True, risk='低', api=api,
                       merger=lambda *a: state.update(merged=True), log_reader=lambda i, l: '随身开发CI模式=full\n')
        self.assertTrue(out['本次合并'])

    def test_trim_status_updates_archives_beyond_20(self):
        items = '\n'.join(f'- 2026-11-{25-i:02d} · 动态第 {i+1} 条' for i in range(25))
        (self.repo / 'STATUS.md').write_text(
            '---\n项目: 演示\n状态: 进行中\n---\n\n# 演示\n\n## 近期动态\n\n' + items + '\n\n## 当前验收结论\n'
        )
        self.assertEqual(board.count_recent_updates((self.repo / 'STATUS.md').read_text()), 25)
        res = board.trim_status_updates(self.repo, max_entries=20, stamp='20261120')
        self.assertEqual((res['total'], res['kept'], res['moved']), (25, 20, 5))
        after = (self.repo / 'STATUS.md').read_text()
        self.assertEqual(board.count_recent_updates(after), 20)
        self.assertIn('动态第 1 条', after)
        self.assertIn('动态第 20 条', after)
        self.assertNotIn('动态第 21 条', after)
        hist = (self.repo / res['archive_path']).read_text()
        self.assertIn('动态第 21 条', hist)
        self.assertIn('动态第 25 条', hist)

    def test_tampered_install_dir_and_overlong_agents_md_are_caught(self):
        # 1. AGENTS.md 超 40 行每周体检点名
        (self.repo / 'AGENTS.md').write_text('\n'.join(f'第 {i} 行' for i in range(45)) + '\n')
        msgs = board.weekly_check(self.s)
        self.assertTrue(any('AGENTS.md 有 45 行（规矩上限 40 行）' in m for m in msgs), msgs)

        # 2. 安装目录手改文件报警
        source = Path(__file__).resolve().parents[1]
        app = self.t / 'installed'
        distribution.build(source, app, 'friend')
        app_store = Store(app)
        self.addCleanup(app_store.db.close)
        (app / 'AGENTS.md').write_text('手改了安装目录入口\n')
        rems = board.backup_reminders(app_store)
        self.assertTrue(any('安装目录文件被手改过' in r for r in rems), rems)


if __name__ == '__main__':
    unittest.main()
