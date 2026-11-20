#!/bin/bash
# 准备测试环境。Python SDK 缓存默认放在操作系统的缓存目录，可用 PORTABLE_TEST_CACHE_DIR 指定仓外的绝对缓存根。
#   integration-wheelhouse  按 scripts/mcp-sdk.lock.txt 下载的 MCP SDK 离线安装包
#   integration-sdk         用上述安装包离线装好的 MCP SDK 解释器，只给 verify_capabilities.py 测共享上下文接口用
#   adapters/bots/node_modules     机器人官方 SDK 冒烟测试要的依赖，按锁文件安装
# 自带战队后台已移除（见 DECISIONS 2026-11-18），不再下载它的依赖。
# 已存在则跳过；只在缺失时联网下载公开安装包，不读写任何凭据。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PORTABLE_TEST_PYTHON:-python3}"
SDK="$("$PY" -B "$ROOT/scripts/sdk_cache.py" --prepare)"
if [ ! -d "$ROOT/adapters/bots/node_modules" ]; then
  (cd "$ROOT/adapters/bots" && npm ci --no-audit --no-fund --silent)
fi
echo "测试环境就绪：$SDK"
