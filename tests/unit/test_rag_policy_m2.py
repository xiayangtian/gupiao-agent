from financial_report_fetcher.rag.qa import RagQA
from webapp.chat_models import Scope, SourcePolicy, ToolPolicy


class _Store:
    def query(self, *args, **kwargs):
        return [{"id": "1", "report_id": "601288:2026-06-30:semi_annual", "section": "x", "page": 1, "source": "pdf", "text": "营收"}]


class _AI:
    def __init__(self, calls): self.calls = calls
    def chat_stream(self, messages, *, system=None, tools=None):
        yield {"type": "tool_calls", "tool_calls": self.calls}
        yield {"type": "done", "answer": "不能确认", "model": "test", "usage": {}}


def _scope(): return Scope.company_only("601288", "农业银行", ["601288:2026-06-30:semi_annual"])
def _policy(intent, names): return ToolPolicy(intent, tuple(names), 3, 2, 30, SourcePolicy(local_pdf=True, market_data=True, web=True))
def _tool(name): return {"id": "1", "name": name, "arguments": "{}"}


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
