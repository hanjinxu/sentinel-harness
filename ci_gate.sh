#!/usr/bin/env bash
# CI 入口：退出码直接作为合并门禁结果。
# 分层策略：PR 跑冒烟；夜间用 `make nightly`；模型/拓扑大改才跑 full。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PY:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
  else
    PY="$(command -v python3 || command -v python)"
  fi
fi
cd "$ROOT"

"$PY" -m unittest discover -s tests
"$PY" main.py eval --suite pr_smoke --noise "$@"
