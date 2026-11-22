import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from portable import settings, wizard
from portable.core import Blocked, Store


class DispatchModeSettingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = SimpleNamespace(root=Path(tmp.name).resolve())

    def render(self, **value):
        path = settings.render_rules(self.store, value)
        return Path(path).read_text(encoding='utf-8')

    def test_each_mode_renders_only_its_dispatch_instructions(self):
        markers = {
            'host': '用 AI 工具自带的子 Agent 派工',
            't3': '用 T3 的 delegate_task 派给子会话',
            'team': '派工入口命令：dispatch-example',
            'solo': '由当前对话里的 AI 自己动手做',
        }
        for mode, marker in markers.items():
            with self.subTest(mode=mode):
                rules = self.render(dispatch_mode=mode, team_enabled=True,
                                    team_entry='dispatch-example')
                self.assertEqual(rules.count('【本机派工】'), 1)
                self.assertNotIn('【本机战队】', rules)
                self.assertIn(marker, rules)
                for other_mode, other_marker in markers.items():
                    if other_mode != mode:
                        self.assertNotIn(other_marker, rules)
                self.assertEqual('【战队续接】' in rules, mode == 'team')
                self.assertEqual('【交接包·战队】' in rules, mode == 'team')
                self.assertEqual('窗口只规划审核：' in rules, mode != 'solo')
                self.assertNotIn('一个窗口能做完的自己写', rules)
                if mode != 'solo':
                    self.assertIn('派不出去报用户，不改回自己写', rules)

    def test_effective_plan_only_respects_selection_and_solo(self):
        self.assertEqual(settings.effective_dispatch({}), ('solo', False))
        for plan_only in (None, False, True):
            with self.subTest(plan_only=plan_only):
                for mode in ('host', 't3', 'team', 'solo'):
                    value = {'dispatch_mode': mode, 'plan_only': plan_only}
                    expected = mode != 'solo' and plan_only is not False
                    self.assertEqual(settings.effective_dispatch(value), (mode, expected))
                self.assertEqual(settings.effective_dispatch({
                    'dispatch_mode': None, 'plan_only': plan_only, 'team_enabled': True,
                }), ('team', False))
        self.assertNotIn('窗口只规划审核', self.render(dispatch_mode='solo', plan_only=True))
        self.assertNotIn('窗口只规划审核', self.render(dispatch_mode='host', plan_only=False))

    def test_plan_only_recovery_hint_is_for_host_and_t3(self):
        hint = '告诉用户可以说一句换派工方式或关掉只规划审核（settings set dispatch_mode … / settings set plan_only false）。'
        for mode in ('host', 't3', 'team', 'solo'):
            for plan_only in (True, False):
                with self.subTest(mode=mode, plan_only=plan_only):
                    rules = self.render(dispatch_mode=mode, plan_only=plan_only)
                    if mode in ('host', 't3') and plan_only:
                        self.assertIn('派不出去报用户，不改回自己写。' + hint, rules)
                    else:
                        self.assertNotIn(hint, rules)

    def test_unselected_legacy_team_keeps_previous_text(self):
        rules = self.render(dispatch_mode=None, team_enabled=True, plan_only=True,
                            team_name='示例战队', team_entry='dispatch-example')
        dispatch = next(line for line in rules.splitlines() if line.startswith('- 【本机派工】'))
        self.assertEqual(dispatch,
                         '- 【本机派工】已接入「示例战队」：一个窗口能做完的自己写；'
                         '大活拆成互不冲突的块，同时并行派给它（见公共协议【派工】），派工走战队入口。'
                         '派工入口命令：dispatch-example（第一次用先看它的帮助，再按帮助派工；放后台运行，见【等不无限】）。')
        self.assertNotIn('窗口只规划审核', rules)

    def test_t3_rules_use_configured_provider_and_parallel_limit(self):
        rules = self.render(dispatch_mode='t3', t3_sub_provider='codex', max_parallel=7)
        self.assertIn('子会话默认用「codex」，与主会话用不同家的模型', rules)
        self.assertNotIn('同一家模型', rules)
        self.assertIn('同时在跑的子会话不超过 7 个', rules)
        self.assertIn('派活单第一句写明先切到最新开发分支，子会话不提交主分支', rules)
        self.assertIn('一家派不出去改派另一家并告诉用户一句；都不行就报用户', rules)
        rules = self.render(dispatch_mode='t3')
        self.assertIn('同一家模型，验收时多看一眼', rules)
        self.assertIn('同时在跑的子会话不超过 4 个', rules)

    def test_invalid_settings_do_not_replace_saved_values(self):
        settings.save(self.store, {'dispatch_mode': 'host'})
        path = settings.path(self.store)
        initial = path.read_bytes()
        for key, value in (
            ('dispatch_mode', 'other'), ('plan_only', 1),
            ('max_parallel', True), ('max_parallel', 0), ('max_parallel', 4.0),
            ('t3_sub_provider', None), ('t3_sub_provider', 'codex\n另一行'),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(Blocked):
                settings.set_value(self.store, key, value)
        self.assertEqual(path.read_bytes(), initial)

    def test_settings_set_syncs_legacy_flag_and_keeps_team_entry(self):
        settings.set_value(self.store, 'team_entry', 'dispatch-example')
        for mode in ('host', 't3', 'solo', None):
            with self.subTest(mode=mode):
                team = settings.set_value(self.store, 'dispatch_mode', 'team')
                self.assertIs(team['team_enabled'], True)
                self.assertEqual(settings.effective_dispatch(team), ('team', True))
                value = settings.set_value(self.store, 'dispatch_mode', mode)
                self.assertEqual(value['dispatch_mode'], mode)
                self.assertIs(value['team_enabled'], mode is None)
                self.assertEqual(value['team_entry'], 'dispatch-example')

    def test_settings_set_legacy_switch_changes_mode_only_when_needed(self):
        settings.set_value(self.store, 'team_entry', 'dispatch-example')
        unselected = settings.set_value(self.store, 'team_enabled', False)
        self.assertIsNone(unselected['dispatch_mode'])
        for mode in ('host', 't3'):
            with self.subTest(mode=mode):
                settings.set_value(self.store, 'dispatch_mode', mode)
                off = settings.set_value(self.store, 'team_enabled', False)
                self.assertEqual((off['dispatch_mode'], off['team_enabled']), (mode, False))
                on = settings.set_value(self.store, 'team_enabled', True)
                self.assertEqual((on['dispatch_mode'], on['team_enabled']), ('team', True))
                self.assertIs(on['plan_only'], False)
                rules = (self.store.root / settings.RULES).read_text(encoding='utf-8')
                self.assertIn('一个窗口能做完的自己写', rules)
                self.assertNotIn('窗口只规划审核', rules)
                off = settings.set_value(self.store, 'team_enabled', False)
                self.assertEqual((off['dispatch_mode'], off['team_enabled'], off['team_entry']),
                                 ('solo', False, 'dispatch-example'))

    def test_wizard_normalizes_numbers_and_plain_chinese_choices(self):
        for number, mode, chinese in (
            (1, 'host', '自带帮手'), (2, 't3', '装 T3，用 T3 子会话（建议）'),
            (3, 'team', '接战队'), (4, 'solo', '都没有'),
        ):
            for choice in (number, str(number), mode, chinese):
                with self.subTest(choice=choice):
                    self.assertEqual(wizard.normalize({'dispatch_mode': choice}),
                                     {'dispatch_mode': mode})
        self.assertEqual(wizard.normalize({'dispatch_mode': ''}), {'dispatch_mode': 'host'})
        self.assertEqual(wizard.normalize({'tools': []}), {'tools': []})

    def test_dispatch_answers_validate_team_entry_and_keep_it_when_switching(self):
        home = self.store.root
        store = Store(home / 'app')
        self.addCleanup(store.db.close)
        with self.assertRaisesRegex(Blocked, 'team_entry'):
            wizard.run_wizard(store, {'dispatch_mode': '3'}, home=home, scan=False)
        self.assertFalse(settings.path(store).exists())
        result = wizard.run_wizard(store, {'dispatch_mode': '3', 'team_entry': 'dispatch-example'},
                                   home=home, scan=False)
        self.assertEqual((result['派工方式'], result['窗口只规划审核']), ('接战队', '开'))
        selected = settings.load(store)
        self.assertIs(selected['team_enabled'], True)
        self.assertIsNone(selected['plan_only'])
        wizard.run_wizard(store, {'dispatch_mode': '2'}, home=home, scan=False)
        value = settings.load(store)
        self.assertEqual((value['dispatch_mode'], value['team_enabled'], value['team_entry']),
                         ('t3', False, 'dispatch-example'))
        self.assertNotIn('dispatch-example', (store.root / settings.RULES).read_text(encoding='utf-8'))

    def test_legacy_wizard_answers_keep_working_with_selected_modes(self):
        home = self.store.root
        store = Store(home / 'app')
        self.addCleanup(store.db.close)
        for answers, expected in (
            ({'dispatch_mode': '2', 'plan_only': True}, ('t3', False, True)),
            ({'team_enabled': '没有'}, ('t3', False, True)),
            ({'team_enabled': '有'}, ('team', True, False)),
            ({'team_enabled': '没有'}, ('solo', False, False)),
            ({'dispatch_mode': '1', 'team_enabled': '有'}, ('host', False, False)),
        ):
            with self.subTest(answers=answers):
                wizard.run_wizard(store, answers, home=home, scan=False)
                value = settings.load(store)
                self.assertEqual((value['dispatch_mode'], value['team_enabled'], value['plan_only']), expected)

    def test_terminal_asks_team_entry_only_for_team_and_defaults_to_host(self):
        with patch('builtins.input', side_effect=['', '', '', '', '']) as answer:
            normalized = wizard.normalize(wizard.ask())
        self.assertEqual(normalized['dispatch_mode'], 'host')
        self.assertEqual(answer.call_count, 5)
        with patch('builtins.input', side_effect=['', '', '', '接战队', 'dispatch-example', '']):
            normalized = wizard.normalize(wizard.ask())
        self.assertEqual((normalized['dispatch_mode'], normalized['team_entry']), ('team', 'dispatch-example'))
