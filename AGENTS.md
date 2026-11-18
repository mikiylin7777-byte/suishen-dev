# AGENTS.md
先读 [CONTRIBUTING.md](CONTRIBUTING.md)；公共规矩单源在 `portable/protocol.json`。

## 怎么跑、怎么测
- 全部 Python 测试：`python3 -m unittest discover -s tests`
- 机器人适配测试：`(cd adapters/bots && npm ci) && node --test adapters/bots/test.mjs`
- 个人信息检查：`python3 tools/ci/privacy_check.py`
- 从源码打一份完整版：`python3 scripts/install.py <新目录>/随身开发`；朋友版：`python3 scripts/package_friend.py <输出.zip>`

## 约定
- 只用 Python 标准库；新增文件要进 `portable/protocol.json` 的 `distribution.runtime_files`。
- 代码注释不写真实人名、路径、内网地址；测试用示例数据。
- 不为过检查删测试或放宽检查。
