"""执行经服务端校验的问答来源计划。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from webapp.chat_models import Scope
from webapp.execution_plan import ExecutionPlan

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

class ExecutionExecutor:
    def __init__(self, *, retrieve: Callable[..., Any] | None = None, market_quote: Callable[..., Any] | None = None, web_search: Callable[..., Any] | None = None):
        self.retrieve, self.market_quote, self.web_search = retrieve, market_quote, web_search

    def execute(self, plan: ExecutionPlan, question: str, scope: Scope) -> ExecutionResult:
        handlers = {"retrieve": self.retrieve, "market_quote": self.market_quote, "web_search": self.web_search}
        rows = []
        used = {"retrieve": False, "market_quote": False, "web_search": False}
        for step in plan.steps:
            if step.kind == "answer":
                rows.append(ExecutionStepResult(step.id, step.kind, "completed"))
                continue
            used[step.kind] = True
            handler = handlers[step.kind]
            if handler is None:
                rows.append(ExecutionStepResult(step.id, step.kind, "failed", error="来源当前不可用"))
                continue
            try:
                rows.append(ExecutionStepResult(step.id, step.kind, "completed", handler(question, scope)))
            except Exception:
                rows.append(ExecutionStepResult(step.id, step.kind, "failed", error="来源调用失败"))
        return ExecutionResult(tuple(rows), {"local_pdf": "已使用" if used["retrieve"] else "未使用", "market_data": "已使用" if used["market_quote"] else "未使用", "web": "已使用" if used["web_search"] else "未使用"})
