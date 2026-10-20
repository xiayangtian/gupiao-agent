from datetime import datetime
from zoneinfo import ZoneInfo

from webapp.chat_intent import ChatIntentAnalysis
from webapp.chat_request_resolution import Clarification, ResolvedRequest, resolve_chat_request
from webapp.chat_scope import ScopeRequest

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _analysis(category, subject_kind="none", subject_name="", task_kind="none", time_expression="",
              *, stock_code="", needs_clarification=False, clarification_question=""):
    return ChatIntentAnalysis(
        category=category, subject_kind=subject_kind, subject_name=subject_name,
        stock_code=stock_code, task_kind=task_kind, time_expression=time_expression,
        needs_clarification=needs_clarification, clarification_question=clarification_question,
    )


def test_model_analysis_overrides_case_sensitive_keyword_route_for_a_share():
    result = resolve_chat_request(
        "分析今日a股行情", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("market_analysis", "broad_market", "A股", "recap", "今日"),
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_recap"
    assert result.route.requires_company is False


def test_generic_market_request_defaults_to_a_share_and_today():
    result = resolve_chat_request(
        "分析行情", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("market_analysis", "none", "", "recap"),
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_recap"
    assert result.market_window.label == "今日"
    assert result.window_origin == "default"
    assert "A股" in result.effective_question


def test_market_intent_without_subject_defaults_to_broad_a_share_unless_single_stock_requested():
    result = resolve_chat_request(
        "今天表现如何", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("market_analysis", "none", "", "trend", "今日"),
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_recap"
    assert result.market_window.label == "今日"
    assert "A股" in result.effective_question

    single_stock = resolve_chat_request(
        "个股今日走势", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("market_analysis", "none", "", "trend", "今日"),
    )
    assert isinstance(single_stock, ResolvedRequest)
    assert single_stock.route.intent == "market_trend"
    assert single_stock.route.requires_company is True


def test_orphan_seven_days_defaults_to_a_share_market_recap():
    result = resolve_chat_request(
        "7天", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("unclear", needs_clarification=True,
                           clarification_question="请补充分析对象。"),
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_recap"
    assert result.market_window.label == "近7自然日"
    assert result.market_window.start_date.isoformat() == "2026-10-03"
    assert result.market_window.end_date.isoformat() == "2026-10-09"
    assert result.window_origin == "explicit"
    assert "A股" in result.effective_question


def test_completed_broad_market_context_allows_short_horizon_correction():
    from webapp.chat_followups import PreviousTurnContext

    previous = PreviousTurnContext(
        question="分析A股今日行情", answer="A股今日复盘。", run_id="r-market",
        status="completed", source_mode="market_recap", steps=("market_indices",),
        companies=(), facts=(), tools=(),
    )
    result = resolve_chat_request(
        "7天", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("unclear", needs_clarification=True,
                           clarification_question="请补充分析对象。"),
        previous_context=previous,
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_recap"
    assert result.market_window.label == "近7自然日"
    assert "A股" in result.effective_question


def test_short_market_followup_retains_resolved_company_and_original_intent():
    from webapp.chat_followups import PreviousTurnContext

    previous = PreviousTurnContext(
        question="海尔智家", answer="海尔智家今日行情。", run_id="r-haier",
        status="completed", source_mode="external_market", steps=("market_quote",),
        companies=({"code": "600690", "name": "海尔智家"},), facts=(), tools=(),
        market_window={"kind": "explicit", "label": "今日", "start_date": "2026-10-09",
                       "end_date": "2026-10-09", "trading_days": None,
                       "timezone": "Asia/Shanghai"},
    )
    result = resolve_chat_request(
        "今日行情", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("unclear", needs_clarification=True,
                           clarification_question="请说明具体分析对象。"),
        previous_context=previous,
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_quote"
    assert result.market_window.label == "今日"
    assert "海尔智家 (600690)" in result.effective_question
    assert result.consumed_pending_version is None


def test_followup_window_keeps_a_previous_overseas_market():
    from webapp.chat_followups import PreviousTurnContext

    previous = PreviousTurnContext(
        question="港股", answer="港股行情来源有限。", run_id="r-hk",
        status="partial", source_mode="general_web", steps=("web_search",),
        companies=(), facts=(), tools=(),
        market_window={"kind": "explicit", "label": "今日", "start_date": "2026-10-09",
                       "end_date": "2026-10-09", "trading_days": None,
                       "timezone": "Asia/Shanghai"},
    )
    result = resolve_chat_request(
        "7天", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("unclear", needs_clarification=True,
                           clarification_question="请说明分析对象。"),
        previous_context=previous,
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "general_web"
    assert result.market_window.label == "近7自然日"
    assert "港股" in result.effective_question
    assert "A股" not in result.effective_question
    assert result.allowed_source_kinds == frozenset({"web_search"})


def test_explicit_market_change_inherits_previous_window_not_previous_market():
    from webapp.chat_followups import PreviousTurnContext

    previous = PreviousTurnContext(
        question="分析近期行情", answer="A股近7自然日复盘。", run_id="r-market",
        status="completed", source_mode="market_recap", steps=("market_indices",),
        companies=(), facts=(), tools=(),
        market_window={"kind": "explicit", "label": "近7自然日", "start_date": "2026-10-03",
                       "end_date": "2026-10-09", "trading_days": None,
                       "timezone": "Asia/Shanghai"},
    )
    result = resolve_chat_request(
        "港股", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("market_analysis", "overseas_market", "港股", "recap"),
        previous_context=previous,
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "general_web"
    assert result.market_window.label == "近7自然日"
    assert "港股" in result.effective_question
    assert "A股" not in result.effective_question


def test_explicit_overseas_market_defaults_only_the_missing_window():
    result = resolve_chat_request(
        "港股行情", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("market_analysis", "overseas_market", "港股", "recap"),
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "general_web"
    assert result.market_window.label == "今日"
    assert "港股" in result.effective_question
    assert "A股" not in result.effective_question


def test_orphan_overseas_market_defaults_to_today_without_rewriting_market():
    result = resolve_chat_request(
        "港股", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("unclear", needs_clarification=True,
                           clarification_question="请说明分析哪方面。"),
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "general_web"
    assert result.market_window.label == "今日"
    assert "港股" in result.effective_question
    assert "A股" not in result.effective_question
    assert result.allowed_source_kinds == frozenset({"web_search"})


def test_pending_subject_reply_overrides_unclear_intent_without_losing_original_slots():
    pending = {
        "version": "pending-haier", "created_at": NOW.isoformat(),
        "missing_field": "subject", "intent": "market_quote", "company_code": None,
        "market_window": {"kind": "explicit", "label": "今日", "start_date": "2026-10-09",
                          "end_date": "2026-10-09", "trading_days": None,
                          "timezone": "Asia/Shanghai"},
        "financial_period": None, "question": "再分析下海尔的今日行情",
    }
    result = resolve_chat_request(
        "海尔智家", ScopeRequest.auto(), pending, NOW,
        analysis=_analysis("unclear", needs_clarification=True,
                           clarification_question="您想了解海尔智家的哪方面内容？"),
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_quote"
    assert result.market_window.label == "今日"
    assert result.consumed_pending_version == "pending-haier"
    assert "再分析下海尔的今日行情" in result.effective_question
    assert "海尔智家" in result.effective_question


def test_market_assets_without_stock_identity_are_not_blocked_by_company_scope():
    for kind, name in (("commodity", "黄金"), ("overseas_market", "美股"), ("industry", "半导体")):
        result = resolve_chat_request(
            f"分析{name}走势", ScopeRequest.auto(), None, NOW,
            analysis=_analysis("market_analysis", kind, name, "trend", "近期"),
        )
        assert isinstance(result, ResolvedRequest)
        assert result.route.requires_company is False
        assert result.route.intent == "general_web"


def test_model_can_classify_a_phrase_differently_without_resolver_reclassification():
    result = resolve_chat_request(
        "分析今日A股行情", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("financial_knowledge", task_kind="explain"),
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "general_knowledge"
    assert result.allowed_source_kinds == frozenset()


def test_broad_market_request_does_not_require_a_company():
    result = resolve_chat_request("分析今日股票行情", ScopeRequest.auto(), None, NOW,
                                  analysis=_analysis("market_analysis", "broad_market", "A股", "recap", "今日"))

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_recap"
    assert result.route.requires_company is False
    assert result.market_window.label == "今日"
    assert result.allowed_source_kinds == frozenset({"market_indices", "market_overview", "web_search"})

    recent = resolve_chat_request("分析A股近5个交易日整体表现", ScopeRequest.auto(), None, NOW,
                                  analysis=_analysis("market_analysis", "broad_market", "A股", "recap", "近5个交易日"))
    assert isinstance(recent, ResolvedRequest)
    assert recent.route.intent == "market_recap"
    assert recent.route.requires_company is False
    assert recent.market_window.trading_days == 5


def test_company_market_request_without_period_defaults_to_today():
    result = resolve_chat_request(
        "分析福耀玻璃股价走势", ScopeRequest.auto(), None, NOW,
        analysis=_analysis("market_analysis", "equity", "福耀玻璃", "trend"),
    )

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_trend"
    assert result.market_window.label == "今日"
    assert result.window_origin == "default"
    assert result.effective_question.endswith("今日")


def test_company_recent_market_request_defaults_to_five_trading_days():
    result = resolve_chat_request("分析福耀玻璃近期行情", ScopeRequest.auto(), None, NOW,
                                  analysis=_analysis("market_analysis", "equity", "福耀玻璃", "trend", "近期"))

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_trend"
    assert result.market_window.trading_days == 5
    assert result.window_origin == "fuzzy_default"
    assert "market_kline" in result.allowed_source_kinds
    assert "retrieve" not in result.allowed_source_kinds


def test_orphan_market_horizon_defaults_to_a_share_recap_without_clarification():
    result = resolve_chat_request("近五交易日", ScopeRequest.auto(), None, NOW,
                                  analysis=_analysis("market_analysis", "none", "", "trend", "近五交易日",
                                                     needs_clarification=True,
                                                     clarification_question="请说明分析对象。"))

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_recap"
    assert result.market_window.trading_days == 5
    assert "A股" in result.effective_question


def test_window_reply_consumes_only_a_matching_pending_market_slot():
    pending = {
        "version": "pending-v1", "created_at": NOW.isoformat(),
        "missing_field": "window", "intent": "market_trend", "company_code": "600660",
        "market_window": None, "financial_period": None,
        "question": "福耀玻璃股价走势",
    }
    result = resolve_chat_request("近五交易日", ScopeRequest.auto(), pending, NOW,
                                  analysis=_analysis("market_analysis", "none", "", "trend", "近五交易日"))

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
    result = resolve_chat_request("福耀玻璃", ScopeRequest.auto(), pending, NOW,
                                  analysis=_analysis("market_analysis", "equity", "福耀玻璃", "trend"))

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
    result = resolve_chat_request("分析今日大盘", ScopeRequest.auto(), pending, NOW,
                                  analysis=_analysis("market_analysis", "broad_market", "大盘", "recap", "今日"))

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "market_recap"
    assert result.effective_question == "分析今日大盘"
    assert result.consumed_pending_version is None


def test_financial_trend_keeps_fiscal_period_contract():
    result = resolve_chat_request("农业银行近三年营收趋势", ScopeRequest.auto(), None, NOW,
                                  analysis=_analysis("financial_report", "equity", "农业银行", "report_trend", "近三年"))

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "financial_trend"
    assert result.financial_period == "近三年"
    assert result.market_window is None
    assert result.allowed_source_kinds == frozenset({"retrieve"})


def test_contextual_display_followup_is_answer_only_but_market_causation_is_not():
    from webapp.chat_followups import PreviousTurnContext

    previous = PreviousTurnContext(
        question="分析长江电力近五日走势", answer="长江电力行情。", run_id="r1",
        status="completed", source_mode="external_market", steps=("market_kline",),
        companies=({"code": "600900", "name": "长江电力"},), facts=(), tools=(),
    )
    display = resolve_chat_request("这个为什么没画图？", ScopeRequest.auto(), None, NOW,
                                  analysis=_analysis("financial_knowledge", task_kind="explain"),
                                  previous_context=previous)
    assert isinstance(display, ResolvedRequest)
    assert display.context_only_followup is True
    assert display.route.intent == "general_knowledge"
    assert display.allowed_source_kinds == frozenset()

    cause = resolve_chat_request("长江电力股价为什么涨了？", ScopeRequest.auto(), None, NOW,
                                 analysis=_analysis("market_analysis", "equity", "长江电力", "attribution"),
                                 previous_context=previous)
    assert isinstance(cause, ResolvedRequest)
    assert cause.context_only_followup is False


def test_market_request_conflicting_with_whole_corpus_scope_clarifies():
    result = resolve_chat_request("分析福耀玻璃近期行情", ScopeRequest.whole_corpus(), None, NOW,
                                  analysis=_analysis("market_analysis", "equity", "福耀玻璃", "trend", "近期"))

    assert isinstance(result, Clarification)
    assert result.missing_field == "kind"
    assert "范围" in result.message
