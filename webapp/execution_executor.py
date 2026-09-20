"""执行经服务端校验的问答来源计划。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from webapp.chat_models import Scope
from webapp.execution_plan import ExecutionPlan

_MARKET_KINDS = frozenset((
    "market_quote", "market_indices", "market_breadth",
    "sector_performance", "market_fund_flow",
))
_A_SHARE_INDICES = ("sh000001", "sz399001", "sz399006", "sh000688")


@dataclass(frozen=True)
class ExecutionStepResult:
    id: str
    kind: str
    status: str
    value: Any = None
    error: str = ""


@dataclass(frozen=True)
class ExecutionResult:
    steps: tuple[ExecutionStepResult, ...]
    source_summary: dict[str, str]


def a_share_indices_handler(tencent_quote: Any) -> Callable[[str, Scope], dict[str, Any]]:
    """从腾讯行情返回 A 股主要指数的日/周窗口，不补造缺失市场数据。"""
    def handler(question: str, _scope: Scope) -> dict[str, Any]:
        weekly = any(word in question for word in ("上周", "本周", "周度", "一周"))
        period, count = ("week", 1) if weekly else ("day", 1)
        rows = {}
        for symbol in _A_SHARE_INDICES:
            candles = tencent_quote.kline(symbol, period=period, count=count, adjust="none")
            if candles:
                rows[symbol] = candles[-1]
        if not rows:
            raise RuntimeError("腾讯行情未返回 A 股指数数据")
        return {"provider": "tencent", "window": "week" if weekly else "day", "indices": rows}
    return handler


class ExecutionExecutor:
    def __init__(
        self,
        *,
        retrieve: Callable[..., Any] | None = None,
        web_search: Callable[..., Any] | None = None,
        **handlers: Callable[..., Any] | None,
    ) -> None:
        self.handlers = {"retrieve": retrieve, "web_search": web_search, **handlers}

    def execute(self, plan: ExecutionPlan, question: str, scope: Scope) -> ExecutionResult:
        rows: list[ExecutionStepResult] = []
        used = {"retrieve": False, "market": False, "web_search": False}
        for step in plan.steps:
            if step.kind == "answer":
                rows.append(ExecutionStepResult(step.id, step.kind, "completed"))
                continue
            if step.kind == "retrieve":
                used["retrieve"] = True
            elif step.kind == "web_search":
                used["web_search"] = True
            elif step.kind in _MARKET_KINDS:
                used["market"] = True
            handler = self.handlers.get(step.kind)
            if handler is None:
                rows.append(ExecutionStepResult(step.id, step.kind, "unavailable", error="此阶段数据源尚不可用"))
                continue
            try:
                rows.append(ExecutionStepResult(step.id, step.kind, "completed", handler(question, scope)))
            except Exception:
                rows.append(ExecutionStepResult(step.id, step.kind, "failed", error="来源调用失败"))
        return ExecutionResult(tuple(rows), {
            "local_pdf": "已使用" if used["retrieve"] else "未使用",
            "market_data": "已使用" if used["market"] else "未使用",
            "web": "已使用" if used["web_search"] else "未使用",
        })
