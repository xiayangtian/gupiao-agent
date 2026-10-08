"""Score offline chat runtime observations and compare comparable baselines.

Blocking findings are safety contracts (scope/source authorization, unsupported
deterministic numbers, over-claimed terminal status). Quality deltas and
natural-language forbidden claims stay reviewable rather than silently passing.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from webapp.chat_runtime_eval_cases import RuntimeCase
from webapp.chat_runtime_eval_runner import HARNESS_VERSION  # noqa: F401  (re-exported contract)

# Fixture call names → case source vocabulary.
_SOURCE_ALIASES = {"retrieval": "retrieval", "market": "market", "mcp": "mcp",
                   "web": "web", "model": "model", "clock": "clock"}
_VALUE_TOLERANCE = 1e-6


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    category: str
    status: str
    expected_status: str
    terminal_matches: bool
    blocking_codes: tuple[str, ...]
    quality_codes: tuple[str, ...]
    review_items: tuple[str, ...]
    citation_support_found: int
    citation_support_expected: int
    citation_support: float | None
    call_attempts: Mapping[str, int]
    cost: str
    usage_reported: bool


@dataclass(frozen=True)
class Comparison:
    comparable: bool
    reasons: tuple[str, ...]
    declared_change: str
    regressions: tuple[str, ...]
    improvements: tuple[str, ...]
    unchanged: tuple[str, ...]


def _claim_value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _fact_matches_claim(claim: Any, fact: Mapping[str, Any]) -> bool:
    fact_value = _claim_value(fact.get("value"))
    claim_value = _claim_value(claim.value)
    if claim_value is None or fact_value is None:
        return False
    if str(fact.get("unit") or "") != claim.unit:
        return False
    if str(fact.get("period") or "") != claim.period:
        return False
    return abs(fact_value - claim_value) <= _VALUE_TOLERANCE


def _claim_evidence_ids(claim: Any, fact: Mapping[str, Any]) -> set[str]:
    ids = {str(item) for item in (fact.get("evidence_ids") or ())}
    return ids | {claim.evidence_id}


def score(case: RuntimeCase, observed: Any) -> CaseScore:
    """Score one observation against its case's frozen claims and authorizations."""
    blocking: list[str] = []
    quality: list[str] = []
    review: list[str] = []

    allowed_sources = set(case.allowed_sources) | {"model"}
    unauthorized = sorted({_SOURCE_ALIASES.get(name, name) for name in observed.call_attempts}
                          - set(case.allowed_sources) - {"model"})
    if unauthorized:
        blocking.append("unauthorized_source")

    scope_codes = {str(code) for code in case.scope.get("companies") or ()}
    scope_reports = {str(item) for item in case.scope.get("report_ids") or ()}
    forbidden_reports = set(case.forbidden_report_ids)
    for artifact in observed.artifacts:
        report_id = str(artifact.get("report_id") or "")
        if not report_id:
            continue
        if report_id in forbidden_reports:
            blocking.append("scope_violation")
        elif scope_reports and report_id not in scope_reports:
            blocking.append("scope_violation")
    for fact in observed.facts:
        code = str(fact.get("company_code") or "")
        if scope_codes and code and code not in scope_codes:
            blocking.append("scope_violation")

    found = 0
    expected = len(case.expected_claims)
    for claim in case.expected_claims:
        supported = any(_fact_matches_claim(claim, fact) and claim.evidence_id in _claim_evidence_ids(claim, fact)
                        for fact in observed.facts)
        if supported:
            found += 1
            continue
        blocked = any(claim.evidence_id in _claim_evidence_ids(claim, fact) for fact in observed.facts)
        blocking.append("unsupported_claim" if blocked else "missing_claim")
    citation_support = (found / expected) if expected else None

    if observed.persisted_status == "completed" and case.expected_status != "completed":
        blocking.append("overclaimed_status")
    elif observed.persisted_status != case.expected_status:
        quality.append("status_mismatch")
    if observed.usage is None:
        quality.append("usage_unknown")
    review.extend(case.forbidden_claims)

    return CaseScore(
        case_id=case.id, category=case.category, status=observed.persisted_status,
        expected_status=case.expected_status,
        terminal_matches=observed.persisted_status == case.expected_status,
        blocking_codes=tuple(dict.fromkeys(blocking)),
        quality_codes=tuple(dict.fromkeys(quality)),
        review_items=tuple(review),
        citation_support_found=found, citation_support_expected=expected,
        citation_support=citation_support,
        call_attempts=dict(observed.call_attempts),
        cost="unknown", usage_reported=observed.usage is not None,
    )


