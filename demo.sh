#!/usr/bin/env bash
# 一键演示：完整链路 + 确定性回放 + PR 门禁
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

echo "[1/3] 完整舆情监控链路"
"$PY" main.py demo
echo
echo "[2/3] 确定性轨迹回放"
"$PY" main.py replay
echo
echo "[3/3] PR 冒烟 + CI 门禁"
"$PY" main.py eval --suite pr_smoke
