from financial_report_fetcher.rag.qa import RagQA
from webapp.chat_models import Scope
from webapp.source_runtime import AnswerContext, CallBudget, SourceResult, SourceRuntime


class NoStore:
    def query(self, *args, **kwargs):
        raise AssertionError("retrieval repeated")


class AI:
    def __init__(self):
        self.calls = []

    def chat_stream(self, **kwargs):
        self.calls.append(kwargs)
        assert not kwargs.get("tools")
        assert "provider_payload_marker" not in kwargs.get("system", "")
        assert all("provider_payload_marker" not in message.get("content", "")
                   for message in kwargs["messages"] if message["role"] == "system")
        yield {"type": "done", "answer": "已取得外部参考。", "model": "fixture", "usage": {}}


def test_context_answer_does_not_retrieve_or_execute_tools():
    ai = AI()
    def forbidden(*args):
        raise AssertionError("external call repeated")
    qa = RagQA(NoStore(), ai, tool_executor=forbidden)
    context = AnswerContext(sources=(SourceResult("s1", "fixture", "quote", "market", "success",
        content="provider_payload_marker", as_of="2026-09-24"),))
    events = list(qa.answer_from_context("行情", context=context))
    assert events[-1]["type"] == "done"
    assert "provider_payload_marker" in ai.calls[0]["messages"][-2]["content"]


def test_required_missing_uses_financial_fallback_without_company_facts():
    ai = AI()
    qa = RagQA(NoStore(), ai)
    events = list(qa.answer_from_context(
        "分析农业银行年报", context=AnswerContext(required_missing=True),
    ))
    assert events[-1]["type"] == "done"
    assert len(ai.calls) == 1
    assert "本地必需报告证据缺失" in ai.calls[0]["system"]
    assert "不得回答具体公司事实" in ai.calls[0]["system"]
    assert "一般知识的分析框架" in ai.calls[0]["system"]


def test_compatibility_tool_loop_uses_injected_run_runtime():
    class ToolAI:
        def __init__(self):
            self.calls = 0
        def chat_stream(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "tool_calls", "tool_calls": [
                    {"id": "c1", "name": "fixture_quote", "arguments": "{}"}]}
            else:
                yield {"type": "done", "answer": "有限行情数据", "model": "fixture", "usage": {}}

    invoked = []
    qa = RagQA(NoStore(), ToolAI(), tool_executor=lambda name, args: (
        invoked.append(name) or '{"as_of":"2026-09-24","data":[{"price":1}]}'
    ))
    runtime = SourceRuntime(Scope.whole_corpus(), CallBudget(1, 1, 1), lambda call: True)
    events = list(qa.answer_stream("行情", tools=[{"type":"function", "function":{"name":"fixture_quote"}}],
                                   scope=Scope.whole_corpus(), source_runtime=runtime))
    assert invoked == ["fixture_quote"]
    assert runtime.budget.snapshot() == {"total": 1, "market": 1, "web": 0}
    assert any(event["type"] == "source_result" and event["result"].status == "success" for event in events)
