"""Sentinel Harness — 面向外部舆情监控的生产级 Agent Harness。

分层：
  tools/          工具注册、参数校验、沙箱、执行治理
  guard/          护栏、注入防护、审批、预算
  context/        上下文组装、压缩、交接、三层记忆
  runtime/        LangGraph 状态图、模型网关、checkpoint、服务容器
  observability/  trace / span / metrics / 轨迹回放
  eval/           任务库、grader、runner、CI 门禁、A/B
  flywheel/       失败聚类与 RL 轨迹导出
  business/       舆情影响本体、判定规则、结构化简报
"""
from .config import HARNESS_VERSION

__version__ = HARNESS_VERSION
__all__ = ["__version__", "HARNESS_VERSION"]
