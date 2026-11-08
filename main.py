#!/usr/bin/env python3
"""Sentinel Harness 统一入口。

所有命令都从项目根目录执行：
    python main.py demo
    python main.py eval --suite nightly
    python main.py requirements
    python main.py --help

入口只负责把 src/ 加入模块搜索路径，业务逻辑全部在 src/sentinel/cli.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sentinel.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["--help"]))
