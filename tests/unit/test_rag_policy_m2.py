import time

from financial_report_fetcher.rag.qa import RagQA, SUPPLEMENT_REQUEST_TOOL
from webapp.chat_models import Scope, SourcePolicy, ToolPolicy


class _Store:
    def query(self, *args, **kwargs):
        return [{"id": "1", "report_id": "601288:2026-06-30:semi_annual", "section": "x", "page": 1, "source": "pdf", "text": "营收"}]


class _AI:
    def __init__(self, calls): self.calls = calls
    def chat_stream(self, messages, *, system=None, tools=None):
        yield {"type": "tool_calls", "tool_calls": self.calls}
        yield {"type": "done", "answer": "不能确认", "model": "test", "usage": {}}


class _RecordingAI(_AI):
    """记录每轮实际收到的工具定义名，用于证明策略过滤没有放行未批准工具。"""

    def __init__(self, calls, seen_tools): super().__init__(calls); self.seen_tools = seen_tools
    def chat_stream(self, messages, *, system=None, tools=None):
        self.seen_tools.append([_tool_name(tool) for tool in (tools or [])])
        yield from super().chat_stream(messages, system=system, tools=tools)


def _tool_name(tool): return str((tool.get("function") or {}).get("name") or "")


def _scope(): return Scope.company_only("601288", "农业银行", ["601288:2026-06-30:semi_annual"])
def _policy(intent, names, timeout_seconds=30): return ToolPolicy(
    intent, tuple(names), 3, 2, timeout_seconds, SourcePolicy(local_pdf=True, market_data=True, web=True),
)
def _tool(name, arguments="{}"): return {"id": "1", "name": name, "arguments": arguments}


def test_report_fact_policy_rejects_model_tool_call():
    events = list(RagQA(_Store(), _AI([_tool("web_search")]), tool_executor=lambda *_: "{}").answer_stream("营收多少？", scope=_scope(), tool_policy=_policy("report_fact", ()), tools=[{"type": "function", "function": {"name": "web_search"}}]))
    assert not any(event["type"] == "tool_call" for event in events)
    assert any(event["type"] == "policy_resolved" for event in events)


def test_realtime_policy_rejects_unapproved_tool_name():
    executed = []
    events = list(RagQA(_Store(), _AI([_tool("unsafe")]), tool_executor=lambda *args: executed.append(args) or "{}").answer_stream("今天如何？", scope=_scope(), tool_policy=_policy("realtime_market", ("web_search",)), tools=[{"type": "function", "function": {"name": "web_search"}}]))
    assert any(event["type"] == "tool_result" and event["ok"] is False for event in events)
    assert executed == []


def test_structured_tool_result_is_emitted_for_json_object():
    events = list(RagQA(_Store(), _AI([_tool("web_search")]), tool_executor=lambda *_: '{"price": 10}').answer_stream("今天如何？", scope=_scope(), tool_policy=_policy("realtime_market", ("web_search",)), tools=[{"type": "function", "function": {"name": "web_search"}}]))
    assert any(event["type"] == "structured_tool_result" for event in events)
    assert events[-1]["tool_policy_intent"] == "realtime_market"


def test_scope_rejects_tool_call_with_another_company_code():
    executed = []
    events = list(RagQA(
        _Store(), _AI([{"id": "1", "name": "get_quote", "arguments": '{"symbol": "600900"}'}]),
        tool_executor=lambda *args: executed.append(args) or "{}",
    ).answer_stream(
        "今天如何？", scope=_scope(),
        tool_policy=_policy("realtime_market", ("get_quote",)),
        tools=[{"type": "function", "function": {"name": "get_quote"}}],
    ))

    assert executed == []
    assert any(
        event["type"] == "tool_result" and event["ok"] is False and "范围" in event["summary"]
        for event in events
    )


def test_scope_rejects_tool_call_with_another_company_name():
    """公司名称身份参数必须解析为代码后再校验范围，不能绕过 Scope。"""
    executed = []
    events = list(RagQA(
        _Store(), _AI([{"id": "1", "name": "get_quote", "arguments": '{"symbol": "长江电力"}'}]),
        tool_executor=lambda *args: executed.append(args) or "{}",
        company_code_resolver=lambda value: {"农业银行": "601288", "长江电力": "600900"}.get(value),
    ).answer_stream(
        "今天如何？", scope=_scope(),
        tool_policy=_policy("realtime_market", ("get_quote",)),
        tools=[{"type": "function", "function": {"name": "get_quote"}}],
    ))

    assert executed == []
    assert any(
        event["type"] == "tool_result" and event["ok"] is False and "范围" in event["summary"]
        for event in events
    )


