"""子会话选模型与额度协调：入账、环境故障不入账、恢复时间、余额类、Codex 余量门槛、
同池挡住、原子计数并发、崩溃回收、选家顺序、按量拦截、审查换厂商、本机规则文字与 CLI。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from portable import quota, settings
from portable.core import Blocked

SOURCE = Path(__file__).resolve().parents[1]

# 固定「现在」：2026-11-20 12:00 本地时间，测试不随真实时钟漂移
NOW = time.mktime((2026, 11, 20, 12, 0, 0, 0, 0, -1))


def tier(preferred, backup=(), vendor='OpenAI', pool='codex', billing='subscription', name='gpt-5'):
    return {'preferred': [dict(model=m, vendor=vendor, pool=pool, billing=billing)
                          for m in (preferred if isinstance(preferred, (list, tuple)) else [preferred])],
            'backup': [dict(model=m, vendor=vendor, pool=pool, billing=billing)
                       for m in (backup if isinstance(backup, (list, tuple)) else [backup]) if m]}


def config(tiers=None, **extra):
    value = json.loads(json.dumps(quota.DEFAULT_QUOTA))
    if tiers:
        value['tiers'] = tiers
    value.update(extra)
    return value


class QuotaTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    # ---------- 入账 ----------

    def test_rate_limit_recorded_with_recovery_time_and_sanitized_note(self):
        result = quota.record_error(self.root, 'OpenAI', 'gpt-5.1', 'codex',
                                    '429 rate limit exceeded, resets at 2026-11-26 19:03. '
                                    'api_key=sk-abc12345678901234567890', task='task-1', now=NOW)
        self.assertIs(result['入账'], True)
        self.assertEqual(result['类型'], '限流')
        self.assertTrue(result['恢复'].startswith('2026-11-26 19:03'))
        ledger = quota.load_ledger(self.root)
        self.assertEqual(len(ledger['records']), 1)
        record = ledger['records'][0]
        self.assertEqual((record['家'], record['模型'], record['池'], record['任务']),
                         ('OpenAI', 'gpt-5.1', 'codex', 'task-1'))
        self.assertIn('429 rate limit exceeded', record['说明'])
        self.assertIn('[凭据已脱敏]', record['说明'])
        self.assertNotIn('sk-abc12345678901234567890', record['说明'])

    def test_env_failure_not_recorded_even_with_quota_words(self):
        for text in ('host executable was not found',
                     '额度用完，请充值；host executable was not found',
                     'command not found: codex'):
            with self.subTest(text=text):
                result = quota.record_error(self.root, 'OpenAI', 'gpt-5.1', 'codex', text, now=NOW)
                self.assertIs(result['入账'], False)
        self.assertEqual(quota.load_ledger(self.root)['records'], [])

    def test_timeout_exceeded_and_permission_errors_not_recorded(self):
        for text in ('request timeout exceeded after 30s',
                     'insufficient permissions to write file'):
            with self.subTest(text=text):
                result = quota.record_error(self.root, 'OpenAI', 'gpt-5.1', 'codex', text, now=NOW)
                self.assertIs(result['入账'], False)
        self.assertEqual(quota.load_ledger(self.root)['records'], [])

    def test_enoent_and_429_in_one_message_still_recorded(self):
        result = quota.record_error(
            self.root, 'OpenAI', 'gpt-5.1', 'codex',
            'ENOENT: no such file or directory; 429 rate limit exceeded', now=NOW)
        self.assertIs(result['入账'], True)
        self.assertEqual(result['类型'], '限流')
        self.assertIn('429', result['记录']['说明'])
        self.assertNotIn('ENOENT', result['记录']['说明'])

    def test_recovery_time_unknown_when_text_has_no_time(self):
        result = quota.record_error(self.root, 'OpenAI', 'gpt-5.1', 'codex',
                                    'rate limit exceeded, please try later', now=NOW)
        self.assertEqual(result['恢复'], '未知')

    def test_balance_error_records_recharge_and_blocks_until_cleared(self):
        result = quota.record_error(self.root, 'DeepSeek', 'deepseek/deepseek-chat', 'deepseek',
                                    '402 Insufficient Balance 余额不足', now=NOW)
        self.assertIs(result['入账'], True)
        self.assertEqual(result['类型'], '余额')
        self.assertEqual(result['恢复'], '待充值')
        blocks, _ = quota.blocked_pools(self.root, config(), now=NOW)
        self.assertEqual(blocks['deepseek']['恢复'], '待充值')
        quota.clear_pool(self.root, 'deepseek')
        blocks, _ = quota.blocked_pools(self.root, config(), now=NOW)
        self.assertNotIn('deepseek', blocks)

    def test_expired_rate_limit_no_longer_blocks_but_stays_in_ledger(self):
        quota.record_error(self.root, 'OpenAI', 'gpt-5.1', 'codex',
                           'rate limit exceeded, resets at 2026-11-19 19:03', now=NOW)
        blocks, _ = quota.blocked_pools(self.root, config(), now=NOW)
        self.assertNotIn('codex', blocks)
        self.assertEqual(len(quota.load_ledger(self.root)['records']), 1)

    # ---------- Codex 余量读数 ----------

    def write_codex_reading(self, used_percent, resets_at, mtime):
        directory = self.root / 'fake-sessions' / 'abc'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'session.jsonl'
        path.write_text(json.dumps({
            'timestamp': mtime,
            'rate_limits': {'primary': {'used_percent': used_percent,
                                        'resets_at': resets_at, 'window_minutes': 10080}},
        }) + '\n', encoding='utf-8')
        os.utime(path, (mtime, mtime))
        return path

    def codex_blocks(self, used_percent, resets_at='2026-11-26T19:03:00Z', age_hours=0):
        current = config(codex_pools=['codex'])
        self.write_codex_reading(used_percent, resets_at, NOW - age_hours * 3600)
        blocks, reading = quota.blocked_pools(self.root, current, now=NOW,
                                              sessions_dir=str(self.root / 'fake-sessions'))
        return blocks, reading

    def test_codex_floor_39_blocks_and_41_passes(self):
        blocks, reading = self.codex_blocks(61)  # 剩余 39% < 40%
        self.assertEqual(reading['状态'], 'ok')
        self.assertIn('codex', blocks)
        self.assertEqual(blocks['codex']['来源'], 'codex读数')
        self.assertEqual(blocks['codex']['恢复'], '2026-11-26T19:03:00Z')
        blocks, _ = self.codex_blocks(59)  # 剩余 41%
        self.assertNotIn('codex', blocks)
        blocks, _ = self.codex_blocks(60)  # 剩余恰好 40%，不低于门槛
        self.assertNotIn('codex', blocks)

    def test_codex_nested_payload_unix_reset_blocks_like_a_fresh_reading(self):
        current = config(codex_pools=['codex'])
        directory = self.root / 'fake-sessions'
        directory.mkdir()
        reset_at = int(NOW) + 86400
        path = directory / 'session.jsonl'
        path.write_text(json.dumps({
            'timestamp': NOW, 'type': 'event',
            'payload': {'rate_limits': {'primary': {
                'used_percent': 61, 'resets_at': reset_at, 'window_minutes': 10080}}},
        }) + '\n', encoding='utf-8')
        os.utime(path, (NOW, NOW))
        blocks, reading = quota.blocked_pools(self.root, current, now=NOW, sessions_dir=str(directory))
        self.assertEqual(reading['状态'], 'ok')
        self.assertEqual(reading['已用百分比'], 61)
        self.assertEqual(blocks['codex']['来源'], 'codex读数')
        self.assertEqual(blocks['codex']['恢复'],
                         time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(reset_at)))

    def test_codex_reading_stale_over_six_hours_is_unknown_and_does_not_block(self):
        blocks, reading = self.codex_blocks(99, age_hours=7)
        self.assertEqual(reading['状态'], 'stale')
        self.assertNotIn('codex', blocks)

    def test_codex_path_configurable_and_missing_dir_reports_none(self):
        current = config(codex_pools=['codex'])
        blocks, reading = quota.blocked_pools(self.root, current, now=NOW,
                                              sessions_dir=str(self.root / 'not-there'))
        self.assertEqual(reading['状态'], 'none')
        self.assertNotIn('codex', blocks)

    def test_codex_staleness_follows_reading_file_not_newer_sibling(self):
        current = config(codex_pools=['codex'])
        directory = self.root / 'fake-sessions'
        old = directory / 'old'
        old.mkdir(parents=True)
        fresh = directory / 'fresh'
        fresh.mkdir()
        reading_path = old / 'session.jsonl'
        reading_path.write_text(json.dumps({
            'timestamp': NOW - 7 * 3600,
            'rate_limits': {'primary': {'used_percent': 90,
                                        'resets_at': int(NOW) + 86400, 'window_minutes': 10080}},
        }) + '\n', encoding='utf-8')
        os.utime(reading_path, (NOW - 7 * 3600, NOW - 7 * 3600))
        newer = fresh / 'note.jsonl'
        newer.write_text(json.dumps({'timestamp': NOW, 'type': 'note'}) + '\n', encoding='utf-8')
        os.utime(newer, (NOW, NOW))
        blocks, reading = quota.blocked_pools(self.root, current, now=NOW, sessions_dir=str(directory))
        self.assertEqual(reading['状态'], 'stale')
        self.assertNotIn('codex', blocks)

    def test_codex_staleness_uses_reading_line_timestamp(self):
        directory = self.root / 'fake-sessions'
        directory.mkdir()
        path = directory / 'session.jsonl'
        path.write_text(json.dumps({
            'timestamp': NOW - 7 * 3600,
            'rate_limits': {'primary': {'used_percent': 90,
                                        'resets_at': int(NOW) + 86400, 'window_minutes': 10080}},
        }) + '\n', encoding='utf-8')
        os.utime(path, (NOW, NOW))  # 文件被碰过，读数行仍是 7 小时前
        blocks, reading = quota.blocked_pools(self.root, config(codex_pools=['codex']), now=NOW,
                                              sessions_dir=str(directory))
        self.assertEqual(reading['状态'], 'stale')
        self.assertNotIn('codex', blocks)

    def test_codex_reset_at_or_before_now_is_unknown_and_does_not_block(self):
        for resets_at in (NOW, NOW - 60, '2020-01-01T00:00:00Z'):
            with self.subTest(resets_at=resets_at):
                blocks, reading = self.codex_blocks(99, resets_at=resets_at)
                self.assertEqual(reading['状态'], 'unknown')
                self.assertIn('未知', reading['说明'])
                self.assertNotIn('codex', blocks)

    def test_codex_stat_oserror_reports_none(self):
        directory = self.root / 'fake-sessions'
        directory.mkdir()
        path = directory / 'session.jsonl'
        path.write_text(json.dumps({
            'rate_limits': {'primary': {'used_percent': 90, 'resets_at': int(NOW) + 86400,
                                        'window_minutes': 10080}},
        }) + '\n', encoding='utf-8')
        real = quota.Path.stat

        def boom(self, *args, **kwargs):
            if self.name.endswith('.jsonl'):
                raise OSError('stat failed')
            return real(self, *args, **kwargs)

        with patch.object(quota.Path, 'stat', boom):
            reading = quota.read_codex_rate(str(directory), now=NOW)
        self.assertEqual(reading['状态'], 'none')

    # ---------- 选家 ----------

    def test_preferred_first_then_backup_and_same_pool_blocks_both(self):
        current = config(tiers={
            '重': tier(['gpt-5', 'gpt-6']),
            '普通': tier(['gpt-5'], backup=['gpt-6']),
            '轻': tier(['gpt-5']),
            '审查': tier(['gpt-5'], vendor='OpenAI'),
        }, metered_deepseek=[])
        chosen = quota.pick(self.root, current, '普通', now=NOW)
        self.assertEqual((chosen['状态'], chosen['模型']), ('ok', 'gpt-5'))
        # 撞一个型号，同池全部挡住 → 排队
        quota.record_error(self.root, 'OpenAI', 'gpt-5', 'codex', 'rate limit exceeded', now=NOW)
        chosen = quota.pick(self.root, current, '普通', now=NOW)
        self.assertEqual(chosen['状态'], '排队')
        # 放行后重新可选
        quota.clear_pool(self.root, 'codex')
        chosen = quota.pick(self.root, current, '普通', now=NOW)
        self.assertEqual((chosen['状态'], chosen['模型']), ('ok', 'gpt-5'))

    def test_subscription_wins_over_metered_regardless_of_position(self):
        current = config(tiers={
            '重': {'preferred': [{'model': 'deepseek/deepseek-chat', 'vendor': 'DeepSeek',
                                  'pool': 'deepseek', 'billing': 'metered'}],
                   'backup': [{'model': 'gpt-5', 'vendor': 'OpenAI', 'pool': 'codex',
                               'billing': 'subscription'}]},
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': tier(['gpt-5']),
        })
        chosen = quota.pick(self.root, current, '重', now=NOW)
        self.assertEqual((chosen['状态'], chosen['模型'], chosen['计费']),
                         ('ok', 'gpt-5', 'subscription'))

    def test_available_list_filters_and_resolves_wildcard(self):
        current = config(tiers={
            '重': tier(['gpt-5']),
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': tier(['gpt-5']),
        }, metered_deepseek=[{'model': 'deepseek/*', 'vendor': 'DeepSeek', 'pool': 'deepseek',
                              'billing': 'metered'}])
        # 首选列不出来 → 重活先走按量直连 DeepSeek（当场能列出的型号）
        chosen = quota.pick(self.root, current, '重', now=NOW,
                            available=['openai/gpt-9', 'deepseek/deepseek-reasoner'])
        self.assertEqual((chosen['状态'], chosen['模型'], chosen['计费']),
                         ('ok', 'deepseek/deepseek-reasoner', 'metered'))
        chosen = quota.pick(self.root, current, '重', now=NOW,
                            available=['openai/gpt-5', 'deepseek/deepseek-reasoner'])
        self.assertEqual((chosen['状态'], chosen['模型']), ('ok', 'openai/gpt-5'))
        # 包月被挡时按量通配符解析成当场能列出的型号
        quota.record_error(self.root, 'OpenAI', 'gpt-5', 'codex', 'rate limit exceeded', now=NOW)
        chosen = quota.pick(self.root, current, '重', now=NOW,
                            available=['openai/gpt-5', 'deepseek/deepseek-reasoner'])
        self.assertEqual((chosen['状态'], chosen['模型'], chosen['计费']),
                         ('ok', 'deepseek/deepseek-reasoner', 'metered'))
        self.assertIn('提示', chosen)

    def test_light_queues_and_heavy_reports_when_all_blocked(self):
        current = config(tiers={
            '重': tier(['gpt-5']),
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': tier(['gpt-5']),
        }, metered_deepseek=[])
        quota.record_error(self.root, 'OpenAI', 'gpt-5', 'codex', 'rate limit exceeded', now=NOW)
        for tier_name in ('轻', '普通'):
            self.assertEqual(quota.pick(self.root, current, tier_name, now=NOW)['状态'], '排队')
        self.assertEqual(quota.pick(self.root, current, '重', now=NOW)['状态'], '报用户')

    def test_metered_usage_records_ledger_entry_and_light_never_uses_metered(self):
        current = config(tiers={
            '重': tier(['gpt-5']),
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': tier(['gpt-5']),
        }, metered_deepseek=[{'model': 'deepseek/*', 'vendor': 'DeepSeek', 'pool': 'deepseek',
                              'billing': 'metered'}])
        quota.record_error(self.root, 'OpenAI', 'gpt-5', 'codex', 'rate limit exceeded', now=NOW)
        chosen = quota.pick(self.root, current, '普通', task='task-2', now=NOW)
        self.assertEqual((chosen['状态'], chosen['计费']), ('ok', 'metered'))
        self.assertIn('要花钱', chosen['提示'])
        chosen = quota.pick(self.root, current, '轻', now=NOW)
        self.assertEqual(chosen['状态'], '排队')
        types = [r['类型'] for r in quota.load_ledger(self.root)['records']]
        self.assertIn('按量', types)
        # 按量也不可用：普通排队、重报用户
        quota.record_error(self.root, 'DeepSeek', 'deepseek/deepseek-chat', 'deepseek',
                           'Insufficient Balance', now=NOW)
        self.assertEqual(quota.pick(self.root, current, '普通', now=NOW)['状态'], '排队')
        self.assertEqual(quota.pick(self.root, current, '重', now=NOW)['状态'], '报用户')

    def test_code_tier_skips_anthropic_and_review_switches_vendor(self):
        current = config(tiers={
            '重': {'preferred': [{'model': 'claude-opus', 'vendor': 'Anthropic', 'pool': 'cursor',
                                  'billing': 'subscription'}],
                   'backup': [{'model': 'gpt-5', 'vendor': 'OpenAI', 'pool': 'codex',
                               'billing': 'subscription'}]},
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': {'preferred': [
                {'model': 'claude-sonnet-cursor', 'vendor': 'Anthropic', 'pool': 'cursor',
                 'billing': 'subscription'},
                {'model': 'gpt-5', 'vendor': 'OpenAI', 'pool': 'codex', 'billing': 'subscription'}],
                'backup': []},
        })
        chosen = quota.pick(self.root, current, '重', now=NOW)
        self.assertEqual(chosen['模型'], 'gpt-5')  # 写代码档不用 Anthropic 家
        chosen = quota.pick(self.root, current, '审查', writer_vendor='openai', now=NOW)
        self.assertEqual((chosen['模型'], chosen['审查同家']), ('claude-sonnet-cursor', False))
        chosen = quota.pick(self.root, current, '审查', writer_vendor='anthropic', now=NOW)
        self.assertEqual((chosen['模型'], chosen['审查同家']), ('gpt-5', False))

    def test_review_falls_back_to_same_vendor_with_flag_when_only_one(self):
        current = config(tiers={
            '重': tier(['gpt-5']),
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': tier(['gpt-5']),
        })
        chosen = quota.pick(self.root, current, '审查', writer_vendor='openai', now=NOW)
        self.assertIs(chosen['审查同家'], True)
        self.assertIn('同一家', chosen['提示'])

    def test_review_uses_metered_only_when_no_subscription_and_records_it(self):
        metered = {'model': 'deepseek/deepseek-chat', 'vendor': 'DeepSeek', 'pool': 'deepseek',
                   'billing': 'metered'}
        current = config(tiers={
            '重': tier(['gpt-5']),
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': {'preferred': [metered, {'model': 'gpt-5', 'vendor': 'OpenAI', 'pool': 'codex',
                                            'billing': 'subscription'}], 'backup': []},
        }, metered_deepseek=[])
        chosen = quota.pick(self.root, current, '审查', writer_vendor='openai', now=NOW)
        self.assertEqual((chosen['模型'], chosen['计费']), ('gpt-5', 'subscription'))
        self.assertNotIn('要花钱', chosen.get('提示', ''))
        quota.record_error(self.root, 'OpenAI', 'gpt-5', 'codex', 'rate limit exceeded', now=NOW)
        chosen = quota.pick(self.root, current, '审查', writer_vendor='openai', task='rev-1', now=NOW)
        self.assertEqual((chosen['状态'], chosen['模型'], chosen['计费']),
                         ('ok', 'deepseek/deepseek-chat', 'metered'))
        self.assertIn('要花钱', chosen['提示'])
        self.assertIs(chosen['审查同家'], False)
        self.assertIn('按量', [r['类型'] for r in quota.load_ledger(self.root)['records']])

    def test_config_without_cursor_uses_remaining_vendors(self):
        """删掉配置里的某一条线路后，选家只用剩下的家；程序源码也不写死这条线路。"""
        for name in ('quota.py', 'settings.py'):
            text = (SOURCE / 'portable' / name).read_text(encoding='utf-8').lower()
            self.assertNotIn('cursor', text)
        current = config(tiers={
            '重': {'preferred': [
                {'model': 'claude-opus', 'vendor': 'Anthropic', 'pool': 'claude-sub',
                 'billing': 'subscription'}],
                'backup': [
                    {'model': 'gpt-5', 'vendor': 'OpenAI', 'pool': 'codex', 'billing': 'subscription'},
                    {'model': 'deepseek-pro', 'vendor': 'DeepSeek', 'pool': 'opencode-deepseek',
                     'billing': 'subscription'}]},
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': {'preferred': [
                {'model': 'gemini-pro', 'vendor': 'Google', 'pool': 'gemini', 'billing': 'subscription'},
                {'model': 'deepseek-pro', 'vendor': 'DeepSeek', 'pool': 'opencode-deepseek',
                 'billing': 'subscription'}],
                'backup': [
                    {'model': 'gpt-5', 'vendor': 'OpenAI', 'pool': 'codex', 'billing': 'subscription'}]},
        }, metered_deepseek=[])
        self.assertNotIn('cursor', json.dumps(current).lower())
        chosen = quota.pick(self.root, current, '重', now=NOW)
        self.assertEqual((chosen['状态'], chosen['模型']), ('ok', 'gpt-5'))
        chosen = quota.pick(self.root, current, '审查', writer_vendor='openai', now=NOW)
        self.assertEqual((chosen['状态'], chosen['模型'], chosen['审查同家']),
                         ('ok', 'gemini-pro', False))
        quota.record_error(self.root, 'Google', 'gemini-pro', 'gemini', 'rate limit exceeded', now=NOW)
        chosen = quota.pick(self.root, current, '审查', writer_vendor='openai', now=NOW)
        self.assertEqual((chosen['状态'], chosen['模型'], chosen['审查同家']),
                         ('ok', 'deepseek-pro', False))

    def test_claude_api_active_never_enters_subsession_pool(self):
        current = config(tiers={
            '重': {'preferred': [
                {'model': 'claude-api-active', 'vendor': 'OpenAI', 'pool': 'claude-api',
                 'billing': 'subscription'}],
                'backup': [{'model': 'gpt-5', 'vendor': 'OpenAI', 'pool': 'codex',
                            'billing': 'subscription'}]},
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': {'preferred': [
                {'model': 'claude-api-active', 'vendor': 'Anthropic', 'pool': 'claude-api',
                 'billing': 'subscription'}],
                'backup': [{'model': 'gemini-pro', 'vendor': 'Google', 'pool': 'gemini',
                            'billing': 'subscription'}]},
        })
        with self.assertRaises(Blocked):
            quota.check_config(current)
        chosen = quota.pick(self.root, current, '重', now=NOW)
        self.assertEqual(chosen['模型'], 'gpt-5')
        chosen = quota.pick(self.root, current, '审查', writer_vendor='openai', now=NOW)
        self.assertEqual((chosen['模型'], chosen['审查同家']), ('gemini-pro', False))

    def test_priority_project_queued_task_is_marked_first(self):
        current = config(tiers={
            '重': tier(['gpt-5']),
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': tier(['gpt-5']),
        }, metered_deepseek=[], priority_project='ERP')
        quota.record_error(self.root, 'OpenAI', 'gpt-5', 'codex', 'rate limit exceeded', now=NOW)
        queued = quota.pick(self.root, current, '普通', project='ERP', now=NOW)
        self.assertEqual(queued['状态'], '排队')
        self.assertIs(queued['优先'], True)
        other = quota.pick(self.root, current, '普通', project='其他', now=NOW)
        self.assertEqual(other['状态'], '排队')
        self.assertNotIn('优先', other)

    def test_metered_model_must_be_deepseek_in_config(self):
        with self.assertRaises(Blocked):
            quota.check_config(config(tiers={
                '重': {'preferred': [{'model': 'other/paid', 'vendor': 'X', 'pool': 'p',
                                      'billing': 'metered'}], 'backup': []},
                '普通': tier(['gpt-5']),
                '轻': tier(['gpt-5']),
                '审查': tier(['gpt-5']),
            }))

    # ---------- 全机名额 ----------

    def test_occupy_counts_under_one_lock_and_release(self):
        first = quota.occupy(self.root, 'task-a', 2, now=NOW)
        self.assertIs(first['占位'], True)
        self.assertEqual(quota.occupy(self.root, 'task-b', 2, now=NOW)['占位'], True)
        blocked = quota.occupy(self.root, 'task-c', 2, now=NOW)
        self.assertIs(blocked['占位'], False)
        self.assertEqual(blocked['在跑任务'], ['task-a', 'task-b'])
        quota.release(self.root, 'task-a')
        self.assertIs(quota.occupy(self.root, 'task-c', 2, now=NOW)['占位'], True)

    def test_occupy_eight_processes_at_barrier_take_exactly_three(self):
        ready_dir = self.root / 'ready'
        ready_dir.mkdir()
        go = self.root / 'go'
        script = (
            'import json, sys, time\n'
            'from pathlib import Path\n'
            'from portable.quota import occupy\n'
            'root, task, ready, go = sys.argv[1:]\n'
            'Path(ready).write_text("1", encoding="utf-8")\n'
            'deadline = time.time() + 30\n'
            'while not Path(go).exists():\n'
            '    if time.time() > deadline:\n'
            '        raise SystemExit("barrier timeout")\n'
            '    time.sleep(0.01)\n'
            'print(json.dumps(occupy(root, task, 3)))\n'
        )
        processes = [subprocess.Popen(
            [sys.executable, '-B', '-c', script, str(self.root), f'task-{index}',
             str(ready_dir / str(index)), str(go)],
            cwd=SOURCE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for index in range(8)]
        deadline = time.time() + 30
        while len(list(ready_dir.iterdir())) < 8:
            if time.time() > deadline:
                details = []
                for process in processes:
                    process.kill()
                    out, err = process.communicate(timeout=5)
                    details.append(err or out)
                self.fail('workers did not reach the barrier: ' + ' | '.join(details))
            time.sleep(0.01)
        go.write_text('go', encoding='utf-8')
        results = []
        for process in processes:
            out, err = process.communicate(timeout=90)
            self.assertEqual(process.returncode, 0, err or out)
            results.append(json.loads(out))
        self.assertEqual(sum(1 for item in results if item['占位']), 3)
        self.assertEqual(sum(1 for item in results if not item['占位']), 5)
        self.assertEqual(len(quota.load_ledger(self.root)['占用']), 3)

    def test_occupy_reclaims_ended_and_unknown_over_24_hours(self):
        self.assertIs(quota.occupy(self.root, 'task-1', 1, now=NOW)['占位'], True)
        # 查得到已结束 → 当场回收
        released = quota.occupy(self.root, 'task-2', 1, now=NOW, statuses={'task-1': 'ended'})
        self.assertIs(released['占位'], True)
        # 查不到且不到 24 小时 → 保留占位
        quota.release(self.root, 'task-2')
        quota.occupy(self.root, 'task-3', 1, now=NOW)
        blocked = quota.occupy(self.root, 'task-4', 1, now=NOW + 3600)
        self.assertIs(blocked['占位'], False)
        # 查不到且超过 24 小时 → 回收
        released = quota.occupy(self.root, 'task-4', 1, now=NOW + 25 * 3600)
        self.assertIs(released['占位'], True)
        self.assertNotIn('task-3', released['在跑任务'])

    # ---------- 主会话接管（3.5） ----------

    def test_takeover_error_detection(self):
        self.assertIs(quota.takeover_error('You have reached your 5-hour usage limit for Claude.')['命中'], True)
        self.assertIs(quota.takeover_error('You have reached your 5 小时 额度，请稍后再试。')['命中'], True)
        self.assertIs(quota.takeover_error('host executable was not found')['命中'], False)
        self.assertIs(quota.takeover_error('代码写完了，测试全绿')['命中'], False)

    def test_record_takeover_and_today_count(self):
        entry = quota.record_takeover(self.root, '主会话窗口', model='opus', vendor='Anthropic', now=NOW)
        self.assertEqual(entry['接管']['窗口'], '主会话窗口')
        shown = quota.show(self.root, config(), now=NOW)
        self.assertEqual(shown['今日接管'], 1)

    # ---------- 简报 ----------

    def test_show_counts_yesterday_dispatch_per_vendor_and_brief_line(self):
        current = config(tiers={
            '重': tier(['gpt-5']),
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': tier(['gpt-5']),
        }, metered_deepseek=[])
        yesterday = NOW - 25 * 3600
        today = NOW + 3600
        quota.pick(self.root, current, '重', task='t1', now=yesterday, record=True)
        quota.pick(self.root, current, '普通', task='t2', now=yesterday, record=True)
        quota.pick(self.root, current, '轻', task='t3', now=today, record=True)
        quota.record_error(self.root, 'OpenAI', 'gpt-5', 'codex',
                           'rate limit exceeded, resets at 2026-11-26 19:03', now=yesterday)
        shown = quota.show(self.root, current, now=today)
        self.assertEqual(shown['昨日单数'], {'OpenAI': 2})
        self.assertIn('codex', shown['被挡的池'])
        self.assertIn('昨日派单：OpenAI 2 单', shown['简报一行'])
        self.assertIn('codex（2026-11-26 19:03:00 恢复）', shown['简报一行'])
        empty = quota.show(self.root, current, now=today + 48 * 3600)
        self.assertIn('昨日派单：无', empty['简报一行'])

    # ---------- 公开版默认表 ----------

    def test_public_default_works_with_only_one_vendor_and_no_local_models(self):
        default = json.loads(json.dumps(quota.DEFAULT_QUOTA))
        for tier_name in quota.TIERS:
            with self.subTest(tier=tier_name):
                chosen = quota.pick(self.root, default, tier_name, now=NOW)
                self.assertEqual(chosen['状态'], 'ok')
                self.assertEqual(chosen['模型'], 'default')
        self.assertEqual(default['metered_deepseek'], [])
        text = json.dumps(default, ensure_ascii=False)
        self.assertNotIn('opencode', text)
        self.assertNotIn('gpt-', text)

    # ---------- 本机规则文字 ----------

    def render(self, **value):
        store = SimpleNamespace(root=self.root)
        path = settings.render_rules(store, value)
        return Path(path).read_text(encoding='utf-8')

    def _assert_rules_hide_model_names(self, rules):
        for model in ('gpt', 'opus', 'sonnet', 'deepseek-chat', 'deepseek-pro'):
            self.assertNotIn(model, rules)

    def test_dispatch_rules_without_metered_outlet_queue_light_and_normal(self):
        sentence = '包月全被挡时，轻活和普通活排队，重活报用户'
        for mode in ('host', 't3'):
            with self.subTest(mode=mode):
                rules = self.render(dispatch_mode=mode)
                self.assertIn('按档位选模型', rules)
                self.assertIn('额度池按台账跳过', rules)
                self.assertIn(sentence, rules)
                self.assertNotIn('按量出口', rules)
                self.assertIn('quota show', rules)
                self._assert_rules_hide_model_names(rules)
        rules = self.render(dispatch_mode='solo')
        self.assertNotIn('按档位选模型', rules)
        rules = self.render(dispatch_mode=None, team_enabled=True, team_entry='dispatch-example')
        self.assertNotIn('按档位选模型', rules)

    def test_dispatch_rules_with_metered_outlet_name_configured_vendor(self):
        outlet = {'model': 'deepseek/*', 'vendor': 'DeepSeek', 'pool': 'deepseek', 'billing': 'metered'}
        for mode in ('host', 't3'):
            with self.subTest(mode=mode):
                rules = self.render(dispatch_mode=mode, quota=config(metered_deepseek=[outlet]))
                self.assertIn('按量出口〈DeepSeek〉', rules)
                self.assertIn('记台账并告诉用户一句', rules)
                self.assertIn('它也不可用时轻活和普通活排队、重活报用户', rules)
                self.assertNotIn('包月全被挡时，轻活和普通活排队，重活报用户', rules)
                self._assert_rules_hide_model_names(rules)

    def test_quota_setting_validates_and_round_trips(self):
        store = SimpleNamespace(root=self.root)
        custom = config(tiers={
            '重': tier(['gpt-5']),
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': tier(['gpt-5']),
        })
        saved = settings.save(store, {'quota': custom})
        self.assertEqual(saved['quota']['tiers']['重'], custom['tiers']['重'])
        path = settings.path(store)
        before = path.read_bytes()
        for bad in ({'quota': {'gpt_floor_percent': 101}},
                    {'quota': {'tiers': {}}},
                    {'quota': {'metered_deepseek': [{'model': 'other/x', 'vendor': 'X',
                                                    'pool': 'p', 'billing': 'metered'}]}}):
            with self.subTest(bad=bad), self.assertRaises(Blocked):
                settings.set_value(store, 'quota', bad['quota'])
        self.assertEqual(path.read_bytes(), before)

    def test_load_deepcopies_quota_and_merges_missing_fields(self):
        store = SimpleNamespace(root=self.root)
        fresh = settings.load(store)
        fresh['quota']['gpt_floor_percent'] = 1
        fresh['quota']['metered_deepseek'].append(
            {'model': 'deepseek/x', 'vendor': 'DeepSeek', 'pool': 'deepseek', 'billing': 'metered'})
        fresh['quota']['tiers']['重']['preferred'][0]['model'] = 'changed'
        self.assertEqual(quota.DEFAULT_QUOTA['gpt_floor_percent'], 40)
        self.assertEqual(quota.DEFAULT_QUOTA['metered_deepseek'], [])
        self.assertEqual(quota.DEFAULT_QUOTA['tiers']['重']['preferred'][0]['model'], 'default')
        again = settings.load(store)
        self.assertEqual(again['quota']['gpt_floor_percent'], 40)
        self.assertEqual(again['quota']['metered_deepseek'], [])
        self.assertEqual(again['quota']['tiers']['重']['preferred'][0]['model'], 'default')

        stored = json.loads(json.dumps(quota.DEFAULT_QUOTA))
        del stored['stale_hours']
        del stored['priority_project']
        path = settings.path(store)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'owner_name': '旧', 'quota': stored}, ensure_ascii=False),
                        encoding='utf-8')
        loaded = settings.load(store)
        self.assertEqual(loaded['quota']['stale_hours'], 6)
        self.assertEqual(loaded['quota']['priority_project'], '')
        saved = settings.set_value(store, 'owner_name', '改过')
        self.assertEqual(saved['owner_name'], '改过')
        self.assertEqual(saved['quota']['stale_hours'], 6)
        on_disk = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(on_disk['quota']['stale_hours'], 6)
        self.assertEqual(on_disk['quota']['priority_project'], '')

    # ---------- CLI ----------

    def test_cli_pick_and_show_output_json(self):
        store = SimpleNamespace(root=self.root)
        settings.save(store, {'quota': config(tiers={
            '重': tier(['gpt-5']),
            '普通': tier(['gpt-5']),
            '轻': tier(['gpt-5']),
            '审查': tier(['gpt-5'], vendor='OpenAI'),
        })})
        env = {**os.environ, 'PORTABLE_HOME': str(self.root / 'fake-home'),
               'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, '-B', '-m', 'portable', '--root', str(self.root),
                                 'quota', 'pick', '--tier', '重', '--task', 'task-9'],
                                cwd=SOURCE, capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        chosen = json.loads(result.stdout)
        self.assertEqual((chosen['状态'], chosen['模型'], chosen['任务']), ('ok', 'gpt-5', 'task-9'))
        result = subprocess.run([sys.executable, '-B', '-m', 'portable', '--root', str(self.root),
                                 'quota', 'show'], cwd=SOURCE, capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        shown = json.loads(result.stdout)
        self.assertIn('简报一行', shown)
        self.assertIn('被挡的池', shown)
        self.assertEqual(shown['被挡的池'], {})


if __name__ == '__main__':
    unittest.main()
