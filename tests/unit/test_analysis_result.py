import copy
import json

from financial_report_fetcher.analysis_ai import AiTopicGenerator
from financial_report_fetcher.analysis_result import AnalysisDocument
from financial_report_fetcher.evidence.models import VerificationState
from financial_report_fetcher.insights import (
    InsightPlanner,
    InsightScore,
    InsightSection,
)


_V3_DOCUMENT = {
    "schema_version": 3,
    "analysis_id": "analysis-1",
    "report_id": "600900:2025-12-31:annual",
    "interests": [],
    "stage": "completed",
    "quick": None,
    "sections": [],
    "observations": [],
    "filtered_topics": [],
    "evidence_catalog": {},
    "evidence_summary": {},
    "errors": [],
    "created_at": "2025-01-01T00:00:00+00:00",
    "updated_at": "2025-01-01T00:00:00+00:00",
    "meta": {},
}


def _v4_document():
    return {
        **_V3_DOCUMENT,
        "schema_version": 4,
        "visualizations": {
            "version": 1,
            "cards": [{
                "id": "cash_flow_structure",
                "topic_id": "dynamic-cash-topic",
                "title": "现金流结构",
                "kind": "cash_flow",
                "status": "partial",
                "unavailable_reason": None,
                "rows": [{
                    "metric_id": "operating_cash_flow",
                    "label": "经营活动现金流净额",
                    "value": 12.5,
                    "unit": "亿元",
                    "direction": "inflow",
                    "evidence_ids": ["pdf-12"],
                }, {
                    "metric_id": "investing_cash_flow",
                    "label": "投资活动现金流净额",
                    "value": -3.0,
                    "unit": "亿元",
                    "direction": "outflow",
                    "evidence_ids": ["pdf-12"],
                }],
            }],
        },
    }


def test_v3_document_loads_without_visualizations():
    document = AnalysisDocument.from_dict(_V3_DOCUMENT)

    assert document.schema_version == 3
    assert document.visualizations is None
    assert "visualizations" not in document.to_dict()


def test_v4_document_round_trips_visualizations():
    document = AnalysisDocument.from_dict(_v4_document())

    assert document.schema_version == 4
    assert document.to_dict()["visualizations"]["cards"][0]["id"] == "cash_flow_structure"


def test_section_tab_label_round_trips_and_v3_payload_defaults_to_empty():
    section = InsightSection(
        section_id="cash-flow",
        title="经营现金流显著改善",
        summary="经营活动现金流净额增加。",
        findings=(),
        score=InsightScore(0, 0, 0, 0, 0),
        verification_state=VerificationState.VERIFIED,
        tab_label="现金流",
    )
    document = AnalysisDocument.from_dict(_V3_DOCUMENT)
    document.sections = [section]

    serialized = document.to_dict()
    assert serialized["sections"][0]["tab_label"] == "现金流"
    assert AnalysisDocument.from_dict(serialized).sections[0].tab_label == "现金流"

    legacy = copy.deepcopy(serialized)
    legacy["sections"][0].pop("tab_label")
    assert AnalysisDocument.from_dict(legacy).sections[0].tab_label == ""


class _PromptCapturingAi:
    def ask(self, prompt, **kwargs):
        self.prompt = json.loads(prompt)
        self.system = kwargs["system"]
        return '{"candidates": []}'


def test_tab_label_planner_prompt_and_candidate_parsing_contract():
    ai = _PromptCapturingAi()
    AiTopicGenerator(ai)({}, interests=())

    assert "tab_label" in ai.system
    assert "最多 6 个汉字" in ai.system
    assert "编号与标点" in ai.system

    planner = InsightPlanner(lambda _evidence, _interests: [
        {
            "candidate_id": "cash-flow",
            "title": "经营现金流显著改善",
            "summary": "摘要",
            "interest_tags": [],
            "evidence_ids": ["e1"],
            "materiality_score": 18,
            "clarity_score": 12,
            "tab_label": " 现金流，2025!A ",
        },
        {
            "candidate_id": "missing-label",
            "title": "未提供短标签的主题",
            "summary": "摘要",
            "interest_tags": [],
            "evidence_ids": ["e1"],
            "materiality_score": 10,
            "clarity_score": 10,
        },
    ])

    candidates = planner.plan({"e1": object()}, interests=())

    assert [candidate.tab_label for candidate in candidates] == ["现金流202", ""]
