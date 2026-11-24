#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONDONTWRITEBYTECODE=1
# 本机全量测试守门（主干同步、本机同一时间只跑一个全量、挂了只重跑失败与受影响项、一批最多两次全量）
if [ "${1:-}" != "--raw" ] && [ -z "${PORTABLE_FULL_LOCK_HELD:-}" ] && [ -z "${CI:-}" ] && [ -z "${GITHUB_ACTIONS:-}" ]; then
  exec python3 -c "import sys;sys.path.insert(0,'.');from portable.core import Blocked;from portable.test_gate import verify_entry
try:
  rc,plan=verify_entry('.')
  sys.exit(rc)
except Blocked as e:
  print('测试守门拦下：'+str(e),file=sys.stderr)
  sys.exit(2)"
fi
if [ "${1:-}" = "--raw" ]; then shift; fi
resume_stage=
if [ "${1:-}" = "--resume-from" ]; then
  resume_stage="$2"
  case "$resume_stage" in
    python|bots|bot-sdk|capabilities) ;;
    *) printf 'Unknown verification stage: %s\n' "$resume_stage" >&2; exit 2 ;;
  esac
  shift 2
fi
if [ "$#" -gt 0 ] && [ "$resume_stage" != "python" ]; then
  printf 'Python test targets require --resume-from python\n' >&2
  exit 2
fi

run_stage() {
  local stage="$1"
  shift
  if [ "$stage" != setup ]; then
    if [ -n "$resume_stage" ] && [ "$resume_stage" != "$stage" ]; then return 0; fi
    resume_stage=
  fi
  if [ -n "${PORTABLE_VERIFY_STAGE_FILE:-}" ]; then
    printf '%s\n' "$stage" > "$PORTABLE_VERIFY_STAGE_FILE"
  fi
  "$@"
}

# Use only explicit isolated DB tools; tests create their own engines, never production.
if [ -d /opt/homebrew/opt/postgresql@17/bin ]; then export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"; fi
# 真实 MySQL 测试：设置 PORTABLE_MYSQL_BIN 指向 mysqld 所在目录才跑，否则跳过
run_stage setup bash scripts/prepare_test_env.sh
if [ "$#" -gt 0 ]; then
  export PYTHONPATH="$ROOT/tests${PYTHONPATH:+:$PYTHONPATH}"
  run_stage python python3 -m unittest -v "$@"
else
  run_stage python python3 -m unittest discover -s tests -v
fi
run_stage bots node --test adapters/bots/test.mjs
# Official optional bot dependencies must be installed from the checked-in lockfile for this check.
run_stage bot-sdk node --test adapters/bots/sdk-smoke.mjs

if [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ]; then
  run_stage capabilities python3 scripts/verify_capabilities.py --skip-client-checks
else
  run_stage capabilities python3 scripts/verify_capabilities.py
fi
