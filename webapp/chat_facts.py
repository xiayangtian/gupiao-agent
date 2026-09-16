"""Fail-closed normalization and conflict detection for structured chat facts."""
from __future__ import annotations

from collections import defaultdict
from math import isfinite
from typing import Any, Sequence

from webapp.chat_models import EvidenceArtifact, Fact, FactConflict, Scope, ToolArtifact

_ACCEPTED_UNITS = frozenset(("元", "万元", "亿元", "百分比", "倍", "股", "元/股"))
_MONEY_SCALES = {"元": 1e-8, "万元": 1e-4, "亿元": 1.0}


class FactNormalizer:
    def normalize(self, raw: dict[str, Any], artifact: EvidenceArtifact | ToolArtifact, scope: Scope) -> Fact | None:
        """Convert a provider-controlled object to a comparable fact, otherwise reject it."""
        if not isinstance(raw, dict) or isinstance(artifact, EvidenceArtifact) and artifact.source == "web":
            return None
        value = raw.get("value")
        unit = raw.get("unit")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or unit not in _ACCEPTED_UNITS:
            return None
        required = ("metric", "period", "period_kind", "entity_scope", "company_code", "evidence_ids")
        if any(not raw.get(name) for name in required):
            return None
        evidence_ids = raw["evidence_ids"]
        if not isinstance(evidence_ids, (tuple, list)) or not all(isinstance(item, str) and item for item in evidence_ids):
            return None
        company_code = raw["company_code"]
        if scope.mode != "whole_corpus" and company_code not in {company.code for company in scope.companies}:
            return None
        original_value = float(value)
        original_unit = unit
        normalized_value = original_value * _MONEY_SCALES[unit] if unit in _MONEY_SCALES else original_value
        normalized_unit = "亿元" if unit in _MONEY_SCALES else unit
        if isinstance(artifact, EvidenceArtifact):
            if artifact.source != "pdf":
                return None
            source_type, verification, as_of = "pdf", "verified", ""
        else:
            if not raw.get("provider") or not raw.get("as_of") or raw.get("as_of") != artifact.as_of:
                return None
            source_type, verification, as_of = "tool", "reference", artifact.as_of
        try:
            return Fact(metric=str(raw["metric"]), value=normalized_value, unit=normalized_unit,
                        period=str(raw["period"]), period_kind=str(raw["period_kind"]),
                        entity_scope=str(raw["entity_scope"]), company_code=str(company_code),
                        source_type=source_type, evidence_ids=tuple(evidence_ids), verification=verification,
                        as_of=as_of, original_value=original_value, original_unit=original_unit)
        except ValueError:
            return None

    @staticmethod
    def comparable(left: Fact, right: Fact) -> bool:
        return (left.company_code, left.period, left.period_kind, left.entity_scope, left.unit) == (
            right.company_code, right.period, right.period_kind, right.entity_scope, right.unit
        )


def detect_conflicts(facts: Sequence[Fact]) -> tuple[FactConflict, ...]:
    groups: dict[tuple[str, str, str, str, str], list[Fact]] = defaultdict(list)
    for fact in facts:
        groups[(fact.company_code, fact.metric, fact.period, fact.period_kind, fact.entity_scope)].append(fact)
    conflicts = []
    for (_, metric, _, _, _), group in groups.items():
        if len(group) > 1 and len({fact.value for fact in group}) > 1:
            conflicts.append(FactConflict(metric=metric, facts=tuple(group), reason="同指标同期间同口径数值不一致"))
    return tuple(conflicts)
