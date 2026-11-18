"""T3 外部能力全部模拟；App、家目录、发行包均为临时夹具。"""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from portable import t3_setup
from portable.__main__ import main
from portable.distribution import build, release_verify
from portable.core import Store

SOURCE = Path(__file__).resolve().parents[1]


class T3SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home'
        self.apps = self.root / 'Applications'
        self.commands = {}
        self.statuses = {}
        self.calls = []
        self.after_run = None
        self.clock = 0
        self.setup = t3_setup.T3Setup(runner=self.runner, which=self.commands.get,
                                      home=self.home, applications=self.apps,
                                      sleep=self.sleep, monotonic=lambda: self.clock)

    def sleep(self, seconds):
        self.clock += seconds

    def registry(self, entries=()):
        store = Store(self.root / '随身开发')
        self.addCleanup(store.db.close)
        store.put('registry', list(entries))
        self.setup.store = store
        return str(store.root.resolve())

    def project(self, name):
        path = self.root / name
        path.mkdir()
        return {'name': name, 'path': str(path)}

    def runner(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.after_run:
            self.after_run(command)
        status = self.statuses.get(tuple(command), 0)
        if isinstance(status, Exception):
            raise status
        return subprocess.CompletedProcess(command, status)

    def app(self, name='T3 Code (Beta).app', user=False):
        app = (self.home / 'Applications' if user else self.apps) / name
        app.mkdir(parents=True)
        return str(app)

    def tty(self, answer=''):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        return Tty(), lambda prompt: answer

    def test_empty_machine_suggests_install_and_runs_nothing(self):
        result = self.setup.setup()
        self.assertIn('帮我装 T3', result['下一步'])
        self.assertIn('可选', result['下一步'])
        self.assertIn('已经能用随身开发', result['先说明'])
        self.assertEqual(next(iter(result)), '先说明')
        self.assertFalse(result['都好了'])
        self.assertIsNone(result['T3 应用']['路径'])
        self.assertEqual(result['Codex 工具']['登录状态'], '没装')
        self.assertFalse(result['Homebrew 工具']['已安装'])
        self.assertEqual(self.calls, [])

    def test_installed_app_without_codex_login_suggests_personal_login(self):
        app = self.app()
        self.commands['codex'] = 'codex'
        self.statuses[('codex', 'login', 'status')] = 1
        result = self.setup.setup()
        self.assertEqual(result['T3 应用']['路径'], app)
        self.assertIn('codex login', result['下一步'])
        self.assertIn('ChatGPT', result['下一步'])
        self.assertFalse(result['都好了'])
        self.assertEqual(self.calls[0][0], ['codex', 'login', 'status'])

    def test_brew_install_rechecks_new_app_and_never_logs_output(self):
        self.commands['brew'] = 'brew'
        self.commands['codex'] = 'codex'
        def installed(command):
            if command == ['brew', 'install', '--cask', 't3-code']:
                self.app()
        self.after_run = installed
        result = self.setup.setup(install=True)
        command, options = next(c for c in self.calls if c[0][0] == 'brew')
        self.assertEqual(command, ['brew', 'install', '--cask', 't3-code'])
        self.assertEqual(options['timeout'], 900)
        for _, options in self.calls:
            self.assertEqual(options['stdout'], subprocess.DEVNULL)
            self.assertEqual(options['stderr'], subprocess.DEVNULL)
            self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertTrue(result['T3 应用']['已安装'])
        self.assertTrue(result['都好了'])
        self.assertEqual(sum(c[0] == ['codex', 'login', 'status'] for c in self.calls), 2)

    def test_without_brew_opens_official_releases_and_explains_dmg(self):
        result = self.setup.setup(install=True)
        self.assertEqual(self.calls[0][0], ['open', t3_setup.RELEASES])
        self.assertIn('下载 .dmg，双击，把 T3 拖进应用程序', result['下一步'])
        self.assertFalse(result['都好了'])

    def test_missing_commands_and_timeout_are_pending_without_exception_details(self):
        self.commands.update(codex='codex', claude='claude')
        self.statuses[('codex', 'login', 'status')] = FileNotFoundError('private account')
        self.statuses[('claude', 'auth', 'status')] = subprocess.TimeoutExpired('private account', 10)
        result = self.setup.setup()
        self.assertEqual(result['Codex 工具']['登录状态'], '待确认')
        self.assertEqual(result['Claude 工具']['登录状态'], '待确认')
        self.assertNotIn('private account', json.dumps(result))
        self.assertTrue(all(options['timeout'] == 10 for _, options in self.calls))

    def test_noninteractive_if_needed_does_not_probe_install_or_prompt(self):
        self.commands['brew'] = 'brew'
        with patch.object(self.setup, 'check', side_effect=AssertionError('must be silent')):
            result = self.setup.setup(if_needed=True, stdin=io.StringIO(),
                                      ask=lambda prompt: self.fail('unexpected prompt'))
        self.assertIsNone(result)
        self.assertEqual(self.calls, [])

    def test_tty_accepts_empty_answer_installs_and_includes_login_hint(self):
        self.commands['brew'] = 'brew'
        stdin, _ = self.tty()
        prompts = []
        def ask(prompt):
            prompts.append(prompt)
            return ''
        result = self.setup.setup(if_needed=True, stdin=stdin, ask=ask)
        self.assertEqual(prompts, ['要现在下载安装 T3 吗？（回车=是 / n=以后）'])
        self.assertEqual(self.calls[0][0], ['brew', 'install', '--cask', 't3-code'])
        self.assertIn('本人', result['登录提示'])

    def test_tty_decline_or_eof_never_installs(self):
        for answer in ('n', 'no', '不', 'unknown', None):
            stdin, ask = self.tty(answer)
            if answer is None:
                def ask(prompt):
                    raise EOFError()
            self.setup.setup(if_needed=True, stdin=stdin, ask=ask)
        self.assertEqual(self.calls, [])

    def test_existing_app_never_reinstalls_and_only_open_mode_opens_it(self):
        app = self.app('T3 Code (Alpha).app', user=True)
        self.commands['brew'] = 'brew'
        self.setup.setup(install=True)
        self.assertEqual(self.calls, [])
        result = self.setup.setup(open_app=True)
        self.assertEqual(self.calls[0][0], ['open', '-a', app])
        self.assertEqual(result['打开说明'], '已打开 T3')

    def test_open_missing_app_does_not_launch_anything(self):
        self.assertIn('帮我装 T3', self.setup.setup(open_app=True)['下一步'])
        self.assertEqual(self.calls, [])

    def test_app_variants_and_either_logged_in_tool_are_ready(self):
        for name, user, tool in [('T3 Code.app', False, 'codex'),
                                 ('T3 Code (Beta).app', True, 'claude')]:
            with self.subTest(name=name):
                self.app(name, user)
                self.commands.clear()
                self.commands[tool] = tool
                result = self.setup.setup()
                self.assertTrue(result['都好了'])
                self.assertIn('把随身开发接进 T3', result['下一步'])
                stdin, _ = self.tty()
                prompts = []
                self.setup.setup(if_needed=True, stdin=stdin,
                                 ask=lambda p: prompts.append(p) or 'n')
                self.assertEqual(prompts, ['要把随身开发和你的项目加进 T3 吗？（回车=是 / n=以后）'])
                self.assertFalse(any(c[0][0] == 'open' for c in self.calls))

    def test_tty_existing_app_only_gives_login_hint_without_prompt(self):
        self.app()
        stdin, _ = self.tty()
        result = self.setup.setup(if_needed=True, stdin=stdin,
                                  ask=lambda p: self.fail('already installed'))
        self.assertIn('claude auth login', result['登录提示'])
        self.assertFalse(result['都好了'])

    def test_install_failure_is_sanitized_and_rechecked(self):
        self.commands['brew'] = 'brew'
        self.statuses[('brew', 'install', '--cask', 't3-code')] = subprocess.TimeoutExpired('private account', 900)
        result = self.setup.setup(install=True)
        self.assertFalse(result['T3 应用']['已安装'])
        self.assertIn('待确认', result['安装说明'])
        self.assertNotIn('private account', json.dumps(result))

    def test_failed_open_is_explained_without_raw_error(self):
        self.statuses[('open', t3_setup.RELEASES)] = FileNotFoundError('private account')
        self.assertIn(t3_setup.RELEASES, self.setup.setup(install=True)['安装说明'])

    def test_cli_json_is_registered_and_does_not_create_runtime_data(self):
        output = io.StringIO()
        with patch('portable.t3_setup.T3Setup', return_value=self.setup), contextlib.redirect_stdout(output):
            self.assertEqual(main(['--root', str(self.root / 'runtime'), 't3-setup']), 0)
        self.assertIn('帮我装 T3', json.loads(output.getvalue())['下一步'])
        self.assertFalse((self.root / 'runtime').exists())

    def test_connect_opens_app_adds_root_and_projects_then_opens_root(self):
        app = self.app()
        entries = [self.project('一个项目'), self.project('另一个项目')]
        root = self.registry(entries)
        self.commands['t3'] = '/fake/t3'
        result = self.setup.setup(connect=True)
        self.assertEqual([c[0] for c in self.calls], [
            ['open', '-a', app],
            ['/fake/t3', 'project', 'add', root, '--title', '开发总管'],
            *[['/fake/t3', 'project', 'add', e['path'], '--title', e['name']] for e in entries],
            ['/fake/t3', 'app', root],
        ])
        self.assertEqual([p['名称'] for p in result['已加进 T3']], ['开发总管', '一个项目', '另一个项目'])
        self.assertEqual(result['没加上'], [])
        self.assertIn('直接说话就能开发', result['下一步'])

    def test_connect_requires_app_even_when_cli_install_is_allowed(self):
        result = self.setup.setup(connect=True, allow_cli_install=True)
        self.assertIn('帮我装 T3', result['下一步'])
        self.assertEqual(self.calls, [])

    def test_missing_cli_does_not_install_or_create_runtime(self):
        self.app()
        self.setup.root = self.root / 'runtime'
        result = self.setup.setup(connect=True)
        self.assertIn('同意装 T3 命令行', result['下一步'])
        self.assertEqual(self.calls, [])
        self.assertFalse(Path(self.setup.root).exists())

    def test_explicit_permission_installs_cli_with_timeout_and_rechecks(self):
        self.app()
        root = self.registry()
        local = str(self.home / '.t3/bin/t3')
        install = ['bash', '-o', 'pipefail', '-c', t3_setup.CLI_INSTALL]
        def installed(command):
            if command == install:
                self.commands[local] = local
        self.after_run = installed
        result = self.setup.setup(connect=True, allow_cli_install=True)
        self.assertEqual(self.calls[0][0], install)
        self.assertEqual(self.calls[0][1]['timeout'], 300)
        self.assertEqual(self.calls[-1][0], [local, 'app', root])
        self.assertIn('已完成', result['命令行安装说明'])
        for _, options in self.calls:
            for stream in ('stdin', 'stdout', 'stderr'):
                self.assertEqual(options[stream], subprocess.DEVNULL)

    def test_cli_install_failure_does_not_attempt_connection_or_expose_errors(self):
        self.app()
        install = ('bash', '-o', 'pipefail', '-c', t3_setup.CLI_INSTALL)
        self.statuses[install] = subprocess.TimeoutExpired('private account', 300)
        result = self.setup.setup(connect=True, allow_cli_install=True)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('待确认', result['命令行安装说明'])
        self.assertIn('同意装 T3 命令行', result['下一步'])
        self.assertNotIn('private account', json.dumps(result))

    def test_cli_on_path_takes_precedence_and_never_installs_again(self):
        self.app()
        self.registry()
        self.commands.update(t3='/fake/path-t3')
        self.commands[str(self.home / '.t3/bin/t3')] = '/fake/local-t3'
        self.setup.setup(connect=True, allow_cli_install=True)
        self.assertEqual(self.calls[1][0][0], '/fake/path-t3')
        self.assertFalse(any('curl' in ' '.join(c[0]) for c in self.calls))

    def test_executable_cli_in_home_is_found_without_path_entry(self):
        self.app()
        root = self.registry()
        local = self.home / '.t3/bin/t3'
        local.parent.mkdir(parents=True)
        local.write_text('#!/bin/sh\nexit 0\n')
        local.chmod(0o755)
        # 使用真实文件可执行性检查，但所有命令仍交给假 runner。
        self.setup.which = lambda name: t3_setup.shutil.which(name, path=str(self.root / 'empty-path'))
        result = self.setup.setup(connect=True)
        self.assertEqual(self.calls[-1][0], [str(local), 'app', root])
        self.assertEqual(len(result['已加进 T3']), 1)

    def test_connect_skips_missing_directory_and_root_alias(self):
        self.app()
        root = self.registry()
        alias = self.root / 'root-alias'
        alias.symlink_to(root, target_is_directory=True)
        self.setup.store.put('registry', [
            {'name': '根目录', 'path': root}, {'name': '别名', 'path': str(alias)},
            {'name': '失效项目', 'path': str(self.root / '不存在')},
        ])
        self.commands['t3'] = 't3'
        result = self.setup.setup(connect=True)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual([p['名称'] for p in result['已加进 T3']], ['开发总管'])
        self.assertEqual([p['名称'] for p in result['已跳过']], ['失效项目'])

    def test_failed_add_is_pending_and_does_not_block_later_projects_or_open(self):
        self.app()
        bad, good = self.project('失败项目'), self.project('成功项目')
        root = self.registry([bad, good])
        self.commands['t3'] = 't3'
        self.statuses[('t3', 'project', 'add', bad['path'], '--title', bad['name'])] = 1
        result = self.setup.setup(connect=True)
        self.assertEqual([p['名称'] for p in result['没加上']], ['失败项目'])
        self.assertIn('待确认', result['没加上'][0]['原因'])
        self.assertEqual([p['名称'] for p in result['已加进 T3']], ['开发总管', '成功项目'])
        self.assertEqual(self.calls[-1][0], ['t3', 'app', root])

    def test_startup_retries_every_two_seconds_until_root_add_succeeds(self):
        self.app()
        root = self.registry()
        self.commands['t3'] = 't3'
        command = ('t3', 'project', 'add', root, '--title', '开发总管')
        self.statuses[command] = 1
        attempts = []
        def ready(c):
            if tuple(c) == command:
                attempts.append(self.clock)
                if len(attempts) == 3:
                    self.statuses[command] = 0
        self.after_run = ready
        result = self.setup.setup(connect=True)
        self.assertEqual(attempts, [0, 2, 4])
        self.assertEqual(len(result['已加进 T3']), 1)

    def test_startup_gives_up_after_thirty_seconds_but_still_opens_root(self):
        self.app()
        root = self.registry()
        self.commands['t3'] = 't3'
        self.statuses[('t3', 'project', 'add', root, '--title', '开发总管')] = 1
        result = self.setup.setup(connect=True)
        self.assertEqual(self.clock, 30)
        self.assertEqual([p['名称'] for p in result['没加上']], ['开发总管'])
        self.assertEqual(self.calls[-1][0], ['t3', 'app', root])

    def test_startup_command_timeouts_count_toward_thirty_second_limit(self):
        self.app()
        root = self.registry()
        self.commands['t3'] = 't3'
        command = ('t3', 'project', 'add', root, '--title', '开发总管')
        self.statuses[command] = subprocess.TimeoutExpired('private account', 10)
        def elapsed(c):
            if tuple(c) == command:
                self.clock += self.calls[-1][1]['timeout']
        self.after_run = elapsed
        result = self.setup.setup(connect=True)
        self.assertEqual(self.clock, 30)
        self.assertEqual([options['timeout'] for c, options in self.calls if tuple(c) == command], [10, 10, 6])
        self.assertNotIn('private account', json.dumps(result))

    def test_failed_project_open_does_not_claim_open_success(self):
        self.app()
        root = self.registry()
        self.commands['t3'] = 't3'
        self.statuses[('t3', 'app', root)] = 1
        result = self.setup.setup(connect=True)
        self.assertEqual(len(result['已加进 T3']), 1)
        self.assertIn('未能自动打开', result['打开说明'])

    def test_open_failure_stops_adds_and_reports_pending(self):
        app = self.app()
        self.registry()
        self.commands['t3'] = 't3'
        self.statuses[('open', '-a', app)] = 1
        result = self.setup.setup(connect=True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(result['没加上']), 1)
        self.assertIn('未能启动', result['没加上'][0]['原因'])

    def test_if_needed_connects_after_install_and_login_confirmation(self):
        self.commands.update(brew='brew', codex='codex', t3='t3')
        root = self.registry()
        def installed(command):
            if command == ['brew', 'install', '--cask', 't3-code']:
                self.app()
        self.after_run = installed
        stdin, _ = self.tty()
        prompts = []
        result = self.setup.setup(if_needed=True, stdin=stdin,
                                  ask=lambda p: prompts.append(p) or '')
        self.assertEqual(prompts, ['要现在下载安装 T3 吗？（回车=是 / n=以后）',
                                   '要把随身开发和你的项目加进 T3 吗？（回车=是 / n=以后）'])
        self.assertEqual(self.calls[-1][0], ['t3', 'app', root])
        self.assertEqual(len(result['已加进 T3']), 1)

    def test_if_needed_with_ready_app_and_missing_cli_only_prompts_for_connect(self):
        self.app()
        self.commands['claude'] = 'claude'
        stdin, _ = self.tty()
        prompts = []
        result = self.setup.setup(if_needed=True, stdin=stdin,
                                  ask=lambda p: prompts.append(p) or '')
        self.assertEqual(len(prompts), 1)
        self.assertIn('同意装 T3 命令行', result['下一步'])
        self.assertEqual([c[0] for c in self.calls], [['claude', 'auth', 'status']])

    def test_cli_connect_passes_root_and_loads_registered_projects(self):
        self.app()
        root = self.registry([self.project('已登记')])
        self.setup.root = root
        self.setup.store = None
        self.commands['t3'] = 't3'
        output = io.StringIO()
        with patch('portable.t3_setup.T3Setup', return_value=self.setup) as constructor, contextlib.redirect_stdout(output):
            self.assertEqual(main(['--root', root, 't3-setup', '--connect']), 0)
        constructor.assert_called_once_with(root=root)
        self.assertEqual([p['名称'] for p in json.loads(output.getvalue())['已加进 T3']], ['开发总管', '已登记'])


class T3DistributionTest(unittest.TestCase):
    def test_packaged_entry_and_silent_noninteractive_zipapp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve() / 'package'
            build(SOURCE, target, edition='friend')
            for name in ('AGENTS.md', 'CLAUDE.md'):
                instructions = (target / name).read_text()
                self.assertIn('### 装好后第一件事', instructions)
                self.assertIn('t3-setup --connect', instructions)
                self.assertIn('wizard', instructions)
                self.assertIn('本人登录', instructions)
            entry = (target / '开始使用.command').read_text()
            self.assertIn('t3-setup --if-needed || true', entry)
            self.assertLess(entry.index('wizard --if-needed'), entry.index('t3-setup --if-needed'))
            archive = target / '工具/portable.pyz'
            with zipfile.ZipFile(archive) as z:
                self.assertIn('portable/t3_setup.py', z.namelist())
            # 非交互路径不探测真实工具、不启动浏览器，也不安装任何东西。
            out = subprocess.run([sys.executable, '-B', str(archive), '--root', str(target),
                                  't3-setup', '--if-needed'], input='', capture_output=True, text=True)
            self.assertEqual((out.returncode, out.stdout, out.stderr), (0, '', ''))
            self.assertEqual(list((target / '数据').iterdir()), [])
            manifest = json.loads((target / '发行.json').read_text())
            manifest['version'] = '0.2.29'
            (target / '发行.json').write_text(json.dumps(manifest))
            self.assertEqual(release_verify(target)['version'], '0.2.29')

    def test_protocol_helper_rule_and_version(self):
        protocol = json.loads((SOURCE / 'portable/protocol.json').read_text())
        self.assertGreaterEqual(tuple(map(int, protocol['version'].split('.'))), (0, 2, 30))
        self.assertTrue(any(rule.startswith('【帮手命令】') and 't3-setup' in rule
                            for rule in protocol['public_rules']))


if __name__ == '__main__':
    unittest.main()
