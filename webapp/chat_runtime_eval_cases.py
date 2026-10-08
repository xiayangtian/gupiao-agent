"""Strict, offline-only case manifests for chat runtime evaluation."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

_ALLOWED_CATEGORIES = {
    "report", "market", "recap", "knowledge", "research", "followup", "failure", "recovery",
}
_ALLOWED_FIXTURE_SOURCES = {"model", "retrieval", "market", "mcp", "web", "clock"}
_ALLOWED_ROOT_KEYS = {"schema_version", "suite_id", "corpus_version", "cases"}
_ALLOWED_CASE_KEYS = {
    "id", "category", "turns", "scope", "expected_intent", "expected_status",
    "allowed_sources", "expected_claims", "forbidden_claims", "forbidden_report_ids",
    "provider_fixture", "max_calls",
}
_ALLOWED_CLAIM_KEYS = {"subject", "period", "unit", "relation", "value", "evidence_id"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_KEY = re.compile(r"(?:token|secret|password|api[_-]?key|authorization|cookie)", re.I)
_URL = re.compile(r"https?://|wss?://", re.I)


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def _nonempty_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    return value.strip()


def _freeze_json(value: Any, where: str) -> Any:
    if isinstance(value, dict):
        frozen = {}
        for key, child in value.items():
            if not isinstance(key, str) or _SECRET_KEY.search(key):
                raise ValueError(f"{where} contains a forbidden credential field")
            if _URL.search(child) if isinstance(child, str) else False:
                raise ValueError(f"{where} must not contain URLs or network endpoints")
            frozen[key] = _freeze_json(child, f"{where}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, list):
        return tuple(_freeze_json(child, where) for child in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{where} contains a non-finite number")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and _URL.search(value):
            raise ValueError(f"{where} must not contain URLs or network endpoints")
        return value
    raise ValueError(f"{where} contains unsupported JSON data")


@dataclass(frozen=True)
class Claim:
    subject: str
    period: str
    unit: str
    relation: str
    value: str | float | int | bool
    evidence_id: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], where: str) -> "Claim":
        data = _mapping(raw, where)
        unknown = set(data) - _ALLOWED_CLAIM_KEYS
        missing = _ALLOWED_CLAIM_KEYS - set(data)
        if unknown or missing:
            raise ValueError(f"{where} has unknown={sorted(unknown)} missing={sorted(missing)}")
        value = data["value"]
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{where}.value must be finite")
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"{where}.value must be scalar")
        return cls(
            subject=_nonempty_string(data["subject"], f"{where}.subject"),
            period=_nonempty_string(data["period"], f"{where}.period"),
            unit=_nonempty_string(data["unit"], f"{where}.unit"),
            relation=_nonempty_string(data["relation"], f"{where}.relation"),
            value=value,
            evidence_id=_nonempty_string(data["evidence_id"], f"{where}.evidence_id"),
        )


@dataclass(frozen=True)
class RuntimeCase:
    id: str
    category: str
    turns: tuple[str, ...]
    scope: Mapping[str, Any]
    expected_intent: str
    expected_status: str
    allowed_sources: tuple[str, ...]
    expected_claims: tuple[Claim, ...]
    forbidden_claims: tuple[str, ...]
    forbidden_report_ids: tuple[str, ...]
    provider_fixture: Mapping[str, Any]
    max_calls: Mapping[str, int]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuntimeCase":
        data = _mapping(raw, "case")
        unknown = set(data) - _ALLOWED_CASE_KEYS
        missing = _ALLOWED_CASE_KEYS - set(data)
        if unknown or missing:
            raise ValueError(f"case has unknown={sorted(unknown)} missing={sorted(missing)}")
        case_id = _nonempty_string(data["id"], "case.id")
        if not _SAFE_ID.fullmatch(case_id):
            raise ValueError("case.id contains unsafe characters")
        category = _nonempty_string(data["category"], f"case {case_id}.category")
        if category not in _ALLOWED_CATEGORIES:
            raise ValueError(f"case {case_id}.category is not supported")
        turns_raw = data["turns"]
        if not isinstance(turns_raw, list) or not turns_raw:
            raise ValueError(f"case {case_id}.turns must be a non-empty array")
        turns = tuple(_nonempty_string(turn, f"case {case_id}.turns[{i}]") for i, turn in enumerate(turns_raw))
        scope = _mapping(data["scope"], f"case {case_id}.scope")
        if set(scope) - {"mode", "companies", "period", "market_window"}:
            raise ValueError(f"case {case_id}.scope has unsupported fields")
        mode = _nonempty_string(scope.get("mode"), f"case {case_id}.scope.mode")
        if mode not in {"company_only", "company_industry", "whole_corpus", "external_market", "general_knowledge"}:
            raise ValueError(f"case {case_id}.scope.mode is unsupported")
        sources = data["allowed_sources"]
        if not isinstance(sources, list) or any(not isinstance(item, str) or not item for item in sources):
            raise ValueError(f"case {case_id}.allowed_sources must be an array of source names")
        if len(sources) != len(set(sources)):
            raise ValueError(f"case {case_id}.allowed_sources contains duplicates")
        claims_raw = data["expected_claims"]
        forbidden_claims = data["forbidden_claims"]
        forbidden_ids = data["forbidden_report_ids"]
        if not isinstance(claims_raw, list) or not isinstance(forbidden_claims, list) or not isinstance(forbidden_ids, list):
            raise ValueError(f"case {case_id} claim and report lists must be arrays")
        fixtures = _mapping(data["provider_fixture"], f"case {case_id}.provider_fixture")
        unknown_fixtures = set(fixtures) - _ALLOWED_FIXTURE_SOURCES
        if unknown_fixtures:
            raise ValueError(f"case {case_id}.provider_fixture has unapproved source(s): {sorted(unknown_fixtures)}")
        frozen_fixtures = _freeze_json(fixtures, f"case {case_id}.provider_fixture")
        calls = _mapping(data["max_calls"], f"case {case_id}.max_calls")
        if set(calls) - _ALLOWED_FIXTURE_SOURCES or any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in calls.values()
        ):
            raise ValueError(f"case {case_id}.max_calls must contain non-negative counts for approved sources")
        if not isinstance(data["scope"], dict):
            raise ValueError(f"case {case_id}.scope must be an object")
        return cls(
            id=case_id,
            category=category,
            turns=turns,
            scope=MappingProxyType(dict(scope)),
            expected_intent=_nonempty_string(data["expected_intent"], f"case {case_id}.expected_intent"),
            expected_status=_nonempty_string(data["expected_status"], f"case {case_id}.expected_status"),
            allowed_sources=tuple(sources),
            expected_claims=tuple(Claim.from_dict(claim, f"case {case_id}.expected_claims[{i}]") for i, claim in enumerate(claims_raw)),
            forbidden_claims=tuple(_nonempty_string(value, f"case {case_id}.forbidden_claims") for value in forbidden_claims),
            forbidden_report_ids=tuple(_nonempty_string(value, f"case {case_id}.forbidden_report_ids") for value in forbidden_ids),
            provider_fixture=frozen_fixtures,
            max_calls=MappingProxyType(dict(calls)),
        )


@dataclass(frozen=True)
class RuntimeSuite:
    schema_version: int
    suite_id: str
    corpus_version: str
    cases: tuple[RuntimeCase, ...]

    @classmethod
    def load(cls, path: Path) -> "RuntimeSuite":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read evaluation suite: {type(exc).__name__}") from exc
        data = _mapping(raw, "suite")
        unknown = set(data) - _ALLOWED_ROOT_KEYS
        missing = _ALLOWED_ROOT_KEYS - set(data)
        if unknown or missing:
            raise ValueError(f"suite has unknown={sorted(unknown)} missing={sorted(missing)}")
        if data["schema_version"] != 1 or isinstance(data["schema_version"], bool):
            raise ValueError("suite.schema_version must be 1")
        if not isinstance(data["cases"], list) or not data["cases"]:
            raise ValueError("suite.cases must be a non-empty array")
        cases = tuple(RuntimeCase.from_dict(case) for case in data["cases"])
        ids = [case.id for case in cases]
        if len(ids) != len(set(ids)):
            raise ValueError("suite contains duplicate case IDs")
        return cls(1, _nonempty_string(data["suite_id"], "suite.suite_id"),
                   _nonempty_string(data["corpus_version"], "suite.corpus_version"), cases)
