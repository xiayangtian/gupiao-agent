import json
from decimal import Decimal

import pytest

from financial_report_fetcher.analysis_ai import (
    TEXT_EVIDENCE_LIMIT,
    AiInsightAnalyzer,
    AiQuickAnalyzer,
    AiStructureVisualizer,
    AiTopicGenerator,
    _evidence_payload,
    _parse_json,
)
from financial_report_fetcher.evidence.models import (
    EntityScope,
    EvidenceRecord,
    SourceLocator,
    SourceType,
    VerificationState,
)
from financial_report_fetcher.insights import InsightCandidate, InsightScorer


class FakeAi:
    def __init__(self, payload):
        self.payload = payload
        self.last_kwargs = None

    def ask(self, *args, **kwargs):
        self.last_prompt = args[0]
        self.last_kwargs = kwargs
        return json.dumps(self.payload, ensure_ascii=False)


class SequenceAi:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def ask(self, *args, **kwargs):
        self.calls += 1
        return next(self.responses)


def _record(
    record_id,
    state=VerificationState.VERIFIED,
    *,
    source_type=SourceType.STRUCTURED,
    page=None,
    text=None,
    period="2025-12-31",
    entity_scope=EntityScope.CONSOLIDATED,
):
    return EvidenceRecord(
        report_id="r1",
        entity_scope=entity_scope,
        fact_name="revenue",
        value=Decimal("100"),
        unit="元",
        currency="CNY",
        period=period,
        source_type=source_type,
        source_locator=SourceLocator(
            provider="fake", page=page, record_id=record_id
        ),
        extraction_confidence=0.9,
        verification_state=state,
        content_hash=record_id,
        parser_version="1",
        text=text,
    )


def test_evidence_payload_keeps_structured_and_samples_oversized_text_records():
    """证据载荷：结构化全保留；文本记录超限时抽样子集并截断，避免输出被截断。"""
    structured = [_record(f"s{i}") for i in range(8)]
    text_records = []
    for index in range(120):
        text = "资产负债表：公司经营情况良好。" if index == 3 else f"第 {index} 页叙述内容。"
        text_records.append(_record(
            f"p{index}",
            source_type=SourceType.PDF_TEXT,
            page=index + 1,
            text=text + "字" * 300,
        ))

    payload = _evidence_payload([*text_records, *structured])
    text_items = [item for item in payload if item["source_type"] == "pdf_text"]
    structured_items = [item for item in payload if item["source_type"] == "structured"]

    assert len(structured_items) == 8, "结构化证据必须全部保留"
    assert len(text_items) <= TEXT_EVIDENCE_LIMIT
    assert all(len(item["text"]) <= 240 for item in text_items)
    # 含报表关键字的页应保留在抽样内，不会被长文本输入淹没。
    assert any(item["text"].startswith("资产负债表：") for item in text_items)


def test_parse_json_recovers_truncated_output_prefix():
    """解析对截断与尾随文字有回退；完全无法恢复时仍抛出解析错误。"""
    assert _parse_json('{"a": 1, "b": "完整"}') == {"a": 1, "b": "完整"}
    # 完整对象后带模型解释文字：raw_decode 应能取回对象。
    assert _parse_json('{"a": 1} 以上是根据财报得出的结论') == {"a": 1}

    with pytest.raises(json.JSONDecodeError):
        _parse_json('{"a": ')


def test_topic_generator_caps_candidates_and_sends_bounded_evidence():
    """主题规划器应限制候选数量、上调输出预算，并发送封顶后的证据。"""
    records = {
        **{f"s{i}": _record(f"s{i}") for i in range(5)},
        **{
            f"p{i}": _record(
                f"p{i}",
                source_type=SourceType.PDF_TEXT,
                page=i + 1,
                text="经营情况叙述。" + "字" * 200,
            )
            for i in range(60)
        },
    }
    ai = FakeAi({"candidates": []})

    AiTopicGenerator(ai)(records, interests=("cash_flow",))

    prompt = json.loads(ai.last_prompt)
    assert prompt["max_candidates"] == 8
    text_sent = [e for e in prompt["evidence"] if e["source_type"] == "pdf_text"]
    structured_sent = [e for e in prompt["evidence"] if e["source_type"] == "structured"]
    assert len(structured_sent) == 5
    assert len(text_sent) <= TEXT_EVIDENCE_LIMIT
    assert ai.last_kwargs["max_tokens"] >= 3000



def test_quick_analyzer_drops_unknown_and_conflicting_evidence_claims():
    verified = _record("verified")
    conflict = _record("conflict", VerificationState.CONFLICT)
    ai = FakeAi({"conclusions": [
        {"conclusion_id": "ok", "claim": "收入为 100", "key_data": "100 元",
         "significance": "重要", "evidence_ids": [verified.stable_id]},
        {"conclusion_id": "bad", "claim": "冲突值", "key_data": "120 元",
         "significance": "不应输出", "evidence_ids": [conflict.stable_id]},
        {"conclusion_id": "made-up", "claim": "臆测", "key_data": "999 元",
         "significance": "不应输出", "evidence_ids": ["missing"]},
    ]})

    result = AiQuickAnalyzer(ai).analyze([verified, conflict], interests=("cash_flow",))

    assert [item.conclusion_id for item in result.conclusions] == ["ok"]
    assert result.conclusions[0].verification_state is VerificationState.VERIFIED
    assert ai.last_kwargs["thinking"] == {"type": "disabled"}
    assert "json_object" in ai.last_prompt


def test_quick_analyzer_retries_empty_json_response_once():
    ai = SequenceAi(["", '{"conclusions": []}'])

    result = AiQuickAnalyzer(ai).analyze([], interests=())

    assert result.conclusions == []
    assert ai.calls == 2


