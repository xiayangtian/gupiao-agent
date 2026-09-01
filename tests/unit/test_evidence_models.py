"""统一财报证据契约的序列化与身份测试。"""

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


def _record(**overrides) -> EvidenceRecord:
    values = {
        "report_id": "600900:2025-12-31:annual",
        "entity_scope": EntityScope.CONSOLIDATED,
        "fact_name": "revenue",
        "value": Decimal("123456789.01"),
        "unit": "yuan",
        "currency": "CNY",
        "period": "2025-12-31",
        "source_type": SourceType.STRUCTURED,
        "source_locator": SourceLocator(
            provider="akshare",
            section="income_statement",
            record_id="income:2025-12-31",
        ),
        "extraction_confidence": 0.99,
        "verification_state": VerificationState.SINGLE_SOURCE,
        "content_hash": "abc123",
        "parser_version": "akshare-v1",
        "text": None,
        "adjustment_state": "reported",
        "source_timestamp": "2026-09-01T12:00:00+08:00",
        "raw_field_name": "营业总收入",
    }
    values.update(overrides)
    return EvidenceRecord(**values)


def test_evidence_record_round_trips_decimal_enums_and_locator():
    """JSON 往返必须保留金额精度、枚举和来源定位。"""
    record = _record(
        source_locator=SourceLocator(
            provider="pdf",
            page=88,
            section="合并利润表",
            bbox=(0.1, 0.2, 0.8, 0.9),
            record_id="table-3-row-5",
        )
    )

    payload = record.to_dict()
    restored = EvidenceRecord.from_dict(payload)

    assert payload["value"] == "123456789.01"
    assert payload["source_locator"]["bbox"] == [0.1, 0.2, 0.8, 0.9]
    assert restored == record
    assert isinstance(restored.value, Decimal)


def test_stable_id_ignores_verification_and_fetch_time_but_tracks_source_content():
    """重新校验或重新抓取不应换 ID，原始内容变化则必须换 ID。"""
    record = _record()
    reverified = replace(
        record,
        verification_state=VerificationState.VERIFIED,
        source_timestamp="2026-09-02T12:00:00+08:00",
    )
    changed = replace(record, value=Decimal("123456789.02"))

    assert reverified.stable_id == record.stable_id
    assert changed.stable_id != record.stable_id


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_evidence_record_rejects_confidence_outside_unit_interval(confidence):
    """非法置信度不能进入后续评分。"""
    with pytest.raises(ValueError, match="extraction_confidence"):
        _record(extraction_confidence=confidence)
