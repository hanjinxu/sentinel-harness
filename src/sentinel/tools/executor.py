"""工具执行治理：参数校验 → 权限门 → 幂等 → 故障注入 → 重试/退避/熔断 → hooks → 观测。

错误分类决定兜底动作：
  TRANSIENT          → 指数退避重试，上限 2 次（Stripe 生产实践）
  LLM_RECOVERABLE    → 错误作为 ToolMessage 回传，模型自行改参
  USER_FIXABLE       → 中断，请求人工输入
  POLICY / BUDGET    → 不重试，立即终止该动作并记录审计
  FATAL              → 冒泡，保留现场
"""
from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core import (ErrorClass, Metrics, ToolCall, ToolResult, stable_hash, ToolRisk)
from .registry import ToolRegistry, ValidationError
from .sandbox import Sandbox, SandboxContext


@dataclass
class ToolContext:
    """handler 的唯一入口参数：业务依赖显式注入，禁止工具自行触网/读环境变量。"""
    run_id: str
    feeds: Dict[str, Any]
    state: Dict[str, Any]
    sandbox_ctx: SandboxContext
    ids: Any
    clock: Callable[[], int]
    artifacts_dir: str = "artifacts"

    def source(self, source_id: str) -> Optional[Dict[str, Any]]:
        for s in self.feeds.get("sources", []):
            if s["id"] == source_id:
                return s
        return None

    def valid_evidence_ids(self) -> List[str]:
        return [s["id"] for s in self.feeds.get("sources", [])]


@dataclass
class FaultProfile:
    """故障注入配置：让「恢复能力」成为可评测的一等公民，而不是靠运气遇到线上抖动。"""
    flaky: Dict[str, float] = field(default_factory=dict)      # tool -> 仅首次调用失败（可恢复）
    persistent: Dict[str, float] = field(default_factory=dict)  # tool -> 每次重试都可能失败（触发熔断）
    timeouts: List[str] = field(default_factory=list)
    malformed: List[str] = field(default_factory=list)
    slow_ms: int = 0
    seed: int = 0
    injection_source_ids: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]], seed: int = 0) -> "FaultProfile":
        d = d or {}
        return cls(flaky=d.get("flaky", {}), persistent=d.get("persistent", {}),
                   timeouts=d.get("timeouts", []), malformed=d.get("malformed", []),
                   slow_ms=d.get("slow_ms", 0), seed=seed,
                   injection_source_ids=d.get("injection_source_ids", []))


class FaultInjector:
    def __init__(self, profile: FaultProfile):
        self.p = profile
        self.rng = random.Random(profile.seed)

    def transient_failure(self, tool: str, attempt: int) -> bool:
        rate = self.p.flaky.get(tool, 0.0)
        return attempt == 0 and self.rng.random() < rate

    def persistent_failure(self, tool: str) -> bool:
        """持续故障：每次重试独立采样，用于观测熔断与 pass^k 的真实衰减。"""
        return self.rng.random() < self.p.persistent.get(tool, 0.0)

    def timeout(self, tool: str) -> bool:
        return tool in self.p.timeouts

    def malformed(self, tool: str) -> bool:
        return tool in self.p.malformed


Hook = Callable[[str, ToolCall, Any], Any]


