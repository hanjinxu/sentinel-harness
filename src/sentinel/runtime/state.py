"""LangGraph 状态定义与 checkpoint 管理。

为什么不再自研状态机：LangGraph 在 super-step 边界写 checkpoint，
天然支持中断恢复、time-travel debugging 与 human-in-the-loop。
本项目在其之上补充：
  - 状态字段全部可 JSON 序列化，便于落盘与回放；
  - 每个 run 独占 thread_id，评测 trial 之间无共享状态；
  - 把 checkpoint 历史作为 trace 的一部分，用于失败归因。
"""
from __future__ import annotations

from typing import Any, Dict, List, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command


class SentinelState(TypedDict, total=False):
    # 身份与可复现性
    task_id: str
    task_prompt: str
    scenario: Dict[str, Any]
    expected: Dict[str, Any]
    run_id: str
    trace_id: str
    config_fingerprint: str
    seed: int
    # 规划与上下文
    plan: Dict[str, Any]
    rubric: List[Dict[str, Any]]
    messages: List[Dict[str, Any]]
    context_prompt: str
    tool_schema: str
    thoughts: List[str]
    handoff: Dict[str, Any]
    # 执行
    pending_calls: List[Dict[str, Any]]
    tool_payloads: Dict[str, Any]
    completed_tools: List[str]
    mention_ids: List[str]
    fetched_ids: List[str]
    tool_calls_log: List[Dict[str, Any]]
    tool_results_log: List[Dict[str, Any]]
    last_error: Dict[str, Any]
    turn: int
    iteration: int
    # 治理
    violations: List[Dict[str, Any]]
    tainted: bool
    injection_seen: bool
    injection_attempted: bool
    needs_approval: bool
    approval_payload: Dict[str, Any]
    approval_done: bool
    approval_decision: Dict[str, Any]
    blocked_tools: List[str]
    # 产物与评估
    brief: Dict[str, Any]
    signals: Dict[str, Any]
    evidence_registry: List[str]
    evaluation: Dict[str, Any]
    feedback: List[str]
    stop_reason: str
    final_output: Dict[str, Any]
    checkpoint_ids: List[str]
    compaction_events: List[str]


class EvaluatorState(TypedDict, total=False):
    """独立评估器子图的 state schema。

    刻意不声明 messages / thoughts / pending_calls —— LangGraph 会按子图 schema
    过滤父 state，因此评估器在框架层就看不到 Generator 的推理链。
    这是 Generator–Evaluator 分离的硬边界，不依赖「prompt 里叮嘱它别看」。
    """
    brief: Dict[str, Any]
    rubric: List[Dict[str, Any]]
    evidence_registry: List[str]
    signals: Dict[str, Any]
    evaluation: Dict[str, Any]


class CheckpointManager:
    def __init__(self) -> None:
        self.saver = InMemorySaver()

    @staticmethod
    def config(thread_id: str) -> Dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": 120}

    def resume(self, graph: Any, thread_id: str, decision: Dict[str, Any]) -> Dict[str, Any]:
        """用 Command(resume=...) 唤醒被 interrupt 暂停的图。"""
        return graph.invoke(Command(resume=decision), config=self.config(thread_id))

    def history(self, graph: Any, thread_id: str) -> List[Dict[str, Any]]:
        out = []
        try:
            for snap in graph.get_state_history(self.config(thread_id)):
                meta = snap.metadata or {}
                out.append({"checkpoint_id": snap.config.get("configurable", {}).get("checkpoint_id"),
                            "next_nodes": list(snap.next or ()),
                            "step": meta.get("step"),
                            "source": meta.get("source"),
                            "has_brief": bool(snap.values.get("brief"))})
        except Exception:  # noqa: BLE001 - 不同 langgraph 版本元数据字段可能不同
            pass
        return out

    def current(self, graph: Any, thread_id: str) -> Dict[str, Any]:
        snap = graph.get_state(self.config(thread_id))
        return dict(snap.values) if snap else {}
