"""服务容器：一次 run 的全部依赖在此组装，保证 trial 之间零共享状态。

设计说明：这是 harness 的 composition root。LangGraph 负责控制流，
但「权限、评测、观测、预算、业务规则」都不属于框架，必须由容器显式注入；
把所有副作用收口到一个对象，才有可能做干净环境与确定性回放。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..business.domain import RUBRIC, valid_evidence_ids
from ..config import HarnessConfig
from ..context.engine import ContextEngine
from ..core import (DeterministicIds, Metrics, ToolCall, ToolResult, Trace,
                    stable_hash, to_dict)
from ..guard.budget import BudgetGuard
from ..guard.security import ApprovalGate, Guard, InjectionDetector, Tripwire
from ..observability.tracing import (ReplayRecorder, ReplaySource, Tracer, build_trace)
from ..tools.builtin import register_builtin_tools
from ..tools.executor import FaultProfile, HookBus, ToolContext, ToolExecutor
from ..tools.registry import ToolRegistry
from ..tools.sandbox import Sandbox, SandboxContext
from .model import ModelGateway, ScriptedPolicy
from .state import CheckpointManager

GRANTED_SCOPES = [
    "feed:read", "entity:read", "sentiment:read", "propagation:read", "risk:read",
    "impact:compute", "evidence:verify", "brief:write", "alert:publish",
    "human:request", "note:read",
]


class Runtime:
    def __init__(self, cfg: HarnessConfig, feeds: Dict[str, Any], *,
                 task_id: str = "demo", task_prompt: str = "", scenario: Optional[Dict[str, Any]] = None,
                 expected: Optional[Dict[str, Any]] = None, seed: Optional[int] = None,
                 fault_profile: Optional[Dict[str, Any]] = None,
                 replay_path: Optional[str] = None,
                 artifacts_dir: str = "artifacts"):
        self.cfg = cfg
        self.feeds = feeds
        self.task_id = task_id
        self.task_prompt = task_prompt
        self.scenario = scenario or {}
        self.expected = expected or {}
        self.seed = cfg.seed if seed is None else seed
        self.artifacts_dir = artifacts_dir
        self.run_id = f"run-{stable_hash({'task': task_id, 'seed': self.seed, 'cfg': cfg.fingerprint()})}"
        self.trace_id = f"tr-{self.run_id}"
        self.fp = cfg.fingerprint()
        self.ids = DeterministicIds(self.seed)

        self.metrics = Metrics()
        self.tracer = Tracer(self.trace_id, self.run_id, self.fp, self.seed)
        self.recorder = ReplayRecorder(self.fp, self.seed)
        self.replay = ReplaySource(replay_path) if replay_path else None
        self.budget = BudgetGuard(cfg.budget, self.metrics)
        self.guard = Guard(InjectionDetector(), Tripwire(limit=6),
                           ApprovalGate(cfg.approval_mode), cfg.company_allowed_hosts)
        self.context = ContextEngine(cfg, artifacts_dir)
        self.checkpoints = CheckpointManager()

        self.registry = register_builtin_tools(ToolRegistry())
        self.sandbox = Sandbox(cfg.company_allowed_hosts, cfg.workspace_root)
        self.sandbox_ctx = SandboxContext(
            run_id=self.run_id, granted_scopes=GRANTED_SCOPES,
            allowed_hosts=cfg.company_allowed_hosts, workspace_root=cfg.workspace_root,
            approval_mode=cfg.approval_mode,
        )
        self.hooks = HookBus()
        self.hooks.post.append(lambda phase, call, result: self.context.offload(call.id, result))
        self.faults = FaultProfile.from_dict(
            fault_profile if fault_profile is not None else cfg.fault_profile, self.seed)
        self.executor = ToolExecutor(self.registry, self.sandbox, fault=self.faults,
                                     hooks=self.hooks, metrics=self.metrics, tracer=self.tracer)
        self.policy = ScriptedPolicy(
            feeds, susceptible_to_injection=bool(self.scenario.get("susceptible_to_injection", True)))
        self.model = ModelGateway(cfg, self.policy, budget=self.budget, tracer=self.tracer,
                                  recorder=self.recorder, replay=self.replay)

        self.tool_calls: List[ToolCall] = []
        self.tool_results: List[ToolResult] = []
        self.violations: List[Dict[str, Any]] = []
        self.tool_schema = json.dumps(self.registry.openai_tools(), ensure_ascii=False)
        self.evidence_registry = valid_evidence_ids(feeds)
        self.rubric = RUBRIC

    # ------------------------------------------------------------------
    def tool_context(self, state: Dict[str, Any]) -> ToolContext:
        merged = {
            "tool_payloads": state.get("tool_payloads", {}),
            "injection_source_ids": self.scenario.get("injection_source_ids", []),
        }
        return ToolContext(run_id=self.run_id, feeds=self.feeds, state=merged,
                           sandbox_ctx=self.sandbox_ctx, ids=self.ids,
                           clock=lambda: self.tracer._now(), artifacts_dir=self.artifacts_dir)

    def sandbox_sync(self, state: Dict[str, Any]) -> None:
        self.sandbox_ctx.tainted = bool(state.get("tainted", False))

    def initial_state(self) -> Dict[str, Any]:
        from .state import SentinelState  # noqa: F401 - 仅作类型说明
        return {
            "task_id": self.task_id, "task_prompt": self.task_prompt,
            "scenario": self.scenario, "expected": self.expected,
            "run_id": self.run_id, "trace_id": self.trace_id,
            "config_fingerprint": self.fp, "seed": self.seed,
            "plan": {}, "rubric": self.rubric, "messages": [], "context_prompt": "",
            "tool_schema": self.tool_schema, "thoughts": [], "handoff": {},
            "pending_calls": [], "tool_payloads": {}, "completed_tools": [],
            "mention_ids": [], "fetched_ids": [], "tool_calls_log": [], "tool_results_log": [],
            "last_error": {}, "turn": 0, "iteration": 0,
            "violations": [], "tainted": False, "injection_seen": False,
            "injection_attempted": False, "needs_approval": False,
            "approval_payload": {}, "approval_done": False, "approval_decision": {},
            "blocked_tools": [], "brief": {}, "signals": {},
            "evidence_registry": self.evidence_registry, "evaluation": {}, "feedback": [],
            "stop_reason": "FINAL_ANSWER", "final_output": {},
            "checkpoint_ids": [], "compaction_events": [],
        }

    def build_trace(self, state: Dict[str, Any]) -> Trace:
        t = build_trace(self.tracer, state, self.metrics, self.tool_calls,
                        self.tool_results, self.violations,
                        state.get("checkpoint_ids", []))
        return t

    def save(self, state: Dict[str, Any], trace: Trace) -> Dict[str, str]:
        base = Path(self.artifacts_dir) / self.run_id
        base.mkdir(parents=True, exist_ok=True)
        (base / "trace.json").write_text(json.dumps(to_dict(trace), ensure_ascii=False, indent=2), encoding="utf-8")
        (base / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        spans = self.tracer.export_jsonl(str(base / "spans.jsonl"))
        replay = self.recorder.save(str(base / "replay.json"))
        return {"trace": str(base / "trace.json"), "state": str(base / "state.json"),
                "spans": spans, "replay": replay, "dir": str(base)}
