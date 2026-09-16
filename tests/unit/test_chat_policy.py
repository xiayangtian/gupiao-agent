from webapp.chat_models import Scope
from webapp.chat_policy import IntentRouter, ToolAvailability, ToolPolicyResolver


def _scope():
    return Scope.company_only("601288", "农业银行", ["601288:2026-06-30:semi_annual"])


def test_report_fact_uses_local_pdf_and_disallows_external_tools():
    decision = IntentRouter().classify("农业银行半年报营收是多少？", _scope())
    policy = ToolPolicyResolver().resolve(decision, _scope(), ToolAvailability.available("web_search", "get_quote"))
    assert decision.intent == "report_fact"
    assert policy.allowed_tools == ()


def test_realtime_market_allows_only_available_tools_with_bounded_budget():
    decision = IntentRouter().classify("农业银行今天为什么下跌？", _scope())
    policy = ToolPolicyResolver().resolve(decision, _scope(), ToolAvailability.available("web_search", "get_quote"))
    assert decision.intent == "realtime_market"
    assert policy.max_calls <= 3
    assert "web_search" in policy.allowed_tools
    assert policy.max_rounds == 2


def test_invalid_model_intent_falls_back_to_local_report_fact():
    from webapp.chat_models import IntentDecision
    decision = IntentDecision.from_dict({"intent": "arbitrary"})
    assert decision.intent == "report_fact"
    assert decision.confidence == "low"


def test_event_signals_follow_realtime_priority_and_unavailable_tools_are_removed():
    decision = IntentRouter().classify("近期新闻有什么原因？", _scope())
    policy = ToolPolicyResolver(timeout_seconds=99).resolve(decision, _scope(), ToolAvailability())
    assert decision.intent == "realtime_market"
    assert policy.allowed_tools == ()
    assert policy.timeout_seconds == 30
