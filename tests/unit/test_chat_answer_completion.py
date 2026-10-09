from webapp.chat_models import Fact, Scope
from webapp.source_runtime import AnswerContext, SourceResult
from financial_report_fetcher.rag.qa import RagQA


class SequenceAI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_stream(self, messages, *, system=None, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "system": system, "tools": tools, "kwargs": kwargs})
        yield from self.responses.pop(0)


def _delta(text):
    return {"type": "delta", "text": text, "reasoning": ""}


def _done(answer, finish_reason="stop"):
    return {"type": "done", "answer": answer, "finish_reason": finish_reason,
            "model": "fixture", "usage": {}}


def _market_context():
    facts = tuple(
        Fact(metric="price", value=value, unit="元/股", period=period, period_kind="daily",
             entity_scope="security", company_code="600660", source_type="tool",
             evidence_ids=(f"source-1:600660:{period}",), verification="reference",
             as_of="2026-10-09", source_category="market")
        for period, value in (("2026-09-28", 52.91), ("2026-09-29", 53.0),
                              ("2026-09-30", 53.58), ("2026-10-08", 51.65),
                              ("2026-10-09", 54.0))
    )
    source = SourceResult(call_id="source-1", provider="tencent", operation="kline",
                          category="market", status="success", as_of="2026-10-09",
                          facts=facts)
    return AnswerContext(sources=(source,))


def test_answer_from_context_continues_when_provider_reports_length_without_new_sources():
    ai = SequenceAI([
        [_delta("近期行情表格未完"), _done("近期行情表格未完", "length")],
        [_delta("，区间收涨。"), _done("，区间收涨。", "stop")],
    ])

    events = list(RagQA(object(), ai).answer_from_context(
        "分析福耀玻璃近期行情", context=AnswerContext(),
        scope=Scope.company_only("600660", "福耀玻璃"),
    ))

    done = events[-1]
    assert done["type"] == "done"
    assert "近期行情表格未完，区间收涨。" == done["answer"]
    assert len(ai.calls) == 2
    assert ai.calls[1]["tools"] is None
    assert ai.calls[1]["kwargs"]["max_tokens"] == 1024
    assert any("只能使用本轮已有证据" in message["content"] for message in ai.calls[1]["messages"])
    assert any("正在补全回答" in event.get("message", "") for event in events)


def test_continuation_does_not_duplicate_the_last_partial_phrase():
    repeated = "当前窗口的回答内容需要从最后一句继续补充："
    partial = "已写内容。" + repeated
    continuation = repeated + "给出完整结论。"
    ai = SequenceAI([
        [_delta(partial), _done(partial, "length")],
        [_delta(continuation), _done(continuation, "stop")],
    ])

    events = list(RagQA(object(), ai).answer_from_context(
        "补全这段内容", context=AnswerContext(),
    ))

    assert events[-1]["type"] == "done"
    assert events[-1]["answer"] == "已写内容。" + repeated + "给出完整结论。"


def test_answer_from_context_continues_on_open_markdown_table_even_if_provider_says_stop():
    partial = "| 日期 | 开盘 | 收盘 | 最高 | 最低 | 成交量 |\n|---|---:|---:|---:|---:|---:|\n| 2026-10-09 | 54.00 |"
    ai = SequenceAI([
        [_delta(partial), _done(partial, "stop")],
        [_delta("\n结论：近期收盘价上行。"), _done("\n结论：近期收盘价上行。", "stop")],
    ])

    events = list(RagQA(object(), ai).answer_from_context(
        "分析福耀玻璃近期行情", context=AnswerContext(),
    ))

    assert events[-1]["type"] == "done"
    assert "结论：近期收盘价上行。" in events[-1]["answer"]
    assert len(ai.calls) == 2


def test_answer_continues_after_complete_table_when_analysis_conclusion_is_missing():
    table = "| 日期 | 收盘价 |\n|---|---:|\n| 2026-10-09 | 54.00 |"
    ai = SequenceAI([
        [_delta(table), _done(table, "stop")],
        [_delta("\n结论：近期区间收涨。"), _done("\n结论：近期区间收涨。", "stop")],
    ])

    events = list(RagQA(object(), ai).answer_from_context(
        "分析福耀玻璃近期行情", context=AnswerContext(),
    ))

    assert events[-1]["type"] == "done"
    assert "结论：近期区间收涨。" in events[-1]["answer"]
    assert len(ai.calls) == 2


def test_exhausted_market_continuation_returns_complete_fact_based_summary():
    broken = "| 日期 | 收盘价 |\n|---|---:|\n| 2026-10-09 |"
    ai = SequenceAI([[_delta(broken), _done(broken, "length")] for _ in range(3)])

    events = list(RagQA(object(), ai).answer_from_context(
        "分析福耀玻璃近期行情", context=_market_context(),
        scope=Scope.company_only("600660", "福耀玻璃"),
    ))

    assert events[-1]["type"] == "done"
    assert "福耀玻璃" in events[-1]["answer"]
    assert "5个交易日" in events[-1]["answer"]
    assert "52.91" in events[-1]["answer"]
    assert "51.65" in events[-1]["answer"]
    assert "54元/股" in events[-1]["answer"]
    assert "区间收涨" in events[-1]["answer"]
    assert len(ai.calls) == 3
    assert events[-1]["completion_mode"] == "evidence_fallback"
    from webapp.chat_verifier import ClaimVerifier
    verification = ClaimVerifier().verify(
        events[-1]["answer"], Scope.company_only("600660", "福耀玻璃"),
        _market_context().sources[0].facts, [], [], answer_intent="market_trend",
    )
    assert verification.status == "passed"


def test_answer_stream_uses_same_completion_guard_for_retrieval_answers():
    class Store:
        def query(self, *_args, **_kwargs):
            return [{"id": "e1", "report_id": "600660:2026-06-30:semi_annual",
                     "source": "pdf", "section": "经营情况", "page": 4,
                     "text": "福耀玻璃本期营业收入为100亿元。"}]

    ai = SequenceAI([
        [_delta("福耀玻璃营业收入为"), _done("福耀玻璃营业收入为", "length")],
        [_delta("100亿元。结论：收入信息已完整。"), _done("100亿元。结论：收入信息已完整。", "stop")],
    ])

    events = list(RagQA(Store(), ai).answer_stream(
        "福耀玻璃半年报营收多少？", scope=Scope.company_only(
            "600660", "福耀玻璃", ("600660:2026-06-30:semi_annual",),
        ),
    ))

    assert events[-1]["type"] == "done"
    assert "100亿元。结论：收入信息已完整。" in events[-1]["answer"]
    assert len(ai.calls) == 2
    assert ai.calls[1]["tools"] is None


def test_incomplete_non_market_answer_is_never_reported_done_after_retry_limit():
    broken = "回答仍然没有结束，"
    ai = SequenceAI([[_delta(broken), _done(broken, "length")] for _ in range(3)])

    events = list(RagQA(object(), ai).answer_from_context(
        "解释该事实", context=AnswerContext(),
    ))

    assert events[-1]["type"] == "error"
    assert events[-1]["error"] == "answer_completion_exhausted"
    assert not any(event["type"] == "done" for event in events)
    assert len(ai.calls) == 3


def test_complete_answer_is_not_unnecessarily_continued():
    ai = SequenceAI([[_delta("基本结论：近期收盘价上行。"), _done("基本结论：近期收盘价上行。", "stop")]])

    events = list(RagQA(object(), ai).answer_from_context(
        "分析福耀玻璃近期行情", context=AnswerContext(),
    ))

    assert events[-1]["type"] == "done"
    assert len(ai.calls) == 1
