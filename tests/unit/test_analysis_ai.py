import json
from decimal import Decimal

import pytest

from financial_report_fetcher.analysis_ai import (
    AiInsightAnalyzer,
    AiQuickAnalyzer,
    AiTopicGenerator,
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


def _record(record_id, state=VerificationState.VERIFIED):
    return EvidenceRecord(
        report_id="r1",
        entity_scope=EntityScope.CONSOLIDATED,
        fact_name="revenue",
        value=Decimal("100"),
        unit="元",
        currency="CNY",
        period="2025-12-31",
        source_type=SourceType.STRUCTURED,
        source_locator=SourceLocator(provider="fake", record_id=record_id),
        extraction_confidence=0.9,
        verification_state=state,
        content_hash=record_id,
        parser_version="1",
    )


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
