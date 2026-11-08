PY ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
export PYTHONPATH := src

.PHONY: help requirements demo test ci nightly full replay ab export mcp baseline

help:
	@echo "Sentinel Harness (LangGraph) — make targets"
	@echo "  requirements  打印 21 项能力覆盖矩阵"
	@echo "  demo          完整舆情监控链路（含 HITL 审批）"
	@echo "  test          51 个单元测试"
	@echo "  ci            PR 冒烟评测 + CI 门禁（退出码即门禁结果）"
	@echo "  nightly       夜间全量评测 k=3"
	@echo "  full          全量 + 留出集 k=5"
	@echo "  replay        确定性轨迹回放"
	@echo "  ab            前缀缓存 A/B 实验"
	@echo "  export        GRPO 轨迹导出"
	@echo "  mcp           导出 MCP tool schema"
	@echo "  baseline      用 nightly 结果更新 baseline"

requirements:
	$(PY) main.py requirements

demo:
	$(PY) main.py demo

test:
	$(PY) -m unittest discover -s tests -v

ci:
	$(PY) main.py eval --suite pr_smoke

nightly:
	$(PY) main.py eval --suite nightly

full:
	$(PY) main.py eval --suite full --include-held-out

replay:
	$(PY) main.py replay

ab:
	$(PY) main.py ab

export:
	$(PY) main.py export

mcp:
	$(PY) main.py mcp

baseline:
	$(PY) main.py eval --suite nightly --update-baseline
