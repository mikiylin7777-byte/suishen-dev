import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from portable import board, distribution, settings, upgrade_channel
from portable.core import Blocked, Store, VERSION


SOURCE = Path(__file__).resolve().parents[1]
# Synthetic fixture only. The production fingerprint is checked against the local rule out of band.
MAIN_PROVIDER = '主会话用 示例主模型 规划审核'
LOCAL_QUEUE = '排队记录见该项目 STATUS.md'
ALL_PROVIDERS_UNAVAILABLE = 'T3 所有厂商都派不出去时，记录原报错，告知调度窗口和用户，保持现有入口'
LEGACY = (
    '【窗口只规划审核】窗口只规划和审核，全部交给子会话。'
    f'{MAIN_PROVIDER}，用另一家执行（默认 Codex 示例模型包月；其他也可），每窗口不超过 4 个，'
    '全机总数逐步提高到 12 个（提高前先看本机负载看板，内存吃紧就不加），'
    f'连库测试的全机不超过 3 个，{LOCAL_QUEUE}。'
    '子会话用 DeepSeek 时：OpenCode Go（包月）优先；额度用完或不可用时，'
    '才用 用户自己钥匙直连的 deepseek/*（按量扣费）。'
    f'子会话自报通过不算数。一家派不出去时换下一家；{ALL_PROVIDERS_UNAVAILABLE}。'
    '上线、改真实数据仍按【上线放行】【门禁示例】由指定窗口执行。'
)
EXPECTED = (
    f'【本机 T3 派工的额外约定】{MAIN_PROVIDER}。默认 Codex 示例模型包月。'
    '子会话用 DeepSeek 时：OpenCode Go（包月）优先；额度用完或不可用时，'
    '才用直连的 deepseek/*（按量扣费）。'
    '全机总数逐步提高到 12 个，提高前先看本机负载看板，内存吃紧就不加。'
    f'连库测试的全机不超过 3 个。{LOCAL_QUEUE}。{ALL_PROVIDERS_UNAVAILABLE}。'
    '上线、改真实数据仍按【上线放行】【门禁示例】由指定窗口执行。'
)


class DispatchModeUpgradeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.candidate = Path(temp.name).resolve() / 'candidate'
        distribution.build(SOURCE, cls.candidate, 'friend')

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.root = self.home / 'old'
        self.previous = self.home / 'intermediate'
        shutil.copytree(self.candidate, self.root)
        shutil.copytree(self.candidate, self.previous)
        self.set_version(self.root, '0.2.31')
        self.set_version(self.previous, '0.2.32')
        for override in (patch.object(Path, 'home', return_value=self.home),
                         patch.dict(os.environ, {'HOME': str(self.home), 'CLAUDECODE': '', 'CODEX_THREAD_ID': ''})):
            override.start()
            self.addCleanup(override.stop)

    def set_version(self, root, version):
        path = root / '发行.json'
        release = json.loads(path.read_bytes())
        path.write_text(json.dumps({**release, 'version': version}), encoding='utf-8')

    def configure(self, **value):
        s = Store(self.root)
        try:
            settings.save(s, {key: val for key, val in value.items() if key in settings.KEYS})
            # Preserve the fixture's raw legacy fields and any keys written by a newer version.
            raw = json.loads(settings.path(s).read_bytes())
            settings.path(s).write_text(json.dumps({**raw, **value}, ensure_ascii=False), encoding='utf-8')
        finally:
            s.db.close()

    def use_legacy_rule_renderer(self):
        runtime = self.root / distribution.RUNTIME
        with zipfile.ZipFile(runtime) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        name = 'portable/settings.py'
        files[name] = files[name].decode().replace("'- 【本机派工】'", "'- 【本机战队】'").encode()
        with zipfile.ZipFile(runtime, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        manifest = self.root / '发行.json'
        release = json.loads(manifest.read_bytes())
        release['files'][distribution.RUNTIME] = hashlib.sha256(runtime.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(release), encoding='utf-8')
        rule = self.root / settings.RULES
        rule.write_bytes(rule.read_bytes().replace('【本机派工】'.encode(), '【本机战队】'.encode()))

    def upgrade(self, target=None):
        return upgrade_channel.upgrade(self.root, target or self.candidate, launchctl=lambda *args: True)

    def check_old_install(self, t3, entry, recommendation):
        self.configure(team_enabled=bool(entry), team_entry=entry)
        before = (self.root / settings.FILE).read_bytes()
        with patch.object(upgrade_channel.shutil, 'which', return_value='/fixture/t3' if t3 else None), \
             patch.object(upgrade_channel, '_readonly_probe', return_value=True) as probe:
            result = self.upgrade()
        question = result['待选·派工方式']
        self.assertEqual(question['推荐项'], recommendation)
        self.assertEqual(question['default'], recommendation)
        self.assertIn(f'直接回车=推荐的第 {recommendation} 项', question['题目'])
        self.assertIn(f'直接回车=推荐的第 {recommendation} 项', question['默认'])
        self.assertEqual([option['设置值'] for option in question['选项']], ['host', 't3', 'team', 'solo'])
        self.assertEqual(question['当前设置'], {'dispatch_mode': None, 'team_enabled': bool(entry)})
        option = question['选项'][question['default']-1]
        self.assertNotEqual(option['设置值'], 'host')
        self.assertEqual(option['答完后运行'], 'settings set dispatch_mode ' + option['设置值'])
        self.assertIn('登录由本人完成', question['选 T3 的建议'])
        if t3:
            probe.assert_any_call(['/fixture/t3', '--version'])
            self.assertIn('家数未知', question['选项'][1]['查到的情况'])
        if entry:
            probe.assert_any_call([str(self.home / 'team'), '--help'])
            self.assertIn('已启用', question['选项'][2]['查到的情况'])
        self.assertEqual((self.root / settings.FILE).read_bytes(), before)

    def test_old_team_install_recommends_team(self):
        self.check_old_install(False, '~/team', 3)

    def test_old_t3_and_team_install_recommends_t3(self):
        self.check_old_install(True, '~/team', 2)

    def test_old_install_with_no_helpers_recommends_solo(self):
        self.check_old_install(False, '', 4)

    def test_host_preferred_to_team_when_t3_probe_fails(self):
        self.configure(team_enabled=True, team_entry='"~/with space/team" dispatch')
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'fixture'}), \
             patch.object(upgrade_channel.shutil, 'which', return_value='/fixture/t3'), \
             patch.object(upgrade_channel, '_readonly_probe', side_effect=[False, True]) as probe:
            result = self.upgrade()['待选·派工方式']
        self.assertEqual(result['推荐项'], 1)
        self.assertEqual(result['default'], 1)
        self.assertIn('CODEX_THREAD_ID', result['选项'][0]['查到的情况'])
        probe.assert_called_with([str(self.home / 'with space/team'), 'dispatch', '--help'])

    def test_chosen_mode_stays_selected_across_two_upgrades(self):
        self.configure(dispatch_mode='host', plan_only=False, max_parallel=2, team_enabled=True,
                       extra_rules=[LEGACY])
        before = (self.root / settings.FILE).read_bytes()
        with patch.object(upgrade_channel, '_dispatch_question') as question:
            for target in (self.previous, self.candidate):
                result = self.upgrade(target)
                self.assertEqual(result['派工方式'], '已选〈host〉')
                self.assertNotIn('待选·派工方式', result)
                self.assertEqual((self.root / settings.FILE).read_bytes(), before)
            question.assert_not_called()

    def test_fingerprint_migration_and_rollback_restore_pair(self):
        legacy_settings = {key: value for key, value in settings.DEFAULTS.items()
                           if key not in upgrade_channel.DISPATCH_KEYS}
        self.configure(**{**legacy_settings, 'extra_rules': ['【朋友规则】保留', LEGACY],
                          'team_enabled': True, 'team_entry': '/fixture/team'})
        self.use_legacy_rule_renderer()
        before = upgrade_channel._settings_pair(self.root)
        previous = json.loads(before[settings.FILE][0])
        self.assertNotIn('max_parallel', previous)
        with patch.object(upgrade_channel, 'LEGACY_DISPATCH_SHA256', hashlib.sha256(LEGACY.encode()).hexdigest()):
            result = self.upgrade()
            migrated = upgrade_channel._settings_pair(self.root)
        self.assertEqual(result['派工方式'], '已选〈t3〉')
        saved = json.loads(migrated[settings.FILE][0])
        self.assertEqual(saved['extra_rules'], ['【朋友规则】保留', EXPECTED])
        self.assertEqual((saved['dispatch_mode'], saved['plan_only'], saved['t3_sub_provider']), ('t3', True, 'codex'))
        self.assertEqual(settings.effective_dispatch(saved), ('t3', True))
        self.assertEqual((saved['team_enabled'], saved['team_entry']), (False, '/fixture/team'))
        rules = migrated[settings.RULES][0].decode()
        self.assertIn(EXPECTED, rules)
        self.assertNotIn(LEGACY, rules)
        dispatch_rule, = (line for line in rules.splitlines() if line.startswith('- 【本机派工】'))
        self.assertIn('不超过 4 个', dispatch_rule)
        upgrade_channel.rollback(self.root, launchctl=lambda *args: True)
        restored = upgrade_channel._settings_pair(self.root)
        rolled_back = json.loads(restored[settings.FILE][0])
        self.assertEqual(rolled_back['extra_rules'], previous['extra_rules'])
        self.assertIs(rolled_back['team_enabled'], True)
        self.assertEqual(settings.effective_dispatch(rolled_back), ('team', False))
        for key in ('dispatch_mode', 'plan_only', 'max_parallel', 't3_sub_provider'):
            if key in previous:
                self.assertEqual(rolled_back[key], previous[key])
            else:
                self.assertNotIn(key, rolled_back)
        self.assertEqual(restored[settings.RULES], before[settings.RULES])
        self.assertEqual(restored[settings.FILE], before[settings.FILE])
        self.assertNotIn('【本机派工】'.encode(), restored[settings.RULES][0])
        self.assertIn('【本机战队】'.encode(), restored[settings.RULES][0])

    def test_rollback_restores_backup_rule_bytes_if_old_renderer_times_out(self):
        self.configure(team_enabled=True)
        self.use_legacy_rule_renderer()
        before = upgrade_channel._settings_pair(self.root)
        self.upgrade()
        timeout = subprocess.TimeoutExpired('旧版规则生成', 15)
        with patch.object(upgrade_channel, '_render_rollback_rules', side_effect=timeout):
            result = upgrade_channel.rollback(self.root, launchctl=lambda *args: True)
        self.assertEqual(upgrade_channel._settings_pair(self.root), before)
        self.assertIn('旧版无法生成本机规则，已恢复升级前备份', result['收尾'])

    def test_rollback_keeps_current_rules_if_old_renderer_fails_without_backup(self):
        self.configure(team_enabled=True)
        rule_path = self.root / settings.RULES
        rule_path.unlink()
        upgraded = self.upgrade()
        self.assertNotIn(settings.RULES, upgrade_channel._backup_settings_pair(Path(upgraded['备份'])))
        current = upgrade_channel._settings_pair(self.root)[settings.RULES]

        def fail_after_partial_write(root, stage, python):
            (stage / settings.RULES).write_text('旧版生成中断', encoding='utf-8')
            raise OSError('旧版规则生成失败')

        with patch.object(upgrade_channel, '_render_rollback_rules', side_effect=fail_after_partial_write):
            result = upgrade_channel.rollback(self.root, launchctl=lambda *args: True)
        self.assertEqual(result['退回'], VERSION+' → 0.2.31')
        self.assertEqual(upgrade_channel._settings_pair(self.root)[settings.RULES], current)
        self.assertIn('本机规则没能用旧版重新生成，也没有升级前备份，保留现状，请人看', result['收尾'])
        self.assertNotIn('旧版无法生成本机规则，已恢复升级前备份', result['收尾'])

    def test_extraction_rejects_missing_local_clauses(self):
        for clause in (MAIN_PROVIDER, LOCAL_QUEUE, ALL_PROVIDERS_UNAVAILABLE):
            with self.subTest(clause=clause):
                self.assertIsNone(upgrade_channel._extract_local_dispatch(LEGACY.replace(clause, '')))

    def test_matched_fingerprint_with_incomplete_rule_keeps_settings(self):
        incomplete = LEGACY.replace(LOCAL_QUEUE, '')
        self.configure(extra_rules=[incomplete, '【自定义】保留'])
        before = upgrade_channel._settings_pair(self.root)
        with patch.object(upgrade_channel, 'LEGACY_DISPATCH_SHA256', hashlib.sha256(incomplete.encode()).hexdigest()), \
             patch.object(upgrade_channel.shutil, 'which', return_value=None):
            result = self.upgrade()
        self.assertEqual(result['自检版本'], VERSION)
        self.assertEqual(result['附加规则'], '附加规则没动，请人看')
        self.assertIn('待选·派工方式', result)
        self.assertEqual(upgrade_channel._settings_pair(self.root), before)

    def test_failed_registry_alignment_explains_skipped_dispatch_question(self):
        self.configure(projects=[])
        with patch.object(board, 'apply', side_effect=Blocked('模拟登记失败')), \
             patch.object(upgrade_channel, '_dispatch_question') as question:
            with self.assertRaisesRegex(Blocked, '派工方式还没问，原因：登记失败'):
                self.upgrade()
        question.assert_not_called()

    def test_fingerprint_mismatch_leaves_friend_rule_and_reports_it(self):
        custom = LEGACY + '朋友另有约定。'
        self.configure(extra_rules=[custom, '【自定义】逐字保留'])
        before = upgrade_channel._settings_pair(self.root)
        with patch.object(upgrade_channel, 'LEGACY_DISPATCH_SHA256', hashlib.sha256(LEGACY.encode()).hexdigest()), \
             patch.object(upgrade_channel.shutil, 'which', return_value=None):
            result = self.upgrade()
        self.assertEqual(result['附加规则'], '附加规则没动，请人看')
        self.assertIn('待选·派工方式', result)
        self.assertEqual(upgrade_channel._settings_pair(self.root), before)

    def test_other_friend_rules_stay_byte_for_byte(self):
        self.configure(extra_rules=['【朋友自定义】  保留标点和空格。'], team_enabled=True)
        before = upgrade_channel._settings_pair(self.root)
        with patch.object(upgrade_channel.shutil, 'which', return_value=None):
            result = self.upgrade()
        self.assertNotIn('附加规则', result)
        self.assertEqual(upgrade_channel._settings_pair(self.root), before)

    def test_failure_after_first_replacement_restores_both_files(self):
        self.configure(extra_rules=[LEGACY])
        before = upgrade_channel._settings_pair(self.root)
        actual_atomic = upgrade_channel.atomic
        calls = 0

        def write(path, data):
            nonlocal calls
            if path == self.root / settings.RULES:
                calls += 1
                if calls == 1:
                    written = json.loads((self.root / settings.FILE).read_bytes())
                    self.assertEqual(written['dispatch_mode'], 't3')
                    raise OSError('模拟替换第二个文件失败')
            actual_atomic(path, data)

        with patch.object(upgrade_channel, 'LEGACY_DISPATCH_SHA256', hashlib.sha256(LEGACY.encode()).hexdigest()), \
             patch.object(upgrade_channel, 'atomic', side_effect=write):
            with self.assertRaisesRegex(Blocked, '模拟替换第二个文件失败'):
                self.upgrade()
        self.assertEqual(upgrade_channel._settings_pair(self.root), before)

    def test_rollback_reverts_dispatch_but_keeps_new_project_and_settings(self):
        saved = dict(dispatch_mode='team', plan_only=True, max_parallel=3,
                     t3_sub_provider='former-provider', extra_rules=['升级前规则'])
        self.configure(projects=[], **saved)
        self.use_legacy_rule_renderer()
        self.upgrade()
        self.configure(projects=[], dispatch_mode='solo', plan_only=False, max_parallel=2,
                       t3_sub_provider='new-provider', extra_rules=['升级后规则'],
                       owner_name='升级后改名', team_entry='/fixture/new-team',
                       future_setting={'keep': True})
        project = self.home / 'new-project'
        project.mkdir()
        s = Store(self.root)
        try:
            s.register('升级后新项目', str(project), 'new-project')
        finally:
            s.db.close()
        result = upgrade_channel.rollback(self.root, launchctl=lambda *args: True)
        self.assertEqual(result['退回'], VERSION+' → 0.2.31')
        self.assertNotIn('待选·派工方式', result)
        self.assertIn('项目登记已对齐本机设置', result['收尾'])
        self.assertIn('本机规则已重新生成', result['收尾'])
        restored = json.loads((self.root / settings.FILE).read_bytes())
        self.assertEqual({key: restored[key] for key in saved}, saved)
        self.assertIs(restored['team_enabled'], True)
        self.assertEqual(restored['owner_name'], '升级后改名')
        self.assertEqual(restored['team_entry'], '/fixture/new-team')
        self.assertEqual(restored['future_setting'], {'keep': True})
        self.assertEqual([item['id'] for item in restored['projects']], ['new-project'])
        registry = json.loads((self.root / '数据/项目注册表.json').read_bytes())
        self.assertEqual([item['id'] for item in registry['projects']], ['new-project'])
        rules = (self.root / settings.RULES).read_text(encoding='utf-8')
        self.assertIn('升级后改名', rules)
        self.assertIn('升级前规则', rules)
        self.assertNotIn('升级后规则', rules)
        self.assertNotIn('【本机派工】', rules)
        self.assertIn('【本机战队】', rules)

    def test_rollback_keeps_an_independently_updated_legacy_team_flag(self):
        self.configure(team_enabled=True)
        with patch.object(upgrade_channel.shutil, 'which', return_value=None):
            self.upgrade()
        s = Store(self.root)
        try:
            settings.set_value(s, 'team_enabled', False)
        finally:
            s.db.close()
        upgrade_channel.rollback(self.root, launchctl=lambda *args: True)
        restored = json.loads((self.root / settings.FILE).read_bytes())
        self.assertIsNone(restored.get('dispatch_mode'))
        self.assertIs(restored['team_enabled'], False)
        self.assertEqual(settings.effective_dispatch(restored), ('solo', False))

    def test_help_probe_timeout_counts_as_unavailable(self):
        with patch.object(upgrade_channel.sys, 'platform', 'darwin'), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(upgrade_channel.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['team'], 10)) as run:
            self.assertFalse(upgrade_channel._readonly_probe(['team', '--help']))
        self.assertEqual(run.call_args.kwargs['timeout'], 10)
        self.assertIn('(deny network*)', run.call_args.args[0][2])
        self.assertEqual(run.call_args.args[0][-2:], ['team', '--help'])
