from __future__ import annotations

from webapp.chat_retrieval_window import select_evidence_window


def test_window_keeps_query_metric_and_following_value_after_300_chars():
    text = ("经营分析。" * 90) + "2025年营业收入为100亿元，同比增加5%，主要由于销量增长。"

    window = select_evidence_window(text, "2025年营业收入同比", max_chars=300)

    assert len(window) <= 300
    assert "100亿元" in window
    assert "同比增加5%" in window


def test_window_keeps_limitations_near_numeric_claim():
    text = ("无关披露。" * 100) + "扣除一次性收益后，利润为30亿元；该数据未经审计。"

    window = select_evidence_window(text, "扣非利润", max_chars=120)

    assert "扣除一次性收益后" in window
    assert "30亿元" in window
    assert "未经审计" in window


def test_window_falls_back_to_prefix_when_query_has_no_match():
    text = "现有披露说明。" * 100

    window = select_evidence_window(text, "不存在的专有指标", max_chars=80)

    assert window == text[:80]


def test_retrieval_diagnosis_distinguishes_missing_ranked_clipped_and_citation_failures():
    from webapp.chat_retrieval_window import diagnose_retrieval

    gold = "report#p40"
    assert diagnose_retrieval(gold, candidate_ids=(), ranked_ids=(), windowed_ids=(), cited_ids=()) == "not_recalled"
    assert diagnose_retrieval(gold, candidate_ids=(gold,), ranked_ids=(), windowed_ids=(), cited_ids=()) == "ranked_out"
    assert diagnose_retrieval(gold, candidate_ids=(gold,), ranked_ids=(gold,), windowed_ids=(), cited_ids=()) == "window_clipped"
    assert diagnose_retrieval(gold, candidate_ids=(gold,), ranked_ids=(gold,), windowed_ids=(gold,), cited_ids=()) == "citation_mismatch"
    assert diagnose_retrieval(gold, candidate_ids=(gold,), ranked_ids=(gold,), windowed_ids=(gold,), cited_ids=(gold,)) == "supported"


def test_runtime_fixture_really_places_gold_fact_after_300_characters():
    from webapp.chat_runtime_eval_runner import load_corpus

    document = load_corpus()[0]

    assert len(document["text"]) > 300
    assert document["text"].index("100亿元") > 300


def test_window_rejects_invalid_budget():
    try:
        select_evidence_window("披露", "披露", max_chars=0)
    except ValueError as exc:
        assert "max_chars" in str(exc)
    else:
        raise AssertionError("non-positive window budget must be rejected")


def test_ragqa_candidate_uses_query_window_for_prompt_and_citation():
    from financial_report_fetcher.rag.qa import RagQA

    text = ("经营分析。" * 90) + "2025年营业收入为100亿元，同比增加5%。"
    evidence = {"id": "e1", "report_id": "601288:2025-12-31:annual", "source": "pdf",
                "section": "经营情况", "page": 88, "text": text}

    class Store:
        def query(self, *_args, **_kwargs):
            return [dict(evidence)]

    class AI:
        system = ""
        messages = ()

        def chat_stream(self, messages, *, system=None, **_kwargs):
            self.messages = messages
            self.system = system
            yield {"type": "done", "answer": "据[1]，营业收入为100亿元。", "usage": {}}

    ai = AI()
    events = list(RagQA(Store(), ai, context_window_strategy="relevance").answer_stream(
        "2025年营业收入同比是多少？",
    ))
    done = next(event for event in events if event["type"] == "done")

    projected_context = "\n".join(message["content"] for message in ai.messages)
    assert "100亿元" in projected_context and "同比增加5%" in projected_context
    assert "100亿元" in done["citations"][0]["snippet"]
    assert done["citations"][0]["page"] == 88
