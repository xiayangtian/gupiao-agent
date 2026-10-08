"""Fail-closed normalization, provenance, and deterministic derivation of chat facts."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from math import isfinite
import re
from typing import Any, Sequence

from webapp.chat_models import EvidenceArtifact, Fact, FactConflict, Scope, ToolArtifact
from webapp.evidence_identity import artifact_evidence_ids
from webapp.source_runtime import SourceResult

_ACCEPTED_UNITS = frozenset(("元", "万元", "亿元", "百分比", "%", "％", "百分点", "点", "指数点", "家", "倍", "股", "元/股"))
_MONEY_SCALES = {"元": 1e-8, "万元": 1e-4, "亿元": 1.0}
_PERCENT_UNITS = frozenset(("百分比", "%", "％"))


class FactNormalizer:
    def normalize(self, raw: dict[str, Any], artifact: EvidenceArtifact | ToolArtifact, scope: Scope) -> Fact | None:
        """Convert a provider-controlled object to a comparable fact, otherwise reject it."""
        if not isinstance(raw, dict) or isinstance(artifact, EvidenceArtifact) and artifact.source == "web":
            return None
        if not isinstance(artifact, (EvidenceArtifact, ToolArtifact)):
            return None
        value = raw.get("value")
        unit = raw.get("unit")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not isfinite(value) or not isinstance(unit, str) or unit not in _ACCEPTED_UNITS):
            return None
        required = ("metric", "period", "period_kind", "entity_scope", "company_code", "evidence_ids")
        if any(not isinstance(raw.get(name), str) or not raw[name].strip() for name in required[:-1]):
            return None
        if not raw.get("evidence_ids"):
            return None
        evidence_ids = raw["evidence_ids"]
        if not isinstance(evidence_ids, (tuple, list)) or not all(isinstance(item, str) and item for item in evidence_ids):
            return None
        company_code = raw["company_code"]
        if scope.mode != "whole_corpus" and company_code not in {company.code for company in scope.companies}:
            return None
        original_value = float(value)
        original_unit = unit
        if unit in _MONEY_SCALES:
            normalized_value, normalized_unit = original_value * _MONEY_SCALES[unit], "亿元"
        elif unit in _PERCENT_UNITS:
            normalized_value, normalized_unit = original_value, "百分比"
        elif unit == "指数点":
            normalized_value, normalized_unit = original_value, "点"
        else:
            normalized_value, normalized_unit = original_value, unit
        if isinstance(artifact, EvidenceArtifact):
            if artifact.source != "pdf" or artifact.availability != "available":
                return None
            report_parts = artifact.report_id.split(":")
            if (len(report_parts) < 3 or report_parts[0] != str(company_code)
                    or report_parts[1] != str(raw["period"])):
                return None
            if scope.mode != "whole_corpus" and artifact.report_id not in scope.report_ids:
                return None
            report_type = report_parts[2].casefold()
            expected_period_kinds = {
                "annual": {"annual", "annual_cumulative"},
                "semi_annual": {"semi_annual_cumulative"},
                "quarterly": {"quarterly_cumulative"},
            }.get(report_type)
            if expected_period_kinds is None or str(raw["period_kind"]) not in expected_period_kinds:
                return None
            controlled_evidence_ids = artifact_evidence_ids(artifact)
            if not controlled_evidence_ids:
                return None
            source_type, source_category, verification, as_of = "pdf", "local_pdf", "verified", ""
        else:
            if artifact.status not in {"success", "partial"}:
                return None
            provider, data_as_of = raw.get("provider"), raw.get("as_of")
            if not provider or not data_as_of or data_as_of != artifact.as_of or provider != artifact.provider:
                return None
            source_type, verification, as_of = "tool", "reference", artifact.as_of
            source_category = _source_category(artifact.provider)
            controlled_evidence_ids = (f"tool:{artifact.provider}:{artifact.tool_name}",)
        try:
            return Fact(metric=str(raw["metric"]), value=normalized_value, unit=normalized_unit,
                        period=str(raw["period"]), period_kind=str(raw["period_kind"]),
                        entity_scope=str(raw["entity_scope"]), company_code=str(company_code),
                        source_type=source_type, evidence_ids=controlled_evidence_ids, verification=verification,
                        as_of=as_of, original_value=original_value, original_unit=original_unit,
                        source_category=source_category)
        except ValueError:
            return None

    @staticmethod
    def comparable(left: Fact, right: Fact) -> bool:
        return (left.company_code, left.period, left.period_kind, left.entity_scope, left.unit) == (
            right.company_code, right.period, right.period_kind, right.entity_scope, right.unit
        )

    def facts_from_pdf_evidence(self, artifact: EvidenceArtifact, scope: Scope) -> tuple[Fact, ...]:
        """Extract only literal metric-number pairs from an in-scope, paginated PDF snippet."""
        if (not isinstance(artifact, EvidenceArtifact) or artifact.source != "pdf"
                or artifact.availability != "available" or not artifact.page or not artifact.snippet):
            return ()
        parts = artifact.report_id.split(":")
        if len(parts) < 3 or not re.fullmatch(r"\d{6}", parts[0]) or not _is_iso_date(parts[1]):
            return ()
        code, period, report_type = parts[0], parts[1], parts[2].casefold()
        if scope.mode != "whole_corpus":
            if code not in {company.code for company in scope.companies} or artifact.report_id not in scope.report_ids:
                return ()
        period_kind = {
            "annual": "annual", "annual_report": "annual", "yearly": "annual",
            "semi_annual": "semi_annual_cumulative", "half_year": "semi_annual_cumulative",
            "quarterly": "quarterly_cumulative",
        }.get(report_type)
        if period_kind is None:
            return ()
        evidence_ids = artifact_evidence_ids(artifact)
        if not evidence_ids:
            return ()
        unit_pattern = r"(亿元|万元|元/股|百分点|百分比|%|％|倍|股|元)"
        number_pattern = re.compile(rf"(?<![\d.])(-?(?:\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.\d+)?)\s*{unit_pattern}")
        metric_aliases = {
            "revenue": ("营业收入", "营收"),
            "net_profit": ("归母净利润", "净利润"),
            "operating_cash_flow": ("经营活动现金流", "经营现金流"),
            "roe": ("净资产收益率", "ROE"),
            "gross_margin": ("毛利率",),
        }
        facts: list[Fact] = []
        boundaries = re.compile(r"[。！？!?；;\n]|(?<!\d)[，,、：:]|(?<=\d)[，,、：:](?!\d)")
        for match in number_pattern.finditer(artifact.snippet):
            left = 0
            right = len(artifact.snippet)
            for boundary in boundaries.finditer(artifact.snippet):
                if boundary.end() <= match.start():
                    left = boundary.end()
                elif boundary.start() >= match.end():
                    right = boundary.start()
                    break
            clause = artifact.snippet[left:right]
            metric_matches = [metric for metric, aliases in metric_aliases.items()
                              if any(alias.casefold() in clause.casefold() for alias in aliases)]
            if len(metric_matches) != 1:
                continue
            metric = metric_matches[0]
            unit = match.group(2)
            if unit in {"%", "％", "百分比"} and any(word in clause for word in ("同比", "环比", "增长率", "增幅")):
                metric += "_growth"
            value = float(match.group(1).replace(",", ""))
            entity_scope = "parent" if any(word in clause for word in ("归母", "母公司")) else ("consolidated" if "合并" in clause else "unknown")
            raw = {
                "metric": metric, "value": value, "unit": unit, "period": period,
                "period_kind": period_kind, "entity_scope": entity_scope,
                "company_code": code, "evidence_ids": evidence_ids,
            }
            fact = self.normalize(raw, artifact, scope)
            if fact is not None:
                facts.append(fact)
        return tuple(facts)


def _source_category(provider: str) -> str:
    normalized = provider.casefold()
    if normalized in {"tencent", "market", "market-data", "market_data"}:
        return "market"
    if normalized in {"mcp", "stock-data-mcp"}:
        return "mcp"
    if normalized in {"web", "web_search"}:
        return "web"
    return "unknown"


def _compatible_inputs(current: Fact, baseline: Fact) -> bool:
    return (
        current.metric == baseline.metric
        and current.company_code == baseline.company_code
        and current.entity_scope == baseline.entity_scope
        and current.unit == baseline.unit
        and current.source_category == baseline.source_category
        and current.source_category != "unknown"
        and isinstance(current.id, str) and current.id.startswith("fact_")
        and isinstance(baseline.id, str) and baseline.id.startswith("fact_")
        and current.verification in {"verified", "reference"}
        and baseline.verification in {"verified", "reference"}
    )


def _dated_periods(current: Fact, baseline: Fact) -> tuple[date, date] | None:
    try:
        return date.fromisoformat(current.period), date.fromisoformat(baseline.period)
    except (TypeError, ValueError):
        return None


def _derived_fact(current: Fact, baseline: Fact, value: float, unit: str, formula: str, *, metric: str | None = None) -> Fact:
    evidence_ids = tuple(dict.fromkeys(current.evidence_ids + baseline.evidence_ids))
    verification = "verified" if current.source_category == "local_pdf" else "reference"
    as_of = max(filter(None, (current.as_of, baseline.as_of)), default="")
    return Fact(
        metric=metric or current.metric, value=value, unit=unit, period=current.period,
        period_kind=current.period_kind, entity_scope=current.entity_scope,
        company_code=current.company_code, source_type="derived", evidence_ids=evidence_ids,
        verification=verification, as_of=as_of, source_category=current.source_category,
        derived_from_ids=(current.id, baseline.id), formula=formula,
    )


def derive_growth(current: Fact, baseline: Fact, *, relation: str) -> Fact | None:
    """Derive percent growth only across comparable, adjacent, same-source periods."""
    if not _compatible_inputs(current, baseline) or current.period_kind != baseline.period_kind:
        return None
    dates = _dated_periods(current, baseline)
    if dates is None:
        return None
    current_date, baseline_date = dates
    if baseline.value == 0:
        return None
    if relation == "yoy":
        if current_date.year - baseline_date.year != 1 or (current_date.month, current_date.day) != (baseline_date.month, baseline_date.day):
            return None
    elif relation == "qoq":
        if current.period_kind not in {"single_quarter", "quarterly"}:
            return None
        current_quarter = current_date.year * 4 + (current_date.month - 1) // 3
        baseline_quarter = baseline_date.year * 4 + (baseline_date.month - 1) // 3
        if current_quarter - baseline_quarter != 1:
            return None
    else:
        return None
    value = (current.value - baseline.value) / abs(baseline.value) * 100
    if not isfinite(value):
        return None
    return _derived_fact(current, baseline, value, "百分比", f"({current.value} - {baseline.value}) / abs({baseline.value}) * 100 [{relation}]")


def derive_percentage_point_delta(current: Fact, baseline: Fact) -> Fact | None:
    dates = _dated_periods(current, baseline)
    if (not _compatible_inputs(current, baseline) or current.unit != "百分比"
            or current.period_kind != baseline.period_kind or current.period == baseline.period
            or current.id == baseline.id or dates is None or dates[0] <= dates[1]):
        return None
    return _derived_fact(current, baseline, current.value - baseline.value, "百分点",
                         f"{current.value}% - {baseline.value}% [percentage-point delta]")


def derive_index_delta(current: Fact, baseline: Fact) -> Fact | None:
    dates = _dated_periods(current, baseline)
    if (not _compatible_inputs(current, baseline) or current.source_category != "market"
            or current.unit != "点" or current.period_kind != baseline.period_kind
            or current.period == baseline.period or current.id == baseline.id
            or dates is None or dates[0] <= dates[1]):
        return None
    return _derived_fact(current, baseline, current.value - baseline.value, "点",
                         f"{current.value} - {baseline.value} [index-point delta]",
                         metric="index_close_delta")


def derive_available_facts(
    facts: Sequence[Fact], *, relations: Sequence[str] = (),
    percentage_points: bool = False, index_delta: bool = False,
) -> tuple[Fact, ...]:
    """Derive only explicitly requested comparisons from adjacent, sourced facts."""
    groups: dict[tuple[str, str, str, str, str, str], dict[str, Fact]] = defaultdict(dict)
    for fact in facts:
        if fact.source_category in {"local_pdf", "market"}:
            key = (fact.source_category, fact.company_code, fact.metric,
                   fact.unit, fact.entity_scope, fact.period_kind)
            groups[key].setdefault(fact.period, fact)
    derived: dict[str, Fact] = {}
    for (category, _code, _metric, unit, _entity, _kind), periods in groups.items():
        ordered = [periods[period] for period in sorted(periods)]
        if category == "local_pdf":
            for current, baseline in zip(ordered[1:], ordered):
                if unit not in {"点", "百分点", "家"}:
                    for relation in relations:
                        fact = derive_growth(current, baseline, relation=relation)
                        if fact is not None:
                            derived[fact.id] = fact
                if percentage_points and unit == "百分比":
                    fact = derive_percentage_point_delta(current, baseline)
                    if fact is not None:
                        derived[fact.id] = fact
        elif (index_delta and unit == "点" and _metric.startswith("index_close") and len(ordered) >= 2):
            fact = derive_index_delta(ordered[-1], ordered[0])
            if fact is not None:
                derived[fact.id] = fact
    return tuple(derived.values())


def facts_from_market_result(result: SourceResult, artifact: ToolArtifact) -> tuple[Fact, ...]:
    """Extract daily Tencent Kline close facts; reject arbitrary tools and unbounded rows."""
    if (not isinstance(result, SourceResult) or not isinstance(artifact, ToolArtifact)
            or result.provider != "tencent" or result.operation != "kline"
            or result.status not in {"success", "partial"} or artifact.provider != "tencent"
            or artifact.tool_name != "kline" or artifact.status not in {"success", "partial"}
            or not result.as_of or not _is_iso_date(result.as_of[:10]) or artifact.as_of != result.as_of
            or not result.call_id or artifact.source_id != result.call_id):
        return ()
    rows = result.payload if isinstance(result.payload, tuple) else ()
    coverage_bounds = _coverage_date_bounds(result.coverage.data_window)
    if coverage_bounds is None:
        return ()
    facts = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol, period, close = row.get("symbol"), row.get("date"), row.get("close")
        if (not isinstance(symbol, str) or not re.fullmatch(r"(?:\d{6}|(?:sh|sz)\d{6})", symbol, re.IGNORECASE)
                or not isinstance(period, str) or not _is_iso_date(period)
                or not coverage_bounds[0] <= date.fromisoformat(period) <= coverage_bounds[1]
                or date.fromisoformat(period) > date.fromisoformat(result.as_of[:10])
                or isinstance(close, bool) or not isinstance(close, (int, float))
                or not isfinite(close) or close < 0):
            continue
        is_index = symbol[:2].casefold() in {"sh", "sz"}
        facts.append(Fact(
            metric="index_close" if is_index else "price", value=float(close),
            unit="点" if is_index else "元/股", period=period, period_kind="daily",
            entity_scope="index" if is_index else "security", company_code=symbol,
            source_type="tool", evidence_ids=(f"{result.call_id}:{symbol}:{period}",),
            verification="reference", as_of=result.as_of, source_category="market",
        ))
    return tuple(facts)


def _coverage_date_bounds(value: str | None) -> tuple[date, date] | None:
    if not isinstance(value, str) or not value:
        return None
    parts = value.split("至", 1)
    try:
        if len(parts) == 1:
            day = date.fromisoformat(parts[0])
            return day, day
        start, end = date.fromisoformat(parts[0]), date.fromisoformat(parts[1])
        return (start, end) if start <= end else None
    except ValueError:
        return None


def _is_iso_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def detect_conflicts(facts: Sequence[Fact]) -> tuple[FactConflict, ...]:
    groups: dict[tuple[str, str, str, str, str, str], list[Fact]] = defaultdict(list)
    for fact in facts:
        groups[(fact.company_code, fact.metric, fact.period, fact.period_kind,
                fact.entity_scope, fact.unit)].append(fact)
    conflicts = []
    for (_, metric, _, _, _, _), group in groups.items():
        if len(group) > 1 and len({fact.value for fact in group}) > 1:
            conflicts.append(FactConflict(metric=metric, facts=tuple(group), reason="同指标同期间同口径数值不一致"))
    return tuple(conflicts)
