"""Mac 上的 T3 安装与项目接入引导；不读取或保存登录信息。"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .board import load
from .core import Blocked, Store

RELEASES = 'https://github.com/pingdotgg/t3code/releases'
CLI_INSTALL = 'curl -fsSL https://t3.codes/install.sh | sh'
# 2026-11-16：外部用户看到只列 Codex、Claude，以为必须登录它们才能用；先说明 T3 是可选的
OPTIONAL_NOTE = ('T3 是可选的聊天窗口，它只能接 Codex 或 Claude，所以这里只查这两个。'
                 '不装 T3、不登录它们也没关系：你现在用的、能读写本机文件的 AI 已经能用随身开发了。')
LOGIN_HINT = ('登录由你本人完成，任选一个：Codex 输入 codex login，按提示用你的 ChatGPT 账号登录；'
              'Claude 输入 claude auth login，按提示用你的 Claude 账号登录。')


class T3Setup:
    def __init__(self, *, runner=None, which=None, home=None, applications=None,
                 store=None, root=None, sleep=None, monotonic=None):
        self.runner = runner if runner is not None else subprocess.run
        self.which = which if which is not None else shutil.which
        self.home = Path(home) if home is not None else Path.home()
        self.store = store
        self.root = root
        self.sleep = sleep if sleep is not None else time.sleep
        self.monotonic = monotonic if monotonic is not None else time.monotonic
        self.applications = (Path(applications) if applications is not None else Path('/Applications'),
                             self.home / 'Applications')

    def _command(self, name):
        try:
            return self.which(name)
        except OSError:
            return None

    def _call(self, command, timeout=10):
        # 不继承终端输入，也不收集输出：登录状态及 brew 输出可能含账号信息。
        try:
            return self.runner(command, timeout=timeout, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return None

    def _app(self):
        for folder in self.applications:
            try:
                for app in sorted(folder.glob('T3 Code*.app')):
                    if app.is_dir():
                        return str(app)
            except OSError:
                continue
        return None

    def _tool(self, name, arguments):
        command = self._command(name)
        if not command:
            return {'已安装': False, '登录状态': '没装'}
        status = self._call([command, *arguments])
        return {'已安装': True, '登录状态': '已登录' if status is True else '未登录' if status is False else '待确认'}

    def check(self):
        app = self._app()
        codex = self._tool('codex', ['login', 'status'])
        claude = self._tool('claude', ['auth', 'status'])
        ready = bool(app and any(t['登录状态'] == '已登录' for t in (codex, claude)))
        if not app:
            next_step = '还没装 T3（可选）：想用 T3 就说「帮我装 T3」，不想用就不用管'
        elif ready:
            next_step = '都好了：说「把随身开发接进 T3」'
        elif codex['已安装']:
            next_step = 'T3 装好了，还没登录 Codex（或状态待确认）：在终端里输入 codex login，按提示用你的 ChatGPT 账号登录'
        elif claude['已安装']:
            next_step = 'T3 装好了，还没登录 Claude（或状态待确认）：在终端里输入 claude auth login，按提示用你的 Claude 账号登录'
        else:
            next_step = 'T3 装好了：请先自行安装 Codex 或 Claude Code，再按登录提示由你本人登录一个工具'
        return {'先说明': OPTIONAL_NOTE, 'T3 应用': {'已安装': bool(app), '路径': app},
                'Homebrew 工具': {'已安装': bool(self._command('brew'))},
                'Codex 工具': codex, 'Claude 工具': claude, '都好了': ready,
                '下一步': next_step, '登录提示': LOGIN_HINT}

    def install(self, result):
        if result['T3 应用']['已安装']:
            return result
        brew = self._command('brew')
        if brew:
            installed = self._call([brew, 'install', '--cask', 't3-code'], timeout=15 * 60)
            result = self.check()
            result['安装说明'] = ('已完成安装检查' if installed and result['T3 应用']['已安装'] else
                              '安装未完成或待确认：请检查网络和应用程序文件夹，再说「帮我装 T3」')
        else:
            opened = self._call(['open', RELEASES])
            result = self.check()
            result['安装说明'] = ('已打开下载页。' if opened else '下载页未能自动打开，请手动访问 ' + RELEASES + '。') + '下载 .dmg，双击，把 T3 拖进应用程序'
            if not result['T3 应用']['已安装']:
                result['下一步'] = result['安装说明'] + '；装好后再说「检查 T3」，然后由你本人登录 AI 工具'
        return result

    def _cli(self):
        command = self._command('t3')
        if command:
            return command
        local = self.home / '.t3/bin/t3'
        return self._command(str(local))

    def _registry(self):
        if self.store is not None:
            return self.store.root.resolve(), load(self.store)
        if self.root is None:
            raise Blocked('接进 T3 需要随身开发根目录（--root）')
        store = Store(self.root)
        try:
            return store.root.resolve(), load(store)
        finally:
            store.db.close()

    def add_project(self, path, title):
        """搬家后把这一个项目加进 T3（只在 T3 和命令行都在时；不装、不启动 T3）。返回 True/False/None（没装）。"""
        app, cli = self._app(), self._cli()
        if not app or not cli:
            return None
        return self._call([cli, 'project', 'add', str(path), '--title', title], timeout=10) is True

    def connect(self, *, allow_cli_install=False):
        result = {'已加进 T3': [], '没加上': [], '已跳过': []}
        app = self._app()
        if not app:
            result['下一步'] = '还没装 T3：说「帮我装 T3」'
            return result
        cli = self._cli()
        if not cli and allow_cli_install:
            installed = self._call(['bash', '-o', 'pipefail', '-c', CLI_INSTALL], timeout=300)
            cli = self._cli()
            result['命令行安装说明'] = ('已完成安装检查' if installed and cli else
                                     '安装未完成或待确认，请检查网络后重试')
        if not cli:
            result['下一步'] = '还缺 T3 命令行，说「同意装 T3 命令行」'
            return result
        root, entries = self._registry()
        projects = [{'名称': '开发总管', '路径': str(root)}]
        for entry in entries:
            path = Path(entry['path']).expanduser().resolve()
            if path == root:
                continue
            project = {'名称': entry['name'], '路径': str(path)}
            if not path.is_dir():
                result['已跳过'].append({**project, '原因': '项目目录不存在或不是文件夹'})
                continue
            projects.append(project)
        if not self._call(['open', '-a', app]):
            result['没加上'] = [{**p, '原因': '未能启动 T3 桌面版'} for p in projects]
            result['下一步'] = '请在应用程序文件夹里打开 T3，再说「把随身开发接进 T3」'
            return result
        # 桌面版启动后可能暂时不响应 CLI；根目录最多等 30 秒，每 2 秒重试。
        deadline = self.monotonic() + 30
        for index, project in enumerate(projects):
            command = [cli, 'project', 'add', project['路径'], '--title', project['名称']]
            added = self._call(command, timeout=10)
            while index == 0 and added is not True:
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    break
                self.sleep(min(2, remaining))
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    break
                added = self._call(command, timeout=min(10, remaining))
            if added is True:
                result['已加进 T3'].append(project)
            else:
                # 非零也可能表示已经加过，不猜原因、不当作整体失败；可安全重试。
                result['没加上'].append({**project, '原因': '原因待确认（可能已添加，可再次运行）'})
        opened = self._call([cli, 'app', str(root)])
        result['打开说明'] = ('已打开开发总管' if opened else
                             '未能自动打开开发总管，请在 T3 左边查看添加结果')
        result['下一步'] = '在 T3 左边点「开发总管」，直接说话就能开发'
        return result

    @staticmethod
    def _confirm(ask, prompt):
        try:
            answer = (ask if ask is not None else input)(prompt)
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in ('', 'y', 'yes', '是', '好')

    def setup(self, *, install=False, open_app=False, connect=False, allow_cli_install=False,
              if_needed=False, stdin=None, ask=None):
        stdin = sys.stdin if stdin is None else stdin
        if if_needed and not stdin.isatty():
            return None
        if connect:
            return self.connect(allow_cli_install=allow_cli_install)
        result = self.check()
        if if_needed:
            if not result['T3 应用']['已安装']:
                if self._confirm(ask, '要现在下载安装 T3 吗？（回车=是 / n=以后）'):
                    result = self.install(result)
            if result['都好了'] and self._confirm(ask, '要把随身开发和你的项目加进 T3 吗？（回车=是 / n=以后）'):
                result = self.connect()
        elif install:
            result = self.install(result)
        elif open_app:
            app = result['T3 应用']['路径']
            if app:
                result['打开说明'] = '已打开 T3' if self._call(['open', '-a', app]) else '未能打开 T3，请在应用程序文件夹里双击它'
        return result


def run(args):
    return T3Setup(root=args.root).setup(install=args.install, open_app=args.open_app,
                                        connect=args.connect, allow_cli_install=args.allow_cli_install,
                                        if_needed=args.if_needed)


def register(subparser):
    p = subparser.add_parser('t3-setup', help='装 T3：先检查，同意后 --install；--connect 接进项目；登录由本人完成')
    modes = p.add_mutually_exclusive_group()
    modes.add_argument('--install', action='store_true')
    modes.add_argument('--open', dest='open_app', action='store_true')
    modes.add_argument('--if-needed', action='store_true')
    modes.add_argument('--connect', action='store_true')
    p.add_argument('--allow-cli-install', action='store_true', help='仅与 --connect 一起使用，表示同意运行官方远程安装脚本')
    p.set_defaults(t3_setup_run=run)
    return p
