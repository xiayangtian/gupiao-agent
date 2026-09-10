"""财务结构可视化 v4 数据契约测试。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from financial_report_fetcher.evidence.models import (
    EntityScope,
    EvidenceRecord,
    SourceLocator,
    SourceType,
    VerificationState,
)
from financial_report_fetcher.visualizations import (
    VisualizationBundle,
    validate_visualization_payload,
)


@pytest.fixture
def pdf_record() -> EvidenceRecord:
    return EvidenceRecord(
        report_id="r1",
        entity_scope=EntityScope.CONSOLIDATED,
        fact_name="pdf_page_12",
        value=None,
        unit=None,
        currency=None,
        period="2026-06-30",
        source_type=SourceType.PDF_TEXT,
        source_locator=SourceLocator(provider="pdf", page=12, record_id="page-12"),
        extraction_confidence=0.99,
        verification_state=VerificationState.UNKNOWN_SCOPE,
        content_hash="page-12-content",
        parser_version="pdf-v1",
        text="合并现金流量表",
    )


def _cash_row(metric_id, value, evidence_id, **overrides):
    row = {
        "metric_id": metric_id,
        "label": metric_id,
        "value": value,
        "unit": "亿元",
        "direction": "inflow" if value > 0 else "outflow",
        "evidence_ids": [evidence_id],
    }
    row.update(overrides)
    return row


def _cash_payload(*rows):
    return {"cards": [{
        "id": "cash_flow_structure",
        "topic_id": "cash",
        "title": "现金流结构",
        "kind": "cash_flow",
        "rows": list(rows),
    }]}


def _validate(payload, pdf_record):
    return validate_visualization_payload(
        payload,
        period="2026-06-30",
        allowed_pdf_evidence={pdf_record.stable_id: pdf_record},
    )


def test_validator_marks_two_current_period_consolidated_cashflow_rows_partial(pdf_record):
    bundle = _validate(_cash_payload(
        _cash_row("operating_cash_flow", 12.5, pdf_record.stable_id),
        _cash_row("investing_cash_flow", -8.0, pdf_record.stable_id),
    ), pdf_record)

    card = bundle.cards[0]
    assert card.status == "partial"
    assert [row.metric_id for row in card.rows] == [
        "operating_cash_flow", "investing_cash_flow",
    ]


def test_validator_marks_all_cashflow_core_rows_complete(pdf_record):
    bundle = _validate(_cash_payload(
        _cash_row("operating_cash_flow", 12.5, pdf_record.stable_id),
        _cash_row("investing_cash_flow", -8.0, pdf_record.stable_id),
        _cash_row("financing_cash_flow", 3.0, pdf_record.stable_id),
    ), pdf_record)

    assert bundle.cards[0].status == "complete"


@pytest.mark.parametrize("row_overrides", [
    {"metric_id": "made_up_metric"},
    {"value": float("nan")},
    {"unit": "万元"},
    {"evidence_ids": ["missing"]},
])
def test_validator_rejects_invalid_rows_without_raising(pdf_record, row_overrides):
    row = _cash_row("operating_cash_flow", 1.0, pdf_record.stable_id)
    row.update(row_overrides)

    card = _validate(_cash_payload(row), pdf_record).cards[0]

    assert card.rows == ()
    assert card.status == "unavailable"
    assert card.unavailable_reason


def test_validator_rejects_duplicate_metrics(pdf_record):
    bundle = _validate(_cash_payload(
        _cash_row("operating_cash_flow", 1.0, pdf_record.stable_id),
        _cash_row("operating_cash_flow", 2.0, pdf_record.stable_id),
        _cash_row("investing_cash_flow", -3.0, pdf_record.stable_id),
    ), pdf_record)

    card = bundle.cards[0]
    assert card.status == "partial"
    assert [row.value for row in card.rows] == [1.0, -3.0]


@pytest.mark.parametrize("record", [
    pytest.param(lambda record: replace(record, source_type=SourceType.OCR_TEXT), id="not-pdf"),
    pytest.param(
        lambda record: replace(record, entity_scope=EntityScope.PARENT), id="parent-scope"
    ),
    pytest.param(lambda record: replace(record, period="2025-12-31"), id="wrong-period"),
    pytest.param(
        lambda record: replace(record, source_locator=SourceLocator(provider="pdf", record_id="no-page")),
        id="missing-page",
    ),
])
def test_validator_rejects_evidence_outside_pdf_current_consolidated_scope(pdf_record, record):
    invalid = record(pdf_record)
    bundle = validate_visualization_payload(
        _cash_payload(
            _cash_row("operating_cash_flow", 1.0, invalid.stable_id),
            _cash_row("investing_cash_flow", -1.0, invalid.stable_id),
        ),
        period="2026-06-30",
        allowed_pdf_evidence={invalid.stable_id: invalid},
    )

    assert bundle.cards[0].status == "unavailable"
    assert bundle.cards[0].rows == ()


def test_validator_requires_two_core_rows_to_draw(pdf_record):
    card = _validate(_cash_payload(
        _cash_row("operating_cash_flow", 1.0, pdf_record.stable_id),
    ), pdf_record).cards[0]

    assert card.status == "unavailable"
    assert "核心指标不足两个" in card.unavailable_reason


def test_validator_serializes_and_restores_immutable_contract(pdf_record):
    bundle = _validate(_cash_payload(
        _cash_row("operating_cash_flow", 1.5, pdf_record.stable_id),
        _cash_row("investing_cash_flow", -2.5, pdf_record.stable_id),
        _cash_row("financing_cash_flow", Decimal("3.5"), pdf_record.stable_id),
    ), pdf_record)

    restored = VisualizationBundle.from_dict(bundle.to_dict())

    assert restored == bundle
    assert restored.cards[0].rows[2].value == 3.5
    assert isinstance(restored.cards[0].rows, tuple)
