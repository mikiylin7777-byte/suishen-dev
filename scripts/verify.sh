#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONDONTWRITEBYTECODE=1
# 真实 MySQL 测试：设置 PORTABLE_MYSQL_BIN 指向 mysqld 所在目录才跑，否则跳过
python3 -m unittest discover -s tests -v
if [ -d adapters/bots/node_modules ] || (cd adapters/bots && npm ci --no-audit --no-fund --silent); then
  node --test adapters/bots/test.mjs
  node --test adapters/bots/sdk-smoke.mjs
fi
python3 tools/ci/privacy_check.py
