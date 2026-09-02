"""统一证据去重、交叉验证和冲突测试。"""

from dataclasses import replace
from decimal import Decimal

from financial_report_fetcher.evidence.models import (
    EntityScope,
    EvidenceRecord,
    SourceLocator,
    SourceType,
    VerificationState,
)
from financial_report_fetcher.evidence.resolver import EvidenceResolver, values_match


def _record(
    value: str,
    *,
    provider: str = "akshare",
    scope: EntityScope = EntityScope.CONSOLIDATED,
    fact_name: str = "revenue",
) -> EvidenceRecord:
    return EvidenceRecord(
        report_id="600900:2025-12-31:annual",
        entity_scope=scope,
        fact_name=fact_name,
        value=Decimal(value),
        unit="yuan",
        currency="CNY",
        period="2025-12-31",
        source_type=SourceType.STRUCTURED,
        source_locator=SourceLocator(provider=provider, record_id=f"{provider}:{fact_name}"),
        extraction_confidence=0.95,
        verification_state=VerificationState.SINGLE_SOURCE,
        content_hash=f"{provider}-{value}-{scope.value}",
        parser_version=f"{provider}-v1",
    )


def test_matching_independent_sources_become_verified():
    """容差内的独立来源才能把同主体事实升级为 verified。"""
    result = EvidenceResolver(relative_tolerance=Decimal("0.001")).resolve([
        _record("1000000", provider="akshare"),
        _record("1000500", provider="tushare"),
    ])

    assert {record.verification_state for record in result.records} == {
        VerificationState.VERIFIED
    }
    assert result.conflicts == ()


def test_duplicate_records_from_one_provider_do_not_self_verify():
    """同一提供方的重复数据不能伪装成交叉验证。"""
    first = _record("100", provider="akshare")
    duplicate = replace(first, content_hash="another-row")

    result = EvidenceResolver().resolve([first, duplicate])

    assert {record.verification_state for record in result.records} == {
        VerificationState.SINGLE_SOURCE
    }
    assert any("独立来源" in warning for warning in result.warnings)


def test_parent_and_consolidated_are_never_merged():
    """母公司与合并报表即使数值相同也必须属于不同组。"""
    result = EvidenceResolver().resolve([
        _record("100", scope=EntityScope.PARENT),
        _record("100", scope=EntityScope.CONSOLIDATED),
    ])

    assert len(result.groups) == 2
    assert all(len(records) == 1 for records in result.groups.values())


def test_material_difference_between_sources_is_conflict_without_auto_selection():
    """超出容差的来源差异必须保留双方并全部标记冲突。"""
    first = _record("100", provider="akshare")
    second = _record("120", provider="tushare")

    result = EvidenceResolver().resolve([first, second])

    assert len(result.conflicts) == 1
    assert set(result.conflicts[0].evidence_ids) == {first.stable_id, second.stable_id}
    assert [record.value for record in result.records] == [Decimal("100"), Decimal("120")]
    assert all(
        record.verification_state is VerificationState.CONFLICT
        for record in result.records
    )


def test_unknown_scope_never_participates_in_cross_validation():
    """主体未知的数据即使跨来源一致，也只能保持 unknown_scope。"""
    result = EvidenceResolver().resolve([
        _record("100", provider="akshare", scope=EntityScope.UNKNOWN),
        _record("100", provider="tushare", scope=EntityScope.UNKNOWN),
    ])

    assert all(
        record.verification_state is VerificationState.UNKNOWN_SCOPE
        for record in result.records
    )


def test_absolute_floor_handles_small_values():
    """小额数值使用 1 元绝对容差，大额数值使用相对容差。"""
    assert values_match(Decimal("1"), Decimal("2"), Decimal("0.001")) is True
    assert values_match(Decimal("1"), Decimal("2.01"), Decimal("0.001")) is False
