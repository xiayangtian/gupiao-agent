from dataclasses import replace
from decimal import Decimal

from financial_report_fetcher.evidence.models import (
    EntityScope,
    EvidenceRecord,
    SourceLocator,
    SourceType,
    VerificationState,
)
from financial_report_fetcher.insights import (
    InsightCandidate,
    InsightFinding,
    InsightPlanner,
    InsightScore,
    InsightScorer,
    InsightSection,
    filter_and_rank,
    sanitize_section,
)


def _candidate(candidate_id: str, *, materiality: int = 10) -> InsightCandidate:
    return InsightCandidate(
        candidate_id=candidate_id,
        title=candidate_id,
        summary="摘要",
        interest_tags=("现金流",),
        evidence_ids=("e1",),
        materiality_score=materiality,
        clarity_score=10,
    )


def _score(total: int, *, disqualifiers=()) -> InsightScore:
    return InsightScore(
        evidence_sufficiency=min(total, 30),
        source_reliability=min(max(total - 30, 0), 25),
        materiality=min(max(total - 55, 0), 20),
        clarity=min(max(total - 75, 0), 15),
        interest_relevance=min(max(total - 90, 0), 10),
        disqualifiers=tuple(disqualifiers),
    )


def _evidence(
    record_id: str,
    *,
    state: VerificationState = VerificationState.VERIFIED,
    scope: EntityScope = EntityScope.CONSOLIDATED,
    provider: str = "tushare",
) -> EvidenceRecord:
    return EvidenceRecord(
        report_id="r1",
        entity_scope=scope,
        fact_name="营业收入",
        value=Decimal("100"),
        unit="元",
        currency="CNY",
        period="2025",
        source_type=SourceType.STRUCTURED,
        source_locator=SourceLocator(provider=provider, record_id=record_id),
        extraction_confidence=0.95,
        verification_state=state,
        content_hash=record_id,
        parser_version="1",
    )


def test_score_bands_route_topics_and_disqualifier_prevents_detail():
    ranked = filter_and_rank(
        [
            (_candidate("detail"), _score(75)),
            (_candidate("observe"), _score(60)),
            (_candidate("hidden"), _score(59)),
            (_candidate("unsafe"), _score(100, disqualifiers=("重大冲突",))),
        ],
        detail_threshold=75,
        observation_threshold=60,
        limit=6,
    )

    assert [item.candidate_id for item in ranked.details] == ["detail"]
    assert [item.candidate_id for item in ranked.observations] == ["unsafe", "observe"]
    assert [item.candidate_id for item in ranked.filtered] == ["hidden"]


def test_ranking_is_stable_by_total_materiality_and_candidate_id():
    score = _score(75)
    ranked = filter_and_rank(
        [
            (_candidate("b", materiality=18), score),
            (_candidate("c", materiality=19), score),
            (_candidate("a", materiality=19), score),
        ],
        detail_threshold=75,
        observation_threshold=60,
        limit=6,
    )
    assert [item.candidate_id for item in ranked.details] == ["a", "c", "b"]


def test_scorer_uses_evidence_and_marks_conflict_and_mixed_scope():
    candidate = replace(_candidate("topic"), evidence_ids=("e1", "e2"))
    evidence = {
        "e1": _evidence("e1", state=VerificationState.CONFLICT),
        "e2": _evidence(
            "e2",
            state=VerificationState.VERIFIED,
            scope=EntityScope.PARENT,
            provider="pdf",
        ),
    }

    score = InsightScorer().score(candidate, evidence, interests=("现金流",))

    assert score.evidence_sufficiency == 20
    assert score.interest_relevance == 10
    assert "重大冲突" in score.disqualifiers
    assert "主体范围混用" in score.disqualifiers


def test_planner_rejects_unknown_evidence_ids_and_has_no_fixed_topics():
    planner = InsightPlanner(lambda records, interests: [
        {
            "candidate_id": "custom",
            "title": "客户集中度变化",
            "summary": "来自本次报告的动态主题",
            "interest_tags": ["经营风险"],
            "evidence_ids": ["e1", "missing"],
            "materiality_score": 17,
            "clarity_score": 12,
        }
    ])

    planned = planner.plan({"e1": _evidence("e1")}, interests=("经营风险",))

    assert [item.title for item in planned] == ["客户集中度变化"]
    assert planned[0].evidence_ids == ("e1",)


def test_sanitize_removes_undisclosed_rows_caps_density_and_highlights():
    valid = InsightFinding(
        claim="收入增长",
        significance="对盈利有重要影响",
        evidence_ids=("e1",),
        highlight_spans=("收入", "增长", "多余"),
        risk_state="warning",
    )
    missing = InsightFinding(
        claim="未披露",
        significance="无数据",
        evidence_ids=(),
        highlight_spans=(),
        has_disclosed_content=False,
    )
    section = InsightSection(
        section_id="s1",
        title="动态主题",
        summary="摘要",
        findings=(missing, valid, valid, valid, valid, valid, valid),
        score=_score(80),
        verification_state=VerificationState.VERIFIED,
    )

    clean = sanitize_section(section)

    assert len(clean.findings) == 5
    assert all(item.has_disclosed_content for item in clean.findings)
    assert all(len(item.highlight_spans) <= 2 for item in clean.findings)
    assert all(item.risk_state == "neutral" for item in clean.findings)
    assert clean.is_detailed_eligible


def test_section_with_fewer_than_three_findings_is_not_a_valid_detail_tab():
    section = InsightSection(
        section_id="s1",
        title="动态主题",
        summary="摘要",
        findings=(InsightFinding("有数据", "有意义", ("e1",), ()),),
        score=_score(80),
        verification_state=VerificationState.SINGLE_SOURCE,
    )
    assert not sanitize_section(section).is_detailed_eligible
