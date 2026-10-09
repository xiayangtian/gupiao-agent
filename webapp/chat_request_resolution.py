"""Pure deterministic request resolution for bounded chat scopes and time windows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping

from webapp.chat_policy import QuestionRoute, QuestionRouter
from webapp.chat_scope import ScopeRequest
from webapp.chat_time import MarketWindow, resolve_financial_period, resolve_market_window


@dataclass(frozen=True)
class Clarification:
    message: str
    missing_field: Literal["subject", "window", "kind"]
    intent: str
    question: str
    company_code: str | None = None
    market_window: MarketWindow | None = None
    financial_period: str | None = None


@dataclass(frozen=True)
class ResolvedRequest:
    route: QuestionRoute
    effective_question: str
    market_window: MarketWindow | None
    financial_period: str | None
    window_origin: Literal["explicit", "recent_default", "clarification", "none"]
    allowed_source_kinds: frozenset[str]
    consumed_pending_version: str | None = None


_SOURCE_KINDS = {
    "report_fact": frozenset({"retrieve"}),
    "financial_trend": frozenset({"retrieve"}),
    "market_quote": frozenset({"market_quote"}),
    "market_trend": frozenset({"market_kline"}),
    "market_recap": frozenset({"market_indices", "market_overview", "web_search"}),
    "general_web": frozenset({"web_search"}),
    "general_knowledge": frozenset(),
    "research_task": frozenset(),
}
_FINANCIAL_TERMS = ("营收", "营业收入", "净利润", "净利", "财报", "年报", "半年报", "季报", "ROE", "毛利率", "现金流")
_RECENT_TERMS = ("近期", "最近", "近来")


def _restore_window(value: Any) -> MarketWindow | None:
    if not isinstance(value, Mapping):
        return None
    try:
        from datetime import date
        return MarketWindow(
            label=value["label"], kind=value["kind"],
            start_date=date.fromisoformat(value["start_date"]) if value.get("start_date") else None,
            end_date=date.fromisoformat(value["end_date"]) if value.get("end_date") else None,
            trading_days=value.get("trading_days"), timezone=value.get("timezone", "Asia/Shanghai"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def serialize_market_window(window: MarketWindow | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {
        "kind": window.kind, "label": window.label,
        "start_date": window.start_date.isoformat() if window.start_date else None,
        "end_date": window.end_date.isoformat() if window.end_date else None,
        "trading_days": window.trading_days, "timezone": window.timezone,
    }


def _pending_is_fresh(pending: Mapping[str, Any], now: datetime) -> bool:
    try:
        created = datetime.fromisoformat(str(pending["created_at"]))
        if created.tzinfo is None or created.utcoffset() is None:
            return False
        current = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
        age = current.astimezone(timezone.utc) - created.astimezone(timezone.utc)
        return timedelta(0) <= age <= timedelta(minutes=15)
    except (KeyError, TypeError, ValueError):
        return False


def _route_for_pending(intent: str) -> QuestionRoute | None:
    routes = {
        "market_quote": QuestionRoute("market_quote", True, False),
        "market_trend": QuestionRoute("market_trend", True, True),
        "market_recap": QuestionRoute("market_recap", False, True),
        "report_fact": QuestionRoute("report_fact", True, False),
        "financial_trend": QuestionRoute("financial_trend", True, True),
    }
    return routes.get(intent)


def _pending_reply_matches(question: str, route: QuestionRoute, pending: Mapping[str, Any], now: datetime) -> bool:
    missing = pending.get("missing_field")
    if missing == "window":
        if route.intent not in {"clarification", str(pending.get("intent") or "")}:
            return False
        return resolve_market_window(question, now) is not None or resolve_financial_period(question) is not None
    if missing == "subject":
        return route.intent in {"general_knowledge", "clarification"} and bool(question.strip())
    if missing == "kind":
        return route.intent not in {"general_knowledge", "clarification"}
    return False


def _clarification_for(
    route: QuestionRoute, question: str, window: MarketWindow | None,
    financial_period: str | None, company_code: str | None,
) -> Clarification | None:
    if route.intent == "clarification":
        if window is not None or any(term in question for term in ("近五交易日", "近5日", "最近五日")):
            return Clarification("请说明要分析大盘，还是哪只股票的走势。", "subject",
                                 "market_trend", question, market_window=window)
        return Clarification("请明确需要分析财务指标还是证券价格走势，并补充公司或市场范围。",
                             "kind", "market_trend", question)
    if route.intent == "financial_trend" and financial_period is None:
        return Clarification("请明确财报期间或趋势范围，例如 2025 年或近三年。",
                             "window", route.intent, question)
    if route.requires_time_window and window is None and route.intent in {"market_trend", "market_recap"}:
        subject = "请明确行情时间范围，例如今日、近五交易日或具体日期。"
        return Clarification(subject, "window", route.intent, question,
                             company_code=company_code, financial_period=financial_period)
    return None


def resolve_chat_request(
    question: str,
    scope_request: ScopeRequest,
    pending: Mapping[str, Any] | None,
    now: datetime,
) -> ResolvedRequest | Clarification:
    """Freeze intent/window/source families using only this turn and a trusted slot.

    The function never scans transcript text. Callers must provide the session-owned
    pending slot; scope resolution/company identity remains in ``ScopeResolver``.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must not be empty")
    if not isinstance(scope_request, ScopeRequest):
        raise ValueError("scope_request must be a ScopeRequest")
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")

    text = question.strip()
    router = QuestionRouter()
    route = router.classify(text)
    window = resolve_market_window(text, now)
    financial_period = resolve_financial_period(text)
    effective_question = text
    window_origin: Literal["explicit", "recent_default", "clarification", "none"] = "explicit" if window else "none"
    consumed_version = None

    if isinstance(pending, Mapping) and _pending_is_fresh(pending, now):
        pending_route = _route_for_pending(str(pending.get("intent") or ""))
        if pending_route and _pending_reply_matches(text, route, pending, now):
            route = pending_route
            effective_question = f"{str(pending.get('question') or '').strip()} {text}".strip()
            consumed_version = str(pending.get("version") or "") or None
            frozen_window = _restore_window(pending.get("market_window"))
            if frozen_window is not None:
                window = frozen_window
                window_origin = "clarification"
            else:
                window = resolve_market_window(text, now) or resolve_market_window(effective_question, now)
                if window is not None:
                    window_origin = "clarification"
            frozen_period = pending.get("financial_period")
            if isinstance(frozen_period, str) and frozen_period:
                financial_period = frozen_period

    code = str(pending.get("company_code") or "") if consumed_version and isinstance(pending, Mapping) else ""
    if code and (len(code) != 6 or not code.isdigit()):
        code = ""
    if route.intent in {"market_quote", "market_trend", "market_recap"}:
        if (scope_request.mode == "whole_corpus" and route.intent in {"market_quote", "market_trend"}) or (
            route.intent == "market_recap" and scope_request.focus_report
        ):
            return Clarification("当前分析范围与行情请求冲突，请确认继续使用行情范围还是返回财报范围。",
                                 "kind", route.intent, effective_question,
                                 company_code=code or None, market_window=window)

    if route.intent == "clarification" and consumed_version is None:
        clarification = _clarification_for(route, text, window, financial_period, None)
        return clarification or Clarification("请说明你希望分析财报经营指标，还是证券价格走势。",
                                               "kind", "market_trend", text)
    clarification = _clarification_for(route, effective_question, window, financial_period, code or None)
    if clarification is not None:
        return clarification

    if route.intent == "market_trend" and any(term in effective_question for term in _RECENT_TERMS):
        window_origin = "recent_default"
    if consumed_version is not None and window_origin == "explicit":
        window_origin = "clarification"
    return ResolvedRequest(
        route=route, effective_question=effective_question, market_window=window,
        financial_period=financial_period, window_origin=window_origin,
        allowed_source_kinds=_SOURCE_KINDS.get(route.intent, frozenset()),
        consumed_pending_version=consumed_version,
    )
