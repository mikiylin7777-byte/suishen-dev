#!/bin/bash
# 准备测试所需的本地环境（.build 已被 git 忽略）：
#   .build/integration-wheelhouse  按 scripts/mcp-sdk.lock.txt 下载的 MCP SDK 离线安装包
#   .build/integration-sdk         用上述安装包离线装好的 MCP SDK 解释器，只给 verify_capabilities.py 测共享上下文接口用
#   adapters/bots/node_modules     机器人官方 SDK 冒烟测试要的依赖，按锁文件安装
# 自带战队后台已移除（见 DECISIONS 2026-11-18），不再下载它的依赖。
# 已存在则跳过；只在缺失时联网下载公开安装包，不读写任何凭据。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK="$ROOT/scripts/mcp-sdk.lock.txt"
WHEELS="$ROOT/.build/integration-wheelhouse"
SDK="$ROOT/.build/integration-sdk"
PY="${PORTABLE_TEST_PYTHON:-python3}"
mkdir -p "$ROOT/.build"
if [ ! -d "$WHEELS" ] || [ -z "$(ls -A "$WHEELS" 2>/dev/null)" ]; then
  tmp="$WHEELS.tmp.$$"; rm -rf "$tmp"
  "$PY" -m pip download --disable-pip-version-check --only-binary=:all: -q -d "$tmp" -r "$LOCK"
  mv "$tmp" "$WHEELS"
fi
if [ ! -x "$SDK/bin/python" ] || ! "$SDK/bin/python" -c 'import mcp' 2>/dev/null; then
  rm -rf "$SDK"
  "$PY" -m venv "$SDK"
  "$SDK/bin/python" -m pip install --isolated --disable-pip-version-check -q --no-index --no-cache-dir --find-links "$WHEELS" -r "$LOCK" || { rm -rf "$SDK"; exit 1; }
fi
"$SDK/bin/python" -c 'import mcp'
if [ ! -d "$ROOT/adapters/bots/node_modules" ]; then
  (cd "$ROOT/adapters/bots" && npm ci --no-audit --no-fund --silent)
fi
echo "测试环境就绪：$SDK"