def test_quick_analyzer_reports_clear_error_after_two_invalid_responses():
    ai = SequenceAi(["", "not json"])

    with pytest.raises(ValueError, match="连续两次未返回有效 JSON"):
        AiQuickAnalyzer(ai).analyze([], interests=())

    assert ai.calls == 2


def test_topic_generator_returns_only_candidates_with_known_evidence():
    record = _record("e1")
    generator = AiTopicGenerator(FakeAi({"candidates": [
        {"candidate_id": "cash", "title": "现金质量", "summary": "摘要",
         "interest_tags": ["cash_flow"], "evidence_ids": [record.stable_id],
         "materiality_score": 18, "clarity_score": 12},
        {"candidate_id": "fake", "title": "虚构", "summary": "摘要",
         "interest_tags": [], "evidence_ids": ["missing"],
         "materiality_score": 20, "clarity_score": 15},
    ]}))

    candidates = generator({record.stable_id: record}, ("cash_flow",))

    assert [item["candidate_id"] for item in candidates] == ["cash"]


def test_structure_visualizer_uses_only_pdf_records_and_validates_evidence():
    pdf_record = _record(
        "pdf", source_type=SourceType.PDF_TEXT, page=12, text="现金流量表披露。"
    )
    web_record = _record("web", source_type=SourceType.STRUCTURED, text="网页结构化数据。")
    ai = FakeAi({"cards": [{
        "id": "cash_flow_structure", "topic_id": "cash", "kind": "cash_flow",
        "rows": [
            {"metric_id": "operating_cash_flow", "label": "经营活动现金流净额", "value": 12,
             "unit": "亿元", "direction": "inflow", "evidence_ids": [pdf_record.stable_id]},
            {"metric_id": "investing_cash_flow", "label": "投资活动现金流净额", "value": -3,
             "unit": "亿元", "direction": "outflow", "evidence_ids": [pdf_record.stable_id]},
        ],
    }]})

    result = AiStructureVisualizer(ai).analyze(
        [pdf_record, web_record], "2025-12-31", {"cash_flow_structure": "cash"}
    )

    assert result.cards[0].status == "partial"
    assert result.cards[0].rows[0].evidence_ids == (pdf_record.stable_id,)
    assert web_record.stable_id not in ai.last_prompt
    prompt = json.loads(ai.last_prompt)
    assert prompt["evidence"] == [{
        "evidence_id": pdf_record.stable_id,
        "page": 12,
        "period": "2025-12-31",
        "text": "现金流量表披露。",
    }]


def test_structure_visualizer_excludes_wrong_scope_period_and_unlocatable_pdf_records():
    valid = _record("valid", source_type=SourceType.PDF_TEXT, page=8, text="有效页")
    invalid_records = [
        _record("parent", source_type=SourceType.PDF_TEXT, page=9, text="母公司页", entity_scope=EntityScope.PARENT),
        _record("prior", source_type=SourceType.PDF_TEXT, page=10, text="上期页", period="2024-12-31"),
        _record("unlocated", source_type=SourceType.PDF_TEXT, page=None, text="无页码"),
    ]
    ai = FakeAi({"cards": []})

    AiStructureVisualizer(ai).analyze(
        [valid, *invalid_records], "2025-12-31", {"cash_flow_structure": "cash"}
    )

    prompt = json.loads(ai.last_prompt)
    assert [item["evidence_id"] for item in prompt["evidence"]] == [valid.stable_id]


@pytest.mark.parametrize("payload, topic_ids", [
    ({"cards": [{
        "id": "cash_flow_structure", "topic_id": "cash", "kind": "cash_flow",
        "rows": [{"metric_id": "operating_cash_flow", "label": "经营活动现金流净额",
                  "value": 12, "unit": "亿元", "direction": "inflow",
                  "evidence_ids": ["missing"]}],
    }]}, {"cash_flow_structure": "cash"}),
    ({"cards": "not-an-array"}, {"cash_flow_structure": "cash"}),
    ({"cards": []}, {}),
])
def test_structure_visualizer_safely_degrades_invalid_evidence_or_missing_topic(payload, topic_ids):
    pdf_record = _record("pdf", source_type=SourceType.PDF_TEXT, page=12, text="现金流量表披露。")
    result = AiStructureVisualizer(FakeAi(payload)).analyze(
        [pdf_record], "2025-12-31", topic_ids
    )
    assert not result.cards or result.cards[0].status == "unavailable"


def test_structure_visualizer_safely_degrades_invalid_json():
    pdf_record = _record("pdf", source_type=SourceType.PDF_TEXT, page=12, text="现金流量表披露。")
    result = AiStructureVisualizer(SequenceAi(["not json", "still not json"])).analyze(
        [pdf_record], "2025-12-31", {"cash_flow_structure": "cash"}
    )
    assert result.cards == ()


def test_insight_analyzer_limits_highlight_and_risk_to_verified_evidence():
    record = _record("e1", VerificationState.SINGLE_SOURCE)
    candidate = InsightCandidate(
        "cash", "现金质量", "摘要", ("cash_flow",), (record.stable_id,), 18, 12
    )
    ai = FakeAi({"findings": [{
        "claim": "现金改善", "significance": "偿债能力增强",
        "evidence_ids": [record.stable_id],
        "highlight_spans": ["现金", "改善", "多余"],
        "risk_state": "verified_risk",
    }] * 3})

    section = AiInsightAnalyzer(ai, InsightScorer()).analyze(
        candidate, [record], interests=("cash_flow",)
    )

    assert section.verification_state is VerificationState.SINGLE_SOURCE
    assert all(item.risk_state == "neutral" for item in section.findings)
    assert all(len(item.highlight_spans) == 2 for item in section.findings)
