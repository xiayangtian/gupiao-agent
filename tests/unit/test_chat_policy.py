from webapp.chat_models import Scope
from webapp.chat_policy import IntentRouter, ToolAvailability, ToolPolicyResolver


class _FakeSearch:
    """让 `_build_chat_tool_defs` 把真实网页搜索工具加进定义（不联网）。"""

    available = True

    def __init__(self, *args, **kwargs):
        pass


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


def _real_chat_tool_names(monkeypatch):
    """真实 provider 工具名：复用生产定义来源，避免白名单与实际工具名分叉。"""
    import webapp.server as server

    monkeypatch.setattr(server, "TavilyWebSearch", _FakeSearch)
    monkeypatch.setattr(server, "_mcp_tool_defs", lambda: [
        {"type": "function", "function": {"name": "get_realtime_quote", "parameters": {}}},
        {"type": "function", "function": {"name": "get_realtime_data", "parameters": {}}},
        {"type": "function", "function": {"name": "get_financial_metrics", "parameters": {}}},
        {"type": "function", "function": {"name": "get_balance_sheet", "parameters": {}}},
    ])
    cfg = type("C", (), {"mcp_tools": True, "web_search": True, "web_search_timeout": 15})()
    defs = server._build_chat_tool_defs(cfg) or []
    return [item["function"]["name"] for item in defs]


def test_realtime_and_event_policies_grant_real_provider_quote_tools(monkeypatch):
    """允许集必须来自真实工具名：实时/事件意图能拿到行情工具家族 + 网页搜索。"""
    names = _real_chat_tool_names(monkeypatch)
    assert "get_realtime_quote" in names and "get_realtime_data" in names
    availability = ToolAvailability.available(*names)

    for question, intent in (("农业银行今天涨跌如何？", "realtime_market"), ("农业银行异动原因？", "event_attribution")):
        decision = IntentRouter().classify(question, _scope())
        policy = ToolPolicyResolver().resolve(decision, _scope(), availability)
        assert decision.intent == intent
        assert set(policy.allowed_tools) == {"get_realtime_quote", "get_realtime_data", "web_search"}
        assert set(policy.allowed_tools) <= set(names)
        assert policy.source_policy.market_data is True
        assert policy.source_policy.web is True
        assert policy.max_calls == 3 and policy.max_rounds == 2


def test_policy_never_grants_tools_outside_the_allow_list(monkeypatch):
    """未列入的财务/报表工具即使可用也不得进入允许集。"""
    names = _real_chat_tool_names(monkeypatch)
    availability = ToolAvailability.available(*names)
    decision = IntentRouter().classify("农业银行今天涨跌如何？", _scope())

    policy = ToolPolicyResolver().resolve(decision, _scope(), availability)

    assert "get_financial_metrics" not in policy.allowed_tools
    assert "get_balance_sheet" not in policy.allowed_tools
    assert "request_missing_reports" not in policy.allowed_tools


def test_quote_family_follows_the_provider_names_not_one_literal(monkeypatch):
    """行情工具家族由 provider 名单决定，新增/改名行情工具后策略不失效。"""
    availability = ToolAvailability.available("get_realtime_quote_v2", "web_search")
    decision = IntentRouter().classify("农业银行今天涨跌如何？", _scope())

    policy = ToolPolicyResolver().resolve(decision, _scope(), availability)

    assert set(policy.allowed_tools) == {"get_realtime_quote_v2", "web_search"}


def test_realtime_policy_without_provider_quote_tools_falls_back_to_local_only():
    """只有网页搜索可用时，行情来源不得被标记为可用。"""
    decision = IntentRouter().classify("农业银行今天涨跌如何？", _scope())
    policy = ToolPolicyResolver().resolve(decision, _scope(), ToolAvailability.available("web_search"))

    assert policy.allowed_tools == ("web_search",)
    assert policy.source_policy.market_data is False
    assert policy.source_policy.web is True
    assert policy.fallback_message
