"""硬预算：在执行点限流，而不是事后对账。

覆盖四类执行约束：步数、token、时延、货币成本。
前缀缓存命中按 10% 计价（对应 prompt caching 的成本模型），
因此 "hold pass rate while cutting cost" 这类 harness 改动可以被量化。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional

from ..config import BudgetConfig
from ..core import Metrics, StopReason


@dataclass
class BudgetState:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_cny: float = 0.0
    turns: int = 0
    started_at: float = 0.0
    stop_reason: Optional[StopReason] = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BudgetGuard:
    def __init__(self, cfg: BudgetConfig, metrics: Optional[Metrics] = None,
                 clock=time.monotonic):
        self.cfg = cfg
        self.metrics = metrics or Metrics()
        self.clock = clock
        self.state = BudgetState(started_at=self.clock())

    def charge_llm(self, prompt_tokens: int, completion_tokens: int, cached_tokens: int,
                   cost_per_1k_prompt: float, cost_per_1k_completion: float) -> None:
        billable_prompt = max(0, prompt_tokens - cached_tokens)
        cost = (billable_prompt / 1000 * cost_per_1k_prompt
                + cached_tokens / 1000 * cost_per_1k_prompt * 0.10
                + completion_tokens / 1000 * cost_per_1k_completion)
        self.state.prompt_tokens += prompt_tokens
        self.state.completion_tokens += completion_tokens
        self.state.cached_tokens += cached_tokens
        self.state.cost_cny = round(self.state.cost_cny + cost, 6)
        self.metrics.prompt_tokens += prompt_tokens
        self.metrics.completion_tokens += completion_tokens
        self.metrics.cached_tokens += cached_tokens
        self.metrics.cost_cny = self.state.cost_cny
        self.metrics.llm_calls += 1

    def tick_turn(self) -> None:
        self.state.turns += 1
        self.metrics.turns += 1

    def check(self) -> Optional[StopReason]:
        s, c = self.state, self.cfg
        if s.turns >= c.max_turns:
            return StopReason.MAX_TURNS
        if s.prompt_tokens >= c.max_prompt_tokens or s.total_tokens >= c.max_total_tokens:
            return StopReason.TOKEN_BUDGET
        if s.cost_cny >= c.max_cost_cny:
            return StopReason.COST_BUDGET
        if (self.clock() - s.started_at) * 1000 >= c.max_wall_ms:
            return StopReason.WALL_CLOCK
        return None

    def snapshot(self) -> Dict[str, float]:
        return {"prompt_tokens": self.state.prompt_tokens,
                "completion_tokens": self.state.completion_tokens,
                "cached_tokens": self.state.cached_tokens,
                "total_tokens": self.state.total_tokens,
                "cost_cny": round(self.state.cost_cny, 6),
                "turns": self.state.turns,
                "elapsed_ms": int((self.clock() - self.state.started_at) * 1000),
                "cache_hit_rate": round(self.state.cached_tokens / max(1, self.state.prompt_tokens), 4)}