def test_scope_accepts_tool_call_with_the_scoped_company_name():
    executed = []
    events = list(RagQA(
        _Store(), _AI([{"id": "1", "name": "get_quote", "arguments": '{"symbol": "农业银行"}'}]),
        tool_executor=lambda *args: executed.append(args) or "{}",
        company_code_resolver=lambda value: {"农业银行": "601288", "长江电力": "600900"}.get(value),
    ).answer_stream(
        "今天如何？", scope=_scope(),
        tool_policy=_policy("realtime_market", ("get_quote",)),
        tools=[{"type": "function", "function": {"name": "get_quote"}}],
    ))

    assert executed == [("get_quote", {"symbol": "农业银行"})]
    assert any(event["type"] == "tool_call" for event in events)


def test_scope_rejects_unresolvable_company_name_parameter():
    """无法解析为代码的身份参数必须受控失败，绝不交给执行器（fail-closed）。"""
    executed = []
    events = list(RagQA(
        _Store(), _AI([{"id": "1", "name": "get_quote", "arguments": '{"symbol": "不存在的公司"}'}]),
        tool_executor=lambda *args: executed.append(args) or "{}",
        company_code_resolver=lambda value: None,
    ).answer_stream(
        "今天如何？", scope=_scope(),
        tool_policy=_policy("realtime_market", ("get_quote",)),
        tools=[{"type": "function", "function": {"name": "get_quote"}}],
    ))

    assert executed == []
    assert any(event["type"] == "tool_result" and event["ok"] is False for event in events)


def test_supplement_tool_stays_defined_and_controlled_under_a_tool_policy():
    """带策略时受控补报工具仍在注入定义中，其他未批准工具仍被过滤。"""
    seen_tools = []
    executed = []
    accepted = []
    ai = _RecordingAI([_tool("request_missing_reports", '{"reason": "缺少原文", "needs": [{"period": "2025-06-30", "report_type": "semi_annual"}]}')], seen_tools)
    events = list(RagQA(
        _Store(), ai,
        tool_executor=lambda *args: executed.append(args) or "{}",
        supplement_request_handler=lambda payload: accepted.append(payload) or True,
    ).answer_stream(
        "2025 上半年经营现金流多少？", scope=_scope(),
        tool_policy=_policy("report_fact", ()),
        tools=[SUPPLEMENT_REQUEST_TOOL, {"type": "function", "function": {"name": "web_search"}}],
    ))

    assert seen_tools and "request_missing_reports" in seen_tools[0]
    assert "web_search" not in seen_tools[0]
    assert executed == []
    assert accepted and accepted[0]["needs"] == [{"period": "2025-06-30", "report_type": "semi_annual"}]
    assert any(event["type"] == "supplement_request" for event in events)


def test_supplement_tool_under_policy_still_rejects_unsupported_payloads():
    """受控工具本身不因豁免过滤而放宽：非法载荷只产生受控失败，不消耗工具额度。"""
    executed = []
    accepted = []
    events = list(RagQA(
        _Store(), _AI([_tool("request_missing_reports", '{"reason": "缺少原文", "needs": [{"period": "2025-06-30"}]}')]),
        tool_executor=lambda *args: executed.append(args) or "{}",
        supplement_request_handler=lambda payload: accepted.append(payload) or True,
    ).answer_stream(
        "2025 上半年经营现金流多少？", scope=_scope(),
        tool_policy=_policy("report_fact", ()),
        tools=[SUPPLEMENT_REQUEST_TOOL],
    ))

    assert accepted == []
    assert executed == []
    assert any(event["type"] == "tool_result" and event["ok"] is False for event in events)


def test_tool_call_timeout_is_bounded_by_policy_timeout_seconds():
    """每次工具调用耗时不得超过策略 timeout_seconds。"""
    def _slow_executor(name, arguments):
        time.sleep(2.0)
        return "{}"

    started = time.monotonic()
    events = list(RagQA(
        _Store(), _AI([_tool("get_quote")]), tool_executor=_slow_executor,
    ).answer_stream(
        "今天如何？", scope=_scope(),
        tool_policy=_policy("realtime_market", ("get_quote",), timeout_seconds=1),
        tools=[{"type": "function", "function": {"name": "get_quote"}}],
    ))
    elapsed = time.monotonic() - started

    assert elapsed < 1.5
    assert any(
        event["type"] == "tool_result" and event["ok"] is False and "超时" in event["summary"]
        for event in events
    )
    done = [event for event in events if event["type"] == "done"][0]
    assert done["tools_used"] == []
