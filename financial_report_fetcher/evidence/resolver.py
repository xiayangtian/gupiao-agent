"""财报证据分组、交叉验证与冲突保留。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Mapping, Sequence

from .models import EntityScope, EvidenceRecord, VerificationState


EvidenceKey = tuple[str, str, EntityScope, str | None, str | None]


def values_match(a: Decimal, b: Decimal, relative_tolerance: Decimal) -> bool:
    """使用相对容差与 1 元绝对下限比较数值。"""
    if relative_tolerance < 0:
        raise ValueError("relative_tolerance 不能为负数")
    return abs(a - b) <= max(
        max(abs(a), abs(b)) * relative_tolerance,
        Decimal("1"),
    )


@dataclass(frozen=True)
class EvidenceConflict:
    key: EvidenceKey
    evidence_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ResolutionResult:
    records: tuple[EvidenceRecord, ...]
    groups: Mapping[EvidenceKey, tuple[EvidenceRecord, ...]]
    conflicts: tuple[EvidenceConflict, ...]
    warnings: tuple[str, ...]


def _key(record: EvidenceRecord) -> EvidenceKey:
    return (
        record.fact_name,
        record.period,
        record.entity_scope,
        record.unit,
        record.currency,
    )


def _source_identity(record: EvidenceRecord) -> str:
    return record.source_locator.provider or record.source_type.value


class EvidenceResolver:
    """在不自动裁决冲突的前提下更新证据验证状态。"""

    def __init__(self, relative_tolerance: Decimal = Decimal("0.001")):
        if relative_tolerance < 0:
            raise ValueError("relative_tolerance 不能为负数")
        self.relative_tolerance = relative_tolerance

    def resolve(self, records: Sequence[EvidenceRecord]) -> ResolutionResult:
        unique: dict[str, EvidenceRecord] = {}
        warnings: list[str] = []
        for record in records:
            if record.stable_id in unique:
                warnings.append(f"重复证据已合并: {record.stable_id}")
                continue
            unique[record.stable_id] = record

        grouped: dict[EvidenceKey, list[EvidenceRecord]] = {}
        for record in unique.values():
            grouped.setdefault(_key(record), []).append(record)

        resolved_groups: dict[EvidenceKey, tuple[EvidenceRecord, ...]] = {}
        conflicts: list[EvidenceConflict] = []
        for key, group in grouped.items():
            if key[2] is EntityScope.UNKNOWN:
                updated = tuple(
                    replace(record, verification_state=VerificationState.UNKNOWN_SCOPE)
                    for record in group
                )
                resolved_groups[key] = updated
                warnings.append(f"主体未知，禁止交叉验证: {key[0]} {key[1]}")
                continue

            sources = {_source_identity(record) for record in group}
            numeric = [record for record in group if record.value is not None]
            cross_source_pairs = [
                (left, right)
                for index, left in enumerate(numeric)
                for right in numeric[index + 1:]
                if _source_identity(left) != _source_identity(right)
            ]
            mismatches = [
                (left, right)
                for left, right in cross_source_pairs
                if not values_match(left.value, right.value, self.relative_tolerance)
            ]

            if mismatches:
                updated = tuple(
                    replace(record, verification_state=VerificationState.CONFLICT)
                    for record in group
                )
                conflicts.append(EvidenceConflict(
                    key=key,
                    evidence_ids=tuple(record.stable_id for record in group),
                    reason="独立来源数值超出允许容差，未自动选择任一值",
                ))
            elif len(sources) >= 2 and cross_source_pairs and len(numeric) == len(group):
                updated = tuple(
                    replace(record, verification_state=VerificationState.VERIFIED)
                    for record in group
                )
            else:
                updated = tuple(
                    replace(record, verification_state=VerificationState.SINGLE_SOURCE)
                    for record in group
                )
                if len(group) > 1 and len(sources) == 1:
                    warnings.append(f"缺少独立来源，不能交叉验证: {key[0]} {key[1]}")
            resolved_groups[key] = updated

        return ResolutionResult(
            records=tuple(
                record
                for group in resolved_groups.values()
                for record in group
            ),
            groups=resolved_groups,
            conflicts=tuple(conflicts),
            warnings=tuple(warnings),
        )
