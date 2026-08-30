"""财务事实校验器的契约测试。"""

from decimal import Decimal
from dataclasses import fields

import pytest

from financial_report_fetcher.facts import Evidence, FinancialFact, validate_metric_rows


def test_validate_metric_rows_drops_invalid_values_and_records_warning():
    """无效年份/值不得进入 facts 或兼容 metrics，但必须有校验信息。"""
    result = validate_metric_rows(
        [
            {
                "year": 2025,
                "revenue": 100.0,
                "net_profit": 20.0,
                "roe": 12.5,
                "gross_margin": 35.0,
                "debt_ratio": 45.0,
            },
            {"year": 2026, "revenue": 999.0},
            {"year": 2024, "revenue": -1.0, "roe": float("inf"), "debt_ratio": -2.0},
        ],
        report_year=2025,
    )

    assert result.metrics == [
        {
            "year": 2025,
            "revenue": 100.0,
            "net_profit": 20.0,
            "roe": 12.5,
            "gross_margin": 35.0,
            "debt_ratio": 45.0,
        }
    ]
    assert result.validation.status == "warning"
    assert result.validation.messages
    assert all(fact.unit in ("亿元", "%") for fact in result.facts)
    assert all(fact.evidence is None for fact in result.facts)


def test_duplicate_year_metric_keeps_last_valid_value_and_warns():
    """后续有效重复指标覆盖此前有效值，且调用方可获知该数据冲突。"""
    result = validate_metric_rows(
        [
            {"year": 2025, "revenue": 100.0, "roe": 12.0},
            {"year": 2025, "revenue": 120.0, "net_profit": 8.0},
        ],
        report_year=2025,
    )

    assert result.metrics == [
        {
            "year": 2025,
            "revenue": 120.0,
            "net_profit": 8.0,
            "roe": 12.0,
            "gross_margin": None,
            "debt_ratio": None,
        }
    ]
    assert [(fact.metric, fact.value) for fact in result.facts] == [
        ("revenue", 120.0),
        ("net_profit", 8.0),
        ("roe", 12.0),
    ]
    assert result.validation.status == "warning"
    assert any("重复" in message.message for message in result.validation.messages)


def test_validate_metric_rows_fails_when_no_valid_fact_exists():
    """全部拒绝时状态必须为 failed，不能悄悄返回空成功结果。"""
    result = validate_metric_rows(
        [{"year": 2026, "revenue": 1.0}, {"year": 2025, "revenue": -1.0}],
        report_year=2025,
    )

    assert result.metrics is None
    assert result.facts == []
    assert result.validation.status == "failed"
    assert result.validation.messages


def test_validate_metric_rows_rejects_boolean_metric_values():
    """布尔值是类型错误，不能被 float() 悄悄转换为 1 或 0。"""
    result = validate_metric_rows(
        [{"year": 2025, "revenue": True, "net_profit": "12.5"}],
        report_year=2025,
    )

    assert result.metrics == [{
        "year": 2025,
        "revenue": None,
        "net_profit": 12.5,
        "roe": None,
        "gross_margin": None,
        "debt_ratio": None,
    }]
    assert [(fact.metric, fact.value) for fact in result.facts] == [("net_profit", 12.5)]
    assert any(message.code == "invalid_number" and message.metric == "revenue"
               for message in result.validation.messages)


def test_validate_metric_rows_rejects_overflowing_value_and_year_without_raising():
    """超大整数值和溢出年份都应转成校验错误，而不是中断分析。"""
    result = validate_metric_rows(
        [
            {"year": 2025, "revenue": 10**400},
            {"year": Decimal("Infinity"), "revenue": 1.0},
        ],
        report_year=2025,
    )

    assert result.metrics is None
    assert result.facts == []
    assert result.validation.status == "failed"
    assert {message.code for message in result.validation.messages} >= {
        "invalid_number",
        "invalid_year",
    }


def test_financial_fact_serializes_public_validation_and_evidence_fields():
    """公开事实模型完整携带证据和事实自身的校验状态。"""
    fact = FinancialFact(
        metric="revenue",
        value=100.0,
        unit="亿元",
        period="2025",
        evidence=Evidence(3, "营业收入为100亿元", "财报正文"),
        validation_status="passed",
        validation_messages=[],
    )

    assert fact.to_dict() == {
        "metric": "revenue",
        "value": 100.0,
        "unit": "亿元",
        "period": "2025",
        "evidence": {"page": 3, "quote": "营业收入为100亿元", "source": "财报正文"},
        "validation_status": "passed",
        "validation_messages": [],
    }
    assert [field.name for field in fields(Evidence)] == ["page", "quote", "source"]
    assert list(fact.to_dict()["evidence"]) == ["page", "quote", "source"]


def test_evidence_requires_non_empty_string_source():
    """证据对象必须显式携带有意义的来源；无证据只能表示为 None。"""
    with pytest.raises(TypeError):
        Evidence(None, None)

    for invalid_source in (None, "", "   ", 123):
        with pytest.raises(ValueError):
            Evidence(None, None, invalid_source)
