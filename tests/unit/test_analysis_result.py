from financial_report_fetcher.analysis_result import AnalysisDocument


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
