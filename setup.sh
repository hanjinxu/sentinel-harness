#!/usr/bin/env bash
# 安装依赖并做一次离线自检。国内网络建议走清华 PyPI 镜像。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# 允许通过 PY 指定已有解释器；否则在项目内创建 .venv，保证环境隔离且可复现。
if [ -z "${PY:-}" ]; then
  python3 -m venv .venv
  PY="$ROOT/.venv/bin/python"
fi

"$PY" -m pip install -r requirement.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
"$PY" main.py requirements
"$PY" main.py demo
echo "依赖已装到 ${PY}；也可执行 source .venv/bin/activate 后直接使用 python main.py ..."
