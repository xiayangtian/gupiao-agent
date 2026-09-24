"""执行经服务端校验的问答来源计划。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from webapp.chat_models import Scope
from webapp.source_runtime import AnswerContext, SourceResult
from financial_report_fetcher.rag.mcp_tools import market_recap_tool_calls
from webapp.execution_plan import ExecutionPlan

_MARKET_KINDS = frozenset((
    "market_quote", "market_kline", "market_indices", "market_breadth",
    "sector_performance", "market_fund_flow", "market_overview",
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
    context: AnswerContext = AnswerContext()


def company_kline_handler(tencent_quote: Any) -> Callable[[str, Scope], dict[str, Any]]:
    """获取已冻结公司 Scope 的近期日/周 K 线，绝不接受模型股票代码。"""
    def handler(question: str, scope: Scope) -> dict[str, Any]:
        if not scope.companies:
            raise RuntimeError("个股走势缺少公司范围")
        weekly = any(word in question for word in ("上周", "本周", "周度", "一周"))
        period, count = ("week", 5) if weekly else ("day", 10)
        rows = tencent_quote.kline(scope.companies[0].code, period=period, count=count, adjust="none")
        if not rows:
            raise RuntimeError("腾讯行情未返回个股 K 线")
        return {"provider": "tencent", "symbol": scope.companies[0].code, "period": period, "bars": rows}
    return handler


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


def market_overview_handler(mcp_client: Any, *, timeout: int) -> Callable[[str, Scope], dict[str, Any]]:
    """聚合固定 A 股 MCP 调用；失败产物保留，不能由模型补造市场数据。"""
    def handler(question: str, _scope: Scope) -> dict[str, Any]:
        weekly = any(word in question for word in ("上周", "本周", "周度", "一周"))
        artifacts = []
        for name, arguments in market_recap_tool_calls(weekly=weekly):
            try:
                artifacts.append({"tool": name, "value": mcp_client.call_tool(name, arguments, timeout=timeout)})
            except Exception:
                artifacts.append({"tool": name, "error": "来源调用失败"})
        if not any("value" in artifact for artifact in artifacts):
            raise RuntimeError("市场概览 MCP 均不可用")
        return {"provider": "stock-data-mcp", "window": "week" if weekly else "day", "artifacts": artifacts}
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
        sources: list[SourceResult] = []
        legacy_used: set[str] = set()
        required_missing = False
        had_failure = False
        for step in plan.steps:
            if step.kind == "answer":
                rows.append(ExecutionStepResult(step.id, step.kind,
                                                "pending" if required_missing else "completed"))
                continue
            dependencies = [row for row in rows if row.id in step.depends_on]
            if any(row.status in {"failed", "unavailable", "skipped"} for row in dependencies):
                rows.append(ExecutionStepResult(step.id, step.kind, "skipped", error="前置来源步骤未完成"))
                if step.required:
                    required_missing = True
                    had_failure = True
                continue
            handler = self.handlers.get(step.kind)
            if handler is None:
                rows.append(ExecutionStepResult(step.id, step.kind, "unavailable", error="此阶段数据源尚不可用"))
                had_failure = True
                required_missing = required_missing or step.required
                continue
            try:
                value = handler(question, scope)
                values = value if isinstance(value, tuple) else (value,)
                source_values = [item for item in values if isinstance(item, SourceResult)]
                sources.extend(source_values)
                if not source_values and value is not None:
                    legacy_used.add("local_pdf" if step.kind == "retrieve" else
                                    "web" if step.kind == "web_search" else "market_data")
                source_failed = any(item.status != "success" for item in source_values)
                has_usable_source = any(item.status in {"success", "partial"} for item in source_values)
                row_status = ("failed" if source_failed and not has_usable_source else
                              "partial" if source_failed else "completed")
                if source_failed:
                    had_failure = True
                    required_missing = required_missing or (step.required and not all(
                        item.status == "success" for item in source_values
                    ))
                rows.append(ExecutionStepResult(step.id, step.kind, row_status, value))
            except Exception:
                rows.append(ExecutionStepResult(step.id, step.kind, "failed", error="来源调用失败"))
                had_failure = True
                required_missing = required_missing or step.required
        summary = {"local_pdf": "未使用", "market_data": "未使用", "web": "未使用"}
        for key in legacy_used:
            summary[key] = "已使用"
        for source in sources:
            key = "local_pdf" if source.category == "local" else ("web" if source.category == "web" else "market_data")
            summary[key] = ("已使用" if source.status == "success" else
                            "部分取得" if source.status == "partial" else
                            "获取失败" if source.status == "failed" else "不可用")
        return ExecutionResult(tuple(rows), summary,
                               AnswerContext(tuple(sources), required_missing=required_missing))
