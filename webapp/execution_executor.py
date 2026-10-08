"""执行经服务端校验的问答来源计划。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from webapp.chat_models import Scope
from webapp.source_runtime import AnswerContext, SourceCall, SourceResult
from webapp.source_adapters import normalize_source
from webapp.chat_time import MarketWindow, select_market_bars
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


def market_kline_request_count(window: MarketWindow) -> int:
    if window.kind == "rolling_trading_days":
        return min(800, max(10, (window.trading_days or 5) * 2))
    if window.kind == "calendar_week":
        return 15
    if window.start_date is None or window.end_date is None:
        return 20
    return min(800, max(10, (window.end_date - window.start_date).days * 2 + 2))


def _select_kline(tencent_quote: Any, symbol: str, window: MarketWindow) -> dict[str, Any]:
    rows = tencent_quote.kline(symbol, period="day", count=market_kline_request_count(window), adjust="none")
    selection = select_market_bars(rows or (), window)
    if not selection.bars:
        raise RuntimeError("腾讯行情未返回请求窗口内的日线")
    return {
        "symbol": symbol, "period": "day", "window": window.label,
        "bars": list(selection.bars), "covered_dates": list(selection.covered_dates),
        "status": selection.status,
    }


def company_kline_handler(
    tencent_quote: Any, window: MarketWindow,
) -> Callable[[str, Scope], dict[str, Any]]:
    """获取已冻结公司 Scope 的日线并按服务端窗口筛选，不接受自由文本改写窗口。"""
    def handler(_question: str, scope: Scope) -> dict[str, Any]:
        if not scope.companies:
            raise RuntimeError("个股走势缺少公司范围")
        return {"provider": "tencent", **_select_kline(tencent_quote, scope.companies[0].code, window)}
    return handler


def a_share_indices_handler(
    tencent_quote: Any, window: MarketWindow,
) -> Callable[[str, Scope], dict[str, Any]]:
    """取四大指数的实际日线并统一筛选同一服务端窗口，保留单项失败。"""
    def handler(_question: str, _scope: Scope) -> dict[str, Any]:
        indices: dict[str, dict[str, Any]] = {}
        for symbol in _A_SHARE_INDICES:
            try:
                indices[symbol] = _select_kline(tencent_quote, symbol, window)
            except Exception:
                indices[symbol] = {"symbol": symbol, "status": "unavailable", "bars": [], "covered_dates": []}
        if not any(item.get("bars") for item in indices.values()):
            raise RuntimeError("腾讯行情未返回 A 股指数数据")
        return {"provider": "tencent", "window": window.label, "indices": indices}
    return handler


def market_overview_handler(
    mcp_client: Any, *, timeout: int, window: MarketWindow,
) -> Callable[[str, Scope], dict[str, Any]]:
    """聚合固定 MCP 调用并标记与用户窗口的匹配情况。"""
    def handler(_question: str, _scope: Scope) -> dict[str, Any]:
        weekly = window.kind in {"calendar_week", "rolling_trading_days"}
        artifacts = []
        for name, arguments in market_recap_tool_calls(weekly=weekly):
            query_window = arguments.get("days")
            matches = (
                (window.kind == "rolling_trading_days" and query_window == f"{window.trading_days}日")
                or (window.label == "今日" and query_window == "今日")
            )
            try:
                raw = mcp_client.call_tool(name, arguments, timeout=timeout)
                source = normalize_source(SourceCall("mcp", name, "market", arguments), raw)
                artifacts.append({
                    "tool": name, "query_window": query_window,
                    "window_match": bool(matches), "status": source.status,
                    "content": source.content, "coverage": source.coverage.summary(),
                })
            except Exception:
                artifacts.append({"tool": name, "query_window": query_window,
                                  "window_match": bool(matches), "status": "failed",
                                  "error": "来源调用失败"})
        if not any(artifact.get("status") in {"success", "partial"} for artifact in artifacts):
            raise RuntimeError("市场概览 MCP 均不可用")
        return {"provider": "stock-data-mcp", "window": window.label, "artifacts": artifacts}
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
        retrieval_hits: list[dict[str, Any]] = []
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
                for source in source_values:
                    retrieval_hits.extend(dict(hit) for hit in source.retrieval_hits)
                if not source_values and value is not None:
                    legacy_used.add("local_pdf" if step.kind == "retrieve" else
                                    "web" if step.kind == "web_search" else "market_data")
                source_failed = any(item.status != "success" for item in source_values)
                has_usable_source = any(item.status in {"success", "partial"} for item in source_values)
                row_status = ("failed" if source_failed and not has_usable_source else
                              "partial" if source_failed else "completed")
                if source_failed:
                    had_failure = True
                    # Optional holes remain visible, but a required aggregate can still
                    # answer with explicit limits when at least one source is usable.
                    required_missing = required_missing or (step.required and not has_usable_source)
                rows.append(ExecutionStepResult(step.id, step.kind, row_status, value))
            except Exception:
                rows.append(ExecutionStepResult(step.id, step.kind, "failed", error="来源调用失败"))
                had_failure = True
                required_missing = required_missing or step.required
        summary = {"local_pdf": "未使用", "market_data": "未使用", "web": "未使用"}
        for key in legacy_used:
            summary[key] = "已使用"
        grouped: dict[str, list[str]] = {}
        for source in sources:
            key = "local_pdf" if source.category == "local" else ("web" if source.category == "web" else "market_data")
            grouped.setdefault(key, []).append(source.status)
        for key, statuses in grouped.items():
            if all(status == "success" for status in statuses):
                summary[key] = "已使用"
            elif any(status in {"success", "partial"} for status in statuses):
                summary[key] = "部分取得"
            elif all(status == "unavailable" for status in statuses):
                summary[key] = "不可用"
            else:
                summary[key] = "获取失败"
        return ExecutionResult(tuple(rows), summary,
                               AnswerContext(tuple(sources), tuple(retrieval_hits), required_missing))
