#!/usr/bin/env bash
# 远程仓库建好之后的发布入口。
# 先在 https://github.com/new 创建 public 空仓库 hanjinxu/sentinel-harness，
# 不要勾选 README / LICENSE / .gitignore 初始化文件，然后执行本脚本。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if ! git ls-remote origin >/dev/null 2>&1; then
  echo "远程仓库尚不存在，或 SSH 未授权。"
  echo "请先创建：https://github.com/new  →  owner=hanjinxu, name=sentinel-harness, public"
  echo "注意：不要勾选任何初始化文件，否则会产生非快进冲突。"
  exit 1
fi

git push -u origin main
echo "已推送：https://github.com/hanjinxu/sentinel-harness"
