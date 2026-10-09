from datetime import datetime
from zoneinfo import ZoneInfo

from webapp.chat_request_resolution import Clarification, ResolvedRequest, resolve_chat_request
from webapp.chat_scope import ScopeRequest

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_broad_market_request_does_not_require_a_company():
    result = resolve_chat_request("分析今日股票行情", ScopeRequest.auto(), None, NOW)

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_recap"
    assert result.route.requires_company is False
    assert result.market_window.label == "今日"
    assert result.allowed_source_kinds == frozenset({"market_indices", "market_overview", "web_search"})


def test_company_recent_market_request_defaults_to_five_trading_days():
    result = resolve_chat_request("分析福耀玻璃近期行情", ScopeRequest.auto(), None, NOW)

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_trend"
    assert result.market_window.trading_days == 5
    assert result.window_origin == "fuzzy_default"
    assert "market_kline" in result.allowed_source_kinds
    assert "retrieve" not in result.allowed_source_kinds


def test_orphan_window_clarifies_subject_without_creating_report_intent():
    result = resolve_chat_request("近五交易日", ScopeRequest.auto(), None, NOW)

    assert isinstance(result, Clarification)
    assert result.missing_field == "subject"
    assert result.intent == "market_trend"
    assert result.market_window.trading_days == 5


def test_window_reply_consumes_only_a_matching_pending_market_slot():
    pending = {
        "version": "pending-v1", "created_at": NOW.isoformat(),
        "missing_field": "window", "intent": "market_trend", "company_code": "600660",
        "market_window": None, "financial_period": None,
        "question": "福耀玻璃股价走势",
    }
    result = resolve_chat_request("近五交易日", ScopeRequest.auto(), pending, NOW)

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_trend"
    assert result.market_window.trading_days == 5
    assert "福耀玻璃股价走势" in result.effective_question
    assert "近五交易日" in result.effective_question
    assert result.consumed_pending_version == "pending-v1"


def test_subject_reply_keeps_the_frozen_market_window():
    pending = {
        "version": "pending-v2", "created_at": NOW.isoformat(),
        "missing_field": "subject", "intent": "market_trend", "company_code": None,
        "market_window": {"kind": "rolling_trading_days", "label": "近五交易日",
                          "start_date": None, "end_date": "2026-10-09", "trading_days": 5,
                          "timezone": "Asia/Shanghai"},
        "financial_period": None, "question": "分析近五交易日股价走势",
    }
    result = resolve_chat_request("福耀玻璃", ScopeRequest.auto(), pending, NOW)

    assert isinstance(result, ResolvedRequest)
    assert result.market_window.trading_days == 5
    assert result.window_origin == "clarification"
    assert result.effective_question == "分析近五交易日股价走势 福耀玻璃"


def test_new_broad_market_topic_does_not_inherit_pending_company():
    pending = {
        "version": "pending-v3", "created_at": NOW.isoformat(),
        "missing_field": "window", "intent": "market_trend", "company_code": "600660",
        "market_window": None, "financial_period": None,
        "question": "福耀玻璃股价走势",
    }
    result = resolve_chat_request("分析今日大盘", ScopeRequest.auto(), pending, NOW)

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_recap"
    assert result.effective_question == "分析今日大盘"
    assert result.consumed_pending_version is None


def test_financial_trend_keeps_fiscal_period_contract():
    result = resolve_chat_request("农业银行近三年营收趋势", ScopeRequest.auto(), None, NOW)

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "financial_trend"
    assert result.financial_period == "近三年"
    assert result.market_window is None
    assert result.allowed_source_kinds == frozenset({"retrieve"})


def test_market_request_conflicting_with_whole_corpus_scope_clarifies():
    result = resolve_chat_request("分析福耀玻璃近期行情", ScopeRequest.whole_corpus(), None, NOW)

    assert isinstance(result, Clarification)
    assert result.missing_field == "kind"
    assert "范围" in result.message
