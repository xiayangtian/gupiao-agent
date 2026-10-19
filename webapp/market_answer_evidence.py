"""Strict, source-local validation for model-interpreted market answer claims."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from webapp.chat_models import Fact, Scope
from webapp.source_runtime import SourceResult

_MAX_ANSWER_CHARS = 12_000
_MAX_CLAIMS = 80
_MAX_TEXT = 160
_MAX_POINTER_CHARS = 512
_MAX_POINTER_DEPTH = 24
_ALLOWED_INTENTS = frozenset({"market_quote", "market_trend", "market_recap"})
_SERVER_DERIVED_METRICS = frozenset({
    "price_change", "price_change_pct", "index_close_delta", "index_close_pct_change",
})
_TOP_LEVEL_KEYS = frozenset({"answer", "claims"})
_CLAIM_KEYS = frozenset({
    "metric", "value", "unit", "period", "period_kind", "entity_scope",
    "company_code", "source_id", "value_pointer", "identity_pointer",
    "time_pointer", "fact_id", "formula",
})
_DANGEROUS_POINTER_SEGMENTS = frozenset({"__proto__", "prototype", "constructor"})
_MARKET_UNITS: dict[str, frozenset[str]] = {
    "price": frozenset({"元/股"}),
    "previous_close": frozenset({"元/股"}),
    "price_change": frozenset({"元/股"}),
    "price_change_pct": frozenset({"百分比", "%", "％"}),
    "index_close": frozenset({"点", "指数点"}),
    "index_close_delta": frozenset({"点", "指数点"}),
    "index_close_pct_change": frozenset({"百分比", "%", "％"}),
    "turnover": frozenset({"元", "万元", "亿元"}),
    "volume": frozenset({"股", "手"}),
    "limit_up_count": frozenset({"家", "只"}),
    "limit_down_count": frozenset({"家", "只"}),
    "market_limit_up_count": frozenset({"家"}),
    "market_limit_down_count": frozenset({"家"}),
    "market_volume_ratio": frozenset({"倍"}),
    "market_total_turnover": frozenset({"亿元"}),
    "market_total_volume": frozenset({"手"}),
    "market_security_sample_count": frozenset({"只"}),
    "market_classified_security_count": frozenset({"只"}),
    "market_volume_sample_count": frozenset({"只"}),
    "market_turnover_sample_count": frozenset({"只"}),
    "market_volume_ratio_sample_count": frozenset({"只"}),
    "market_volume_ratio_active_count": frozenset({"只"}),
    "market_volume_ratio_coverage": frozenset({"百分比", "%", "％"}),
}
_METRIC_FIELD_ALIASES: dict[str, frozenset[str]] = {
    "price": frozenset({"price", "last_price", "latest_price", "current_price"}),
    "previous_close": frozenset({"pre_close", "prev_close", "previous_close"}),
    "price_change": frozenset({"change", "price_change", "change_amount"}),
    "price_change_pct": frozenset({"change_pct", "price_change_pct", "change_percent"}),
    "index_close": frozenset({"close", "index_close"}),
    "index_close_delta": frozenset({"close_delta", "index_close_delta"}),
    "index_close_pct_change": frozenset({"close_pct_change", "index_close_pct_change"}),
    "turnover": frozenset({"turnover", "amount", "成交额"}),
    "volume": frozenset({"volume", "成交量"}),
    "limit_up_count": frozenset({"limit_up_count", "advancing_count", "涨停家数"}),
    "limit_down_count": frozenset({"limit_down_count", "declining_count", "跌停家数"}),
}


@dataclass(frozen=True)
class MarketClaim:
    metric: str
    value: float
    unit: str
    period: str
    period_kind: str
    entity_scope: str
    company_code: str
    source_id: str = ""
    value_pointer: str = ""
    identity_pointer: str = ""
    time_pointer: str = ""
    fact_id: str = ""
    formula: str = ""


@dataclass(frozen=True)
class MarketAnswerDraft:
    answer: str
    claims: tuple[MarketClaim, ...]


@dataclass(frozen=True)
class MarketClaimValidation:
    facts: tuple[Fact, ...]
    rejected: tuple[str, ...]
    status: str


def _bounded_string(value: Any, name: str, *, max_chars: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
        raise ValueError(f"{name} is invalid")
    return value.strip()


def _validate_pointer_text(pointer: str) -> tuple[str, ...]:
    if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > _MAX_POINTER_CHARS:
        raise ValueError("invalid_pointer")
    raw_parts = pointer[1:].split("/")
    if len(raw_parts) > _MAX_POINTER_DEPTH or any(re.search(r"~(?![01])", part) for part in raw_parts):
        raise ValueError("invalid_pointer")
    parts = tuple(part.replace("~1", "/").replace("~0", "~") for part in raw_parts)
    if any(part in _DANGEROUS_POINTER_SEGMENTS for part in parts):
        raise ValueError("invalid_pointer")
    return parts


def _parse_claim(value: Any) -> MarketClaim:
    if not isinstance(value, Mapping):
        raise ValueError("claim_not_object")
    if set(value) - _CLAIM_KEYS:
        raise ValueError("claim_unknown_field")
    required = {"metric", "value", "unit", "period", "period_kind", "entity_scope", "company_code"}
    if not required.issubset(value):
        raise ValueError("claim_missing_field")
    metric = _bounded_string(value["metric"], "metric")
    raw_value = value["value"]
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)) or not math.isfinite(raw_value):
        raise ValueError("claim_value_invalid")
    unit = _bounded_string(value["unit"], "unit", max_chars=32)
    period = _bounded_string(value["period"], "period", max_chars=32)
    try:
        date.fromisoformat(period)
    except ValueError as exc:
        raise ValueError("claim_period_invalid") from exc
    period_kind = _bounded_string(value["period_kind"], "period_kind", max_chars=32)
    if period_kind not in {"point_in_time", "daily", "weekly", "monthly", "interval"}:
        raise ValueError("claim_period_kind_invalid")
    entity_scope = _bounded_string(value["entity_scope"], "entity_scope", max_chars=32)
    if entity_scope not in {"company", "security", "index", "market", "industry"}:
        raise ValueError("claim_entity_scope_invalid")
    company_code = _bounded_string(value["company_code"], "company_code", max_chars=32)
    for name in ("source_id", "value_pointer", "identity_pointer", "time_pointer", "fact_id", "formula"):
        if name in value and not isinstance(value[name], str):
            raise ValueError("claim_source_reference_invalid")
    source_id = value.get("source_id", "")
    value_pointer = value.get("value_pointer", "")
    identity_pointer = value.get("identity_pointer", "")
    time_pointer = value.get("time_pointer", "")
    fact_id = value.get("fact_id", "")
    formula = value.get("formula", "")
    if len(source_id) > _MAX_TEXT or len(fact_id) > _MAX_TEXT:
        raise ValueError("claim_source_reference_invalid")
    if fact_id:
        if source_id or value_pointer or identity_pointer or time_pointer:
            raise ValueError("claim_evidence_anchor_ambiguous")
    elif not source_id or not value_pointer:
        raise ValueError("claim_source_reference_missing")
    for pointer in (value_pointer, identity_pointer, time_pointer):
        if pointer:
            _validate_pointer_text(pointer)
    if formula and (formula not in {"price_change", "price_change_pct"} or not fact_id):
        raise ValueError("claim_formula_invalid")
    return MarketClaim(
        metric=metric, value=float(raw_value), unit=unit, period=period,
        period_kind=period_kind, entity_scope=entity_scope, company_code=company_code,
        source_id=source_id, value_pointer=value_pointer,
        identity_pointer=identity_pointer, time_pointer=time_pointer,
        fact_id=fact_id, formula=formula,
    )


def parse_market_answer_draft(raw: str) -> MarketAnswerDraft:
    """Parse a strict model-produced JSON draft without treating it as verified evidence."""
    if not isinstance(raw, str) or len(raw) > _MAX_ANSWER_CHARS * 4:
        raise ValueError("invalid_market_draft")
    try:
        data = json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non_finite")))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("invalid_market_draft") from exc
    if not isinstance(data, Mapping) or set(data) != _TOP_LEVEL_KEYS:
        raise ValueError("invalid_market_draft")
    answer = data.get("answer")
    claims = data.get("claims")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > _MAX_ANSWER_CHARS:
        raise ValueError("invalid_market_draft")
    if not isinstance(claims, list) or len(claims) > _MAX_CLAIMS:
        raise ValueError("invalid_market_draft")
    try:
        parsed_claims = tuple(_parse_claim(item) for item in claims)
    except ValueError as exc:
        raise ValueError("invalid_market_draft") from exc
    return MarketAnswerDraft(answer.strip(), parsed_claims)


def _resolve_pointer(payload: Any, pointer: str) -> Any:
    parts = _validate_pointer_text(pointer)
    value = payload
    for part in parts:
        if isinstance(value, Mapping):
            if part not in value:
                raise ValueError("pointer_missing")
            value = value[part]
        elif isinstance(value, (list, tuple)):
            if not part.isdigit() or (len(part) > 1 and part.startswith("0")):
                raise ValueError("invalid_pointer")
            index = int(part)
            if index >= len(value):
                raise ValueError("pointer_missing")
            value = value[index]
        else:
            raise ValueError("invalid_pointer")
    return value


def _normalized_code(value: Any) -> str:
    text = str(value).strip().upper()
    if len(text) >= 2 and text[:2] in {"SH", "SZ", "BJ"}:
        text = text[2:]
    return text


def _source_by_id(sources: Sequence[SourceResult], source_id: str) -> SourceResult | None:
    return next((source for source in sources if source.call_id == source_id), None)


def _scope_allows(scope: Scope, code: str) -> bool:
    if not scope.companies:
        return True
    return any(_normalized_code(company.code) == _normalized_code(code) for company in scope.companies)


def _date_from(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _window_allows(window: Any, day: date) -> bool:
    if window is None:
        return True
    start = getattr(window, "start_date", None)
    end = getattr(window, "end_date", None)
    if start is not None and day < start:
        return False
    if end is not None and day > end:
        return False
    return True


def _field_key(pointer: str) -> str:
    return pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~").lower()


def _semantic_anchor(metric: str, pointer: str) -> bool:
    return _field_key(pointer) in _METRIC_FIELD_ALIASES.get(metric, frozenset())


def _build_fact(claim: MarketClaim, value: float, source: SourceResult) -> Fact:
    return Fact(
        metric=claim.metric,
        value=value,
        unit=claim.unit,
        period=claim.period,
        period_kind=claim.period_kind,
        entity_scope=claim.entity_scope,
        company_code=claim.company_code,
        source_type="tool",
        evidence_ids=(f"{source.call_id}#{claim.value_pointer}",),
        verification="reference",
        as_of=source.as_of,
        source_category="market",
    )


def _validate_source_claim(
    claim: MarketClaim, sources: Sequence[SourceResult], scope: Scope, window: Any,
) -> tuple[Fact | None, str]:
    source = _source_by_id(sources, claim.source_id)
    if source is None or source.category != "market" or source.status not in {"success", "partial"}:
        return None, "unresolved_source_reference"
    if claim.metric in _SERVER_DERIVED_METRICS:
        return None, "derived_metric_requires_server_fact"
    if not _scope_allows(scope, claim.company_code):
        return None, "market_identity_mismatch"
    allowed_units = _MARKET_UNITS.get(claim.metric)
    if not allowed_units or claim.unit not in allowed_units:
        return None, "market_unit_mismatch"
    try:
        raw_value = _resolve_pointer(source.payload, claim.value_pointer)
    except ValueError:
        return None, "unresolved_source_reference"
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)) or not math.isfinite(raw_value):
        return None, "source_value_mismatch"
    if not math.isclose(float(raw_value), claim.value, rel_tol=1e-9, abs_tol=1e-6):
        return None, "source_value_mismatch"
    if not _semantic_anchor(claim.metric, claim.value_pointer):
        return None, "ambiguous_metric_semantics"

    requested_symbol = source.request_context.get("symbol") or source.request_context.get("code")
    if requested_symbol and _normalized_code(requested_symbol) != _normalized_code(claim.company_code):
        return None, "market_identity_mismatch"
    if claim.identity_pointer:
        try:
            identity = _resolve_pointer(source.payload, claim.identity_pointer)
        except ValueError:
            return None, "market_identity_mismatch"
        if _normalized_code(identity) != _normalized_code(claim.company_code):
            return None, "market_identity_mismatch"
    elif not requested_symbol:
        return None, "market_identity_mismatch"

    observed_time = source.as_of
    if claim.time_pointer:
        try:
            observed_time = str(_resolve_pointer(source.payload, claim.time_pointer))
        except ValueError:
            return None, "market_time_mismatch"
    observed_day = _date_from(observed_time)
    if observed_day is None or claim.period != observed_day.isoformat() or not _window_allows(window, observed_day):
        return None, "market_time_mismatch"
    try:
        fact = _build_fact(claim, float(raw_value), source)
    except ValueError:
        return None, "invalid_market_draft"
    return fact, ""


def validate_market_answer_claims(
    draft: MarketAnswerDraft,
    *,
    sources: Sequence[SourceResult],
    existing_facts: Sequence[Fact],
    scope: Scope,
    window: Any,
    intent: str,
) -> MarketClaimValidation:
    """Resolve source anchors to trusted Facts; never performs I/O or trusts claim status."""
    if intent not in _ALLOWED_INTENTS or not isinstance(scope, Scope):
        return MarketClaimValidation((), ("invalid_market_draft",), "blocked")
    facts_by_id = {fact.id: fact for fact in existing_facts}
    accepted: list[Fact] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for claim in draft.claims:
        if claim.fact_id:
            fact = facts_by_id.get(claim.fact_id)
            allowed_units = _MARKET_UNITS.get(claim.metric)
            if (fact is None or fact.source_category not in {"market", "mcp"}
                    or fact.verification not in {"verified", "reference"}
                    or fact.metric != claim.metric or not math.isclose(fact.value, claim.value, rel_tol=1e-9, abs_tol=1e-6)
                    or fact.unit != claim.unit or not allowed_units or fact.unit not in allowed_units
                    or fact.period != claim.period or fact.period_kind != claim.period_kind
                    or fact.entity_scope != claim.entity_scope or fact.company_code != claim.company_code
                    or not _scope_allows(scope, fact.company_code)):
                rejected.append("unresolved_source_reference")
                continue
            if fact.source_type == "derived":
                inputs = [facts_by_id.get(item) for item in fact.derived_from_ids]
                if (not fact.formula or not inputs or any(
                    item is None or item.source_category not in {"market", "mcp"}
                    or item.verification not in {"verified", "reference"}
                    or item.company_code != fact.company_code
                    for item in inputs
                ) or (claim.formula and claim.formula != fact.formula)):
                    rejected.append("unresolved_source_reference")
                    continue
            elif fact.metric in _SERVER_DERIVED_METRICS or claim.formula:
                rejected.append("derived_metric_requires_server_fact")
                continue
            candidate = fact
        else:
            candidate, error = _validate_source_claim(claim, sources, scope, window)
            if candidate is None:
                rejected.append(error)
                continue
        if candidate.id not in seen:
            accepted.append(candidate)
            seen.add(candidate.id)
    if not rejected:
        status = "passed"
    elif accepted:
        status = "partial"
    else:
        status = "blocked"
    return MarketClaimValidation(tuple(accepted), tuple(rejected), status)
