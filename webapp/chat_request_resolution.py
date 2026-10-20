"""Pure deterministic request resolution for bounded chat scopes and time windows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Literal, Mapping

from webapp.chat_followups import PreviousTurnContext, is_context_only_followup
from webapp.chat_intent import ChatIntentAnalysis
from webapp.chat_policy import QuestionRoute
from webapp.chat_scope import ScopeRequest
from webapp.chat_time import (
    MarketWindow, has_market_period_expression, is_fuzzy_market_horizon,
    is_orphan_market_horizon, market_window_error, resolve_financial_period, resolve_market_window,
)


@dataclass(frozen=True)
class Clarification:
    message: str
    missing_field: Literal["subject", "window", "kind"]
    intent: str
    question: str
    company_code: str | None = None
    market_window: MarketWindow | None = None
    financial_period: str | None = None
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedRequest:
    route: QuestionRoute
    effective_question: str
    market_window: MarketWindow | None
    financial_period: str | None
    window_origin: Literal["explicit", "fuzzy_default", "default", "clarification", "none"]
    allowed_source_kinds: frozenset[str]
    consumed_pending_version: str | None = None
    context_only_followup: bool = False


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
_MARKET_WORDS = ("行情", "走势", "趋势", "大盘", "股市", "市场", "指数", "涨跌", "股价")
_NON_A_SHARE_SUBJECTS = (
    "港股", "香港股", "美股", "境外", "海外", "黄金", "原油", "商品", "行业", "板块",
    "半导体", "债券", "基金", "外汇", "期货", "加密货币", "比特币",
)
_SINGLE_STOCK_TERMS = ("个股", "股价", "某只", "这只", "单只")
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
        return route.intent in {
            "general_knowledge", "clarification", "market_quote", "market_trend",
            "report_fact", "financial_trend",
        } and bool(question.strip())
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
                             "kind", "market_trend", question,
                             options=("财务指标", "证券价格走势"))
    if route.intent == "financial_trend" and financial_period is None:
        return Clarification("请明确财报期间或趋势范围，例如 2025 年或近三年。",
                             "window", route.intent, question)
    if route.requires_time_window and window is None and route.intent in {"market_trend", "market_recap"}:
        error = market_window_error(question)
        if error == "market_window_too_long":
            message = "该行情期限超出当前安全查询上限（自然窗口最多800个月、交易日窗口最多800个），请缩短时间范围。"
        elif has_market_period_expression(question):
            message = "无法解析该行情期限，请改用自然日、交易日、周、月或年表达。"
        else:
            message = "请明确行情时间范围，例如近五交易日、近半年或具体日期。"
        return Clarification(message, "window", route.intent, question,
                             company_code=company_code, financial_period=financial_period)
    return None


def _generic_market_question(text: str) -> bool:
    if any(term in text for term in ("什么是", "解释", "含义", "概念", "定义")):
        return False
    if text.strip() in {"行情", "大盘", "市场", "股市", "指数"}:
        return True
    if (not any(term in text for term in _MARKET_WORDS)
            or any(term in text for term in (*_NON_A_SHARE_SUBJECTS, *_SINGLE_STOCK_TERMS))
            or any(char.isdigit() for char in text)):
        return False
    residual = text.casefold()
    for term in sorted((
        "a股", "怎么样", "分析", "查看", "看看", "查询", "复盘", "表现", "涨跌", "走势", "趋势",
        "行情", "大盘", "股市", "市场", "指数", "今日", "今天", "如何", "再", "帮我", "请", "看", "了解", "的", "下",
    ), key=len, reverse=True):
        residual = residual.replace(term, "")
    return not re.sub(r"[\W_]+", "", residual)


def _previous_market_route(context: PreviousTurnContext | None) -> QuestionRoute | None:
    if context is None or context.status not in {"completed", "partial"}:
        return None
    if context.source_mode == "market_recap" and not context.companies:
        return QuestionRoute("market_recap", False, True)
    if context.source_mode == "external_market" and context.companies:
        if "market_quote" in context.steps:
            return QuestionRoute("market_quote", True, False)
        return QuestionRoute("market_trend", True, True)
    if context.source_mode == "general_web" and any(
        term in context.question for term in ("港股", "香港股", "美股", "境外市场", "海外市场")
    ):
        return QuestionRoute("general_web", False, False)
    return None


def _route_from_analysis(analysis: ChatIntentAnalysis) -> QuestionRoute:
    if analysis.category == "market_analysis":
        if analysis.subject_kind == "broad_market":
            return QuestionRoute("market_recap", False, True)
        if analysis.subject_kind == "equity":
            if analysis.task_kind == "attribution":
                return QuestionRoute("general_web", True, False)
            if analysis.task_kind == "trend":
                return QuestionRoute("market_trend", True, True)
            if analysis.task_kind == "recap":
                return QuestionRoute("market_recap", False, True)
            return QuestionRoute("market_quote", True, False)
        if analysis.subject_kind == "none" and analysis.task_kind in {"trend", "quote"}:
            return QuestionRoute("market_trend", True, True)
        # Industry, commodity and overseas assets may use authorized web evidence,
        # but never inherit the single-company quote/K-line tools.
        return QuestionRoute("general_web", False, False)
    if analysis.category == "financial_report":
        if analysis.task_kind == "report_trend":
            return QuestionRoute("financial_trend", True, True)
        return QuestionRoute("report_fact", True, False)
    if analysis.category == "financial_knowledge":
        return QuestionRoute("general_knowledge", False, False)
    if analysis.category == "unclear":
        return QuestionRoute("clarification", False, False)
    if analysis.category == "non_financial":
        return QuestionRoute("clarification", False, False)
    raise ValueError("unsupported chat intent category")


def resolve_chat_request(
    question: str,
    scope_request: ScopeRequest,
    pending: Mapping[str, Any] | None,
    now: datetime,
    *,
    analysis: ChatIntentAnalysis,
    previous_context: PreviousTurnContext | None = None,
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

    if not isinstance(analysis, ChatIntentAnalysis):
        raise ValueError("analysis must be a validated ChatIntentAnalysis")
    text = question.strip()
    pending_fresh = isinstance(pending, Mapping) and _pending_is_fresh(pending, now)
    previous_market_route = None if pending_fresh else _previous_market_route(previous_context)
    orphan_horizon = is_orphan_market_horizon(text)
    generic_market = _generic_market_question(text)
    default_a_share = (
        not pending_fresh and previous_market_route is None
        and (
            (orphan_horizon and analysis.category not in {"financial_report", "non_financial"})
            or (analysis.category == "unclear" and generic_market)
            or (analysis.category == "market_analysis" and (
                (orphan_horizon and analysis.subject_kind == "none")
                or (generic_market and not any(term in text for term in _SINGLE_STOCK_TERMS)
                    and analysis.subject_kind in {"none", "broad_market"})
                or (analysis.subject_kind == "none" and not analysis.stock_code
                    and not any(term in text for term in _SINGLE_STOCK_TERMS)
                    and analysis.task_kind in {"quote", "trend", "recap"})
            ))
        )
    )
    explicit_overseas = any(term in text for term in ("港股", "香港股", "美股", "境外市场", "海外市场"))
    contextual_window_update = bool(previous_market_route and (
        orphan_horizon or (generic_market and not explicit_overseas)
    ))
    default_overseas = analysis.category == "unclear" and explicit_overseas
    contextual_market_update = bool(previous_market_route and explicit_overseas)
    market_request = analysis.category == "market_analysis" or default_a_share or default_overseas or contextual_window_update or contextual_market_update

    if default_a_share:
        route = QuestionRoute("market_recap", False, True)
    elif contextual_window_update and previous_market_route is not None:
        route = (QuestionRoute("market_trend", True, True)
                 if previous_market_route.intent == "market_trend" else previous_market_route)
    elif (contextual_market_update or default_overseas) and analysis.category == "unclear":
        route = QuestionRoute("general_web", False, False)
    else:
        route = _route_from_analysis(analysis)
    context_only_followup = (
        not pending_fresh and is_context_only_followup(text, previous_context)
    )
    if context_only_followup:
        route = QuestionRoute("general_knowledge", False, False)

    time_text = f"{text} {analysis.time_expression}".strip()
    window = resolve_market_window(time_text, now) if market_request else None
    financial_period = resolve_financial_period(time_text) if analysis.category == "financial_report" else None
    effective_question = text
    window_origin: Literal["explicit", "fuzzy_default", "default", "clarification", "none"] = (
        "fuzzy_default" if window and is_fuzzy_market_horizon(text)
        else "explicit" if window else "none"
    )
    consumed_version = None

    if default_a_share:
        if window is None and not analysis.time_expression:
            window = resolve_market_window("今日", now)
            window_origin = "default"
        if orphan_horizon:
            effective_question = f"A股大盘行情 {text}"
        else:
            explicitly_a_share = "a股" in text.casefold()
            effective_question = text if explicitly_a_share else f"{text} A股大盘行情"
            if window is not None and not any(term in text for term in ("今天", "今日")) and not has_market_period_expression(text):
                effective_question += " 今日"
    elif contextual_window_update and previous_context is not None:
        if previous_market_route and previous_market_route.intent == "market_recap":
            descriptor = "A股大盘行情"
        elif previous_market_route and previous_market_route.intent == "general_web":
            subject = next((term for term in ("港股", "香港股", "美股", "境外市场", "海外市场")
                            if term in previous_context.question), "海外市场")
            descriptor = f"{subject}市场行情"
        else:
            company = previous_context.companies[0]
            subject = f"{company.get('name') or company.get('code')} ({company.get('code')})"
            descriptor = f"{subject} 行情走势"
        effective_question = f"{descriptor} {text}"
    elif contextual_market_update and previous_context is not None:
        inherited_window = (
            _restore_window(previous_context.market_window)
            or resolve_market_window(previous_context.question, now)
        )
        window = window or inherited_window or resolve_market_window("今日", now)
        if window is not None and window_origin == "none":
            window_origin = "default" if inherited_window is None else "explicit"
        effective_question = f"分析{text}市场行情 {window.label if window else '今日'}"
    elif default_overseas:
        window = window or resolve_market_window("今日", now)
        if window is not None and window_origin == "none":
            window_origin = "default"
        effective_question = f"分析{text}市场行情 {window.label if window else '今日'}"
    elif market_request and window is None and not analysis.time_expression:
        window = resolve_market_window("今日", now)
        if window is not None:
            window_origin = "default"
            effective_question = f"{text} 今日"

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

    if analysis.category == "non_financial":
        return Clarification("当前仅支持金融类问题。", "kind", "non_financial", text)
    if analysis.category == "unclear" and consumed_version is None and not (
        default_a_share or default_overseas or contextual_window_update or contextual_market_update
    ):
        return Clarification(analysis.clarification_question, "kind", "clarification", text,
                             options=("财报经营指标", "证券价格走势"))
    if analysis.needs_clarification and consumed_version is None and not (
        default_a_share or default_overseas or contextual_window_update or contextual_market_update
    ):
        if analysis.subject_kind == "none" or (route.requires_company and not analysis.subject_name and not analysis.stock_code):
            missing_field: Literal["subject", "window", "kind"] = "subject"
        elif route.requires_time_window and window is None:
            missing_field = "window"
        else:
            missing_field = "kind"
        return Clarification(
            analysis.clarification_question, missing_field, route.intent, text,
            market_window=window, financial_period=financial_period,
            options=("财报经营指标", "证券价格走势") if missing_field == "kind" else (),
        )

    code = str(pending.get("company_code") or "") if consumed_version and isinstance(pending, Mapping) else ""
    if code and (len(code) != 6 or not code.isdigit()):
        code = ""
    if route.intent in {"market_quote", "market_trend", "market_recap"}:
        if (scope_request.mode == "whole_corpus" and route.intent in {"market_quote", "market_trend"}) or (
            route.intent == "market_recap" and scope_request.focus_report
        ):
            return Clarification("当前分析范围与行情请求冲突，请确认继续使用行情范围还是返回财报范围。",
                                 "kind", route.intent, effective_question,
                                 company_code=code or None, market_window=window,
                                 options=("继续使用行情范围", "返回财报范围"))

    if route.intent == "clarification" and consumed_version is None:
        clarification = _clarification_for(route, text, window, financial_period, None)
        return clarification or Clarification("请说明你希望分析财报经营指标，还是证券价格走势。",
                                               "kind", "market_trend", text,
                                               options=("财报经营指标", "证券价格走势"))
    clarification = _clarification_for(route, effective_question, window, financial_period, code or None)
    if clarification is not None:
        return clarification

    if route.intent == "market_trend" and window is not None and is_fuzzy_market_horizon(effective_question):
        window_origin = "fuzzy_default"
    if consumed_version is not None and window_origin == "explicit":
        window_origin = "clarification"
    return ResolvedRequest(
        route=route, effective_question=effective_question, market_window=window,
        financial_period=financial_period, window_origin=window_origin,
        allowed_source_kinds=_SOURCE_KINDS.get(route.intent, frozenset()),
        consumed_pending_version=consumed_version,
        context_only_followup=context_only_followup,
    )
