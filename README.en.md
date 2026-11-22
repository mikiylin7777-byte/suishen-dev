# Suishen Dev (随身开发)

English · [中文](README.md)

**Helping people who can't code build software with AI — simply and happily.**

Suishen Dev is not another AI. It gives the AI you already use (Codex, Claude Code, T3, or any AI chat that can read and write local files) a shared set of **rules, memory and helpers**:

- **One public rulebook**: how the AI starts work, reports progress, and which actions always need your explicit approval (deploying, touching real data, accounts/keys, spending money or sending messages outside).
- **Project memory lives in files**: progress, feature lists and lessons learned are written into each project's own files, so you can switch AIs or computers without losing context.
- **Dashboard**: one page shows every project's stage, who it is waiting on and what is blocked (Obsidian-friendly).
- **Merge gate**: changes merge only when every automated check passes; high-risk changes wait for your approval.
- **Background service**: daily backups, weekly health checks, one-step upgrade and rollback.
- **Helper commands**: screenshots of the result, undo the last change, move existing projects in, continue on another computer, secret-free help bundles — all run by the AI while you talk in plain language.

The user interface and documentation are currently in Chinese.

## Install
Requirements: macOS, Python 3.9+.

1. Download the latest `suishen-dev-<version>.zip` from [Releases](https://github.com/mikiylin7777-byte/suishen-dev/releases) and verify its SHA256.
2. Unzip and move the `随身开发` folder into your home folder.
3. Double-click `开始使用.command` and answer a few setup questions.

## Choosing how to delegate work

Choose one option during setup. Press Enter to use the first option.

1. **Use your AI tool’s built-in helpers (default).** Choose this for tools with subagents, such as Claude Code or Codex.
2. **Use T3 sub-sessions.** Consider connecting a second AI provider in T3 for the implementation work while the main session plans and reviews.
3. **Connect an AI team.** Bring your own team software and tell your AI which command dispatches work.
4. **Work in the current session.** Your AI handles the work itself, one step at a time.

The first three options turn on “Plan and review only” by default. You can turn it off separately. It stays off in the fourth option. If you’re upgrading and haven’t made a choice yet, your AI checks which helpers are available and asks you once. To switch anytime, tell your AI “Change how I delegate work.”

## Upgrade & Rollback
Tell your AI: "把随身开发升级到最新版" (`python3 ~/随身开发/工具/portable.pyz --root ~/随身开发 upgrade apply`), which checks GitHub Releases, verifies the SHA256 digest and upgrades in place. Say "退回上一版" (`upgrade rollback`) to roll back.

## Develop
```bash
python3 -m unittest discover -s tests
```
Standard library only; the Feishu/WeCom bot adapters use their official SDKs (optional).

## Contributing
Issues, suggestions and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Please report security issues privately as described in [SECURITY.md](SECURITY.md).

## License
[MIT](LICENSE) © 2026 Aring