class HookBus:
    """pre/post tool hooks。通过时保持安静，失败时才说话，避免 hook 本身污染上下文。"""

    def __init__(self) -> None:
        self.pre: List[Hook] = []
        self.post: List[Hook] = []
        self.events: List[Dict[str, Any]] = []

    def run_pre(self, call: ToolCall, args: Dict[str, Any]) -> Dict[str, Any]:
        cur = args
        for h in self.pre:
            cur = h("pre", call, cur) or cur
        return cur

    def run_post(self, call: ToolCall, result: Any) -> Any:
        cur = result
        for h in self.post:
            cur = h("post", call, cur) or cur
        return cur

    def record(self, name: str, **kw: Any) -> None:
        self.events.append({"hook": name, **kw})


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, sandbox: Sandbox, *,
                 fault: Optional[FaultProfile] = None, hooks: Optional[HookBus] = None,
                 metrics: Optional[Metrics] = None, tracer: Any = None):
        self.registry = registry
        self.sandbox = sandbox
        self.fault = FaultInjector(fault or FaultProfile())
        self.hooks = hooks or HookBus()
        self.metrics = metrics or Metrics()
        self.tracer = tracer
        self._idempotency: Dict[str, ToolResult] = {}
        self._consecutive_failures: Dict[str, int] = {}
        self._circuit_open: set[str] = set()
        self.circuit_threshold = 2   # 连续 2 次工具调用级失败即熔断，避免模型反复撞同一故障

    # ------------------------------------------------------------------
    def execute(self, call: ToolCall, tctx: ToolContext) -> ToolResult:
        spec = self.registry.get(call.name)
        if spec is None:
            return ToolResult(call.id, call.name, False, error=f"unknown_tool:{call.name}",
                              error_class=ErrorClass.LLM_RECOVERABLE)
        if call.name in self._circuit_open:
            return ToolResult(call.id, call.name, False, error="circuit_open",
                              error_class=ErrorClass.FATAL)

        # 1. 确定性参数校验（模型可自纠错）
        errs = self.registry.validate(call.name, call.arguments)
        if errs:
            return ToolResult(call.id, call.name, False, error=";".join(errs),
                              error_class=ErrorClass.LLM_RECOVERABLE)

        # 2. 权限门：策略写在代码里
        decision = self.sandbox.check(spec, call.arguments, tctx.sandbox_ctx)
        if decision.blocked:
            self.metrics.guard_blocks += 1
            self.hooks.record("policy_denied", tool=call.name, rule=decision.rule)
            return ToolResult(call.id, call.name, False, error=decision.reason,
                              error_class=ErrorClass.POLICY, blocked_by=decision.rule)
        if decision.requires_approval:
            # 审批由 LangGraph 的 interrupt 节点处理，这里只上报意图
            return ToolResult(call.id, call.name, False, error="approval_required",
                              error_class=ErrorClass.USER_FIXABLE, blocked_by="pending_approval",
                              payload={"args": decision.sanitized_args, "tool": call.name})

        args = self.hooks.run_pre(call, decision.sanitized_args)

        # 3. 幂等：同一 (tool, args) 不重复产生副作用
        idem_key = stable_hash({"tool": call.name, "args": args, "run": tctx.run_id})
        if spec.idempotent and idem_key in self._idempotency:
            cached = self._idempotency[idem_key]
            return ToolResult(call.id, call.name, cached.ok, cached.payload, cached.error,
                              cached.error_class, 0, 0, cached.tainted, None, 0, True)

        started = time.monotonic()
        result = self._with_retry(call, spec, args, tctx, decision.redactions)
        result.latency_ms = int((time.monotonic() - started) * 1000)
        result.payload = self.hooks.run_post(call, result.payload)
        if spec.idempotent and result.ok:
            self._idempotency[idem_key] = result

        self.metrics.tool_calls += 1
        self.metrics.tool_failures += int(not result.ok)
        self.metrics.retries += result.retries
        if self.tracer:
            self.tracer.tool_span(call, result, decision.rule)
        return result

    def _with_retry(self, call: ToolCall, spec: Any, args: Dict[str, Any], tctx: ToolContext,
                    redactions: int) -> ToolResult:
        last: Optional[ToolResult] = None
        for attempt in range(spec.max_retries + 1):
            if self.fault.timeout(call.name):
                last = ToolResult(call.id, call.name, False, error="tool_timeout",
                                  error_class=ErrorClass.TRANSIENT, retries=attempt)
            elif self.fault.transient_failure(call.name, attempt):
                last = ToolResult(call.id, call.name, False, error="upstream_503",
                                  error_class=ErrorClass.TRANSIENT, retries=attempt)
            elif self.fault.persistent_failure(call.name):
                last = ToolResult(call.id, call.name, False, error="upstream_unavailable",
                                  error_class=ErrorClass.TRANSIENT, retries=attempt)
            else:
                try:
                    payload = spec.handler(args, tctx)
                    if self.fault.malformed(call.name):
                        payload = {"__malformed__": True}
                    last = ToolResult(call.id, call.name, True, payload=payload, retries=attempt)
                    last.tainted = bool(isinstance(payload, dict) and payload.get("untrusted"))
                except ValidationError as e:
                    last = ToolResult(call.id, call.name, False, error=str(e),
                                      error_class=ErrorClass.LLM_RECOVERABLE, retries=attempt)
                except Exception as e:  # noqa: BLE001 - 保留现场，交给上层分类
                    last = ToolResult(call.id, call.name, False, error=f"{type(e).__name__}:{e}",
                                      error_class=ErrorClass.FATAL, retries=attempt)
            if last.ok or last.error_class in {ErrorClass.POLICY, ErrorClass.FATAL,
                                               ErrorClass.LLM_RECOVERABLE, ErrorClass.USER_FIXABLE}:
                break
            if attempt < spec.max_retries:
                time.sleep(0.001 * (2 ** attempt))     # 退避；离线评测保持极短
        assert last is not None
        if last.ok:
            self._consecutive_failures[call.name] = 0
        else:
            n = self._consecutive_failures.get(call.name, 0) + 1
            self._consecutive_failures[call.name] = n
            if n >= self.circuit_threshold:
                self._circuit_open.add(call.name)
                self.hooks.record("circuit_open", tool=call.name, failures=n)
        return last

    # ------------------------------------------------------------------
    def execute_batch(self, calls: List[ToolCall], tctx: ToolContext) -> List[ToolResult]:
        """只读工具并发，写工具串行 —— 由 ToolRisk 声明驱动，而非调用方自觉。"""
        reads = [c for c in calls if (self.registry.get(c.name) and
                                      self.registry.require(c.name).risk is ToolRisk.READ)]
        read_ids = {c.id for c in reads}
        writes = [c for c in calls if c.id not in read_ids]
        results: List[ToolResult] = []
        if reads:
            with ThreadPoolExecutor(max_workers=min(4, len(reads))) as pool:
                futures = {pool.submit(self.execute, c, tctx): c for c in reads}
                for f, c in futures.items():
                    try:
                        results.append(f.result())
                    except Exception as e:  # noqa: BLE001
                        results.append(ToolResult(c.id, c.name, False, error=repr(e),
                                                  error_class=ErrorClass.FATAL))
        for c in writes:
            results.append(self.execute(c, tctx))
        order = {c.id: i for i, c in enumerate(calls)}
        return sorted(results, key=lambda r: order.get(r.call_id, 999))