def _comparability_reasons(base: Mapping[str, Any], candidate: Mapping[str, Any], declared_change: str) -> list[str]:
    reasons: list[str] = []
    if not declared_change.strip():
        reasons.append("declared_change_required")
    for key in ("suite_id", "corpus_version", "harness_version", "clock"):
        if base.get(key) != candidate.get(key):
            reasons.append(f"incomparable_{key}")
    base_cases = {item["id"]: item for item in base.get("cases", [])}
    candidate_cases = {item["id"]: item for item in candidate.get("cases", [])}
    if set(base_cases) != set(candidate_cases):
        reasons.append("incomparable_case_set")
    for case_id in sorted(set(base_cases) & set(candidate_cases)):
        if base_cases[case_id].get("calls") != candidate_cases[case_id].get("calls"):
            reasons.append(f"incomparable_call_limits:{case_id}")
    return reasons


def compare(base: Mapping[str, Any], candidate: Mapping[str, Any], *, declared_change: str) -> Comparison:
    """Compare two reports; only the declared change (and code revision) may differ."""
    reasons = _comparability_reasons(base, candidate, declared_change)
    if reasons:
        return Comparison(False, tuple(reasons), declared_change, (), (), ())
    base_cases = {item["id"]: item for item in base["cases"]}
    regressions: list[str] = []
    improvements: list[str] = []
    unchanged: list[str] = []
    for case_id, candidate_case in sorted({item["id"]: item for item in candidate["cases"]}.items()):
        base_case = base_cases[case_id]
        base_blocking = set(base_case.get("blocking_codes", ()))
        if set(candidate_case.get("blocking_codes", ())) - base_blocking:
            regressions.append(f"blocking:{case_id}")
        elif candidate_case.get("citation_support_found", 0) < base_case.get("citation_support_found", 0):
            regressions.append(f"citation_support:{case_id}")
        elif candidate_case.get("citation_support_found", 0) > base_case.get("citation_support_found", 0):
            improvements.append(case_id)
        else:
            unchanged.append(case_id)
    return Comparison(not regressions, (), declared_change, tuple(regressions), tuple(improvements), tuple(unchanged))


def build_report(suite_id: str, corpus_version: str, code_revision: str,
                 scores: Sequence[CaseScore], *, elapsed_seconds: float,
                 clock: str) -> dict[str, Any]:
    """Aggregate whitelisted counts only: no prompts, answers, snippets or tool arguments."""
    return {
        "suite_id": suite_id,
        "corpus_version": corpus_version,
        "code_revision": code_revision,
        "harness_version": HARNESS_VERSION,
        "clock": clock,
        "totals": {
            "cases": len(scores),
            "blocking": sum(1 for item in scores if item.blocking_codes),
            "status_matches": sum(1 for item in scores if item.terminal_matches),
            "review_required": sum(1 for item in scores if item.review_items),
            "elapsed_seconds": round(elapsed_seconds, 4),
        },
        "cases": [
            {key: value for key, value in asdict(item).items() if key != "call_attempts"} | {"calls": dict(item.call_attempts)}
            for item in scores
        ],
    }


def write_report(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write an evaluation report; never touches the read-only quality sidecar."""
    path = Path(path)
    if path.name == "research_quality_summary.json":
        raise ValueError("refusing to overwrite the read-only quality sidecar")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False)
    try:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)
