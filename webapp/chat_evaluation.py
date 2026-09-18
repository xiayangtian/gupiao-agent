"""Offline, deterministic quality evaluation for immutable trusted-chat runs.

This module is deliberately a contract evaluator rather than an agent runner.  It
only accepts :class:`FixtureAgent`, which replays a versioned fixture that carries
one fixed, serialized ``AnswerRun``/``ResearchRun`` output per case.  No model, MCP,
web, or production-data client is imported, and an arbitrary callable collaborator
is rejected because it could reach the network during a production evaluation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil, isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

from webapp.chat_models import AnswerRun, EvidenceArtifact
from webapp.evidence_identity import artifact_evidence_ids, validated_pdf_url
from webapp.research_models import ResearchRun


#: The only fixture schema this evaluator can replay.
FIXTURE_SCHEMA_VERSION = 2
QUALITY_SUMMARY_SCHEMA_VERSION = 2

_CASE_FIELDS = frozenset((
    "id", "suite", "expected_failure_codes", "question", "scope", "intent", "allowed_sources",
    "expected_fact_ids", "expected_company_codes", "expected_report_ids", "forbidden_report_ids",
    "expected_status",
))
_SUITE_NAMES = frozenset(("health", "probe"))
_FIXTURE_OUTPUT_FIELDS = frozenset(("answer_run", "research_run"))
_ALLOWED_SOURCES = frozenset(("local_pdf", "market_data", "web"))

#: Fixed, safe failure identifiers.  Quality APIs expose counts by these codes only,
#: never the case ids, prompts, or prose that produced them.
FAILURE_CODES = frozenset((
    "scope_leak",
    "scope_boundary_mismatch",
    "source_outside_policy",
    "expected_citation_missing",
    "unsupported_numeric_claim",
    "invalid_pdf_page_url",
    "external_fact_missing_source",
    "external_fact_missing_as_of",
    "completed_run_with_failed_verifier",
    "stopped_run_rendered_complete",
    "run_status_mismatch",
))
_FAILURE_ORDER = tuple(sorted(FAILURE_CODES))


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _texts(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an array")
    return tuple(_text(item, f"{name}[]") for item in value)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _sequence(value: object, name: str) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an array")
    return value


@dataclass(frozen=True)
class EvaluationCase:
    """Versionable input expectations for one controlled question."""

    id: str
    suite: Literal["health", "probe"]
    expected_failure_codes: tuple[str, ...]
    question: str
    scope: str
    intent: str
    allowed_sources: tuple[str, ...]
    expected_fact_ids: tuple[str, ...]
    expected_company_codes: tuple[str, ...]
    expected_report_ids: tuple[str, ...]
    forbidden_report_ids: tuple[str, ...]
    expected_status: str

    def __post_init__(self) -> None:
        for name in ("id", "question", "scope", "intent", "expected_status"):
            _text(getattr(self, name), name)
        if self.suite not in _SUITE_NAMES:
            raise ValueError("suite must be health or probe")
        if not isinstance(self.expected_failure_codes, tuple):
            raise ValueError("expected_failure_codes must be a tuple")
        _texts(self.expected_failure_codes, "expected_failure_codes")
        if not set(self.expected_failure_codes).issubset(FAILURE_CODES):
            raise ValueError("expected_failure_codes contains unsupported code")
        if self.suite == "health" and self.expected_failure_codes:
            raise ValueError("health cases must not declare expected_failure_codes")
        if self.suite == "probe" and not self.expected_failure_codes:
            raise ValueError("probe cases require expected_failure_codes")
        for name in (
            "allowed_sources", "expected_fact_ids", "expected_company_codes",
            "expected_report_ids", "forbidden_report_ids",
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple):
                raise ValueError(f"{name} must be a tuple")
            _texts(values, name)
        if not set(self.allowed_sources).issubset(_ALLOWED_SOURCES):
            raise ValueError("allowed_sources contains unsupported source")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "EvaluationCase":
        if not isinstance(value, Mapping):
            raise ValueError("evaluation case must be an object")
        missing = [name for name in _CASE_FIELDS if name not in value]
        if missing:
            # Deterministic ordering makes the schema failure usable in fixtures.
            raise ValueError(f"evaluation case missing required fields: {', '.join(sorted(missing))}")
        return cls(
            id=_text(value.get("id"), "id"),
            suite=_text(value.get("suite"), "suite"),  # type: ignore[arg-type]
            expected_failure_codes=_texts(value.get("expected_failure_codes"), "expected_failure_codes"),
            question=_text(value.get("question"), "question"),
            scope=_text(value.get("scope"), "scope"),
            intent=_text(value.get("intent"), "intent"),
            allowed_sources=_texts(value.get("allowed_sources"), "allowed_sources"),
            expected_fact_ids=_texts(value.get("expected_fact_ids"), "expected_fact_ids"),
            expected_company_codes=_texts(value.get("expected_company_codes"), "expected_company_codes"),
            expected_report_ids=_texts(value.get("expected_report_ids"), "expected_report_ids"),
            forbidden_report_ids=_texts(value.get("forbidden_report_ids"), "forbidden_report_ids"),
            expected_status=_text(value.get("expected_status"), "expected_status"),
        )


@dataclass(frozen=True)
class EvaluationFixture:
    """A versioned corpus of cases plus each case's fixed serialized output."""

    schema_version: int
    cases: tuple[EvaluationCase, ...]
    outputs: Mapping[str, tuple[AnswerRun, ResearchRun | None]]

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise ValueError("fixture schema_version must be an integer")
        if self.schema_version != FIXTURE_SCHEMA_VERSION:
            raise ValueError(f"unsupported fixture schema_version: {self.schema_version}")
        if not isinstance(self.cases, tuple) or not self.cases:
            raise ValueError("fixture must contain at least one case")
        if not all(isinstance(case, EvaluationCase) for case in self.cases):
            raise ValueError("fixture cases must be EvaluationCase records")
        if not isinstance(self.outputs, Mapping):
            raise ValueError("fixture outputs must be a mapping")
        case_ids = [case.id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("fixture case ids must be unique")
        if set(self.outputs) != set(case_ids):
            raise ValueError("every fixture case requires a fixed serialized output")
        if any(
            not isinstance(output, tuple)
            or len(output) != 2
            or not isinstance(output[0], AnswerRun)
            or (output[1] is not None and not isinstance(output[1], ResearchRun))
            for output in self.outputs.values()
        ):
            raise ValueError("fixture outputs must be (AnswerRun, ResearchRun | None) pairs")
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationFixture":
        data = _mapping(payload, "evaluation fixture")
        unknown = set(data) - {"schema_version", "cases"}
        if unknown:
            raise ValueError(f"evaluation fixture contains unsupported keys: {sorted(unknown)}")
        cases: list[EvaluationCase] = []
        outputs: dict[str, tuple[AnswerRun, ResearchRun | None]] = {}
        for raw in _sequence(data.get("cases", []), "cases"):
            entry = _mapping(raw, "evaluation case entry")
            unknown = set(entry) - _CASE_FIELDS - _FIXTURE_OUTPUT_FIELDS
            if unknown:
                raise ValueError(f"evaluation case entry contains unsupported keys: {sorted(unknown)}")
            case = EvaluationCase.from_dict(entry)
            missing = sorted(_FIXTURE_OUTPUT_FIELDS - set(entry))
            if missing:
                raise ValueError(f"fixture case {case.id} missing fixed output: {', '.join(missing)}")
            if case.id in outputs:
                raise ValueError("fixture case ids must be unique")
            answer_run = AnswerRun.from_dict(_mapping(entry.get("answer_run"), "case answer_run"))
            research_raw = entry.get("research_run")
            research_run = (
                ResearchRun.from_dict(_mapping(research_raw, "case research_run"))
                if research_raw is not None else None
            )
            cases.append(case)
            outputs[case.id] = (answer_run, research_run)
        return cls(data.get("schema_version", 0), tuple(cases), outputs)

    @classmethod
    def load(cls, path: str | Path) -> "EvaluationFixture":
        with open(path, encoding="utf-8") as source:
            payload = json.load(source)
        return cls.from_dict(payload)


class FixtureAgent:
    """The fixture-backed fake collaborator accepted by :class:`ChatEvaluator`."""

    def __init__(self, fixture: EvaluationFixture) -> None:
        if not isinstance(fixture, EvaluationFixture):
            raise ValueError("fixture agent requires an EvaluationFixture")
        self.fixture = fixture

    def run(self, case: EvaluationCase) -> tuple[AnswerRun, ResearchRun | None]:
        if not isinstance(case, EvaluationCase):
            raise ValueError("fixture agent requires an EvaluationCase")
        if case.id not in self.fixture.outputs:
            raise ValueError(f"fixture has no fixed output for case {case.id}")
        return self.fixture.outputs[case.id]


@dataclass(frozen=True)
class EvaluationResult:
    """Per-case observations and safe aggregate inputs; no source prose is retained."""

    case_id: str
    forbidden_report_ids_hit: tuple[str, ...] = ()
    disallowed_sources: tuple[str, ...] = ()
    missing_expected_fact_ids: tuple[str, ...] = ()
    scope_mode_matches: bool = True
    scope_company_mismatches: tuple[str, ...] = ()
    scope_report_mismatches: tuple[str, ...] = ()
    unsupported_numeric_claims: int = 0
    invalid_pdf_page_urls: int = 0
    external_facts_missing_source: int = 0
    external_facts_missing_as_of: int = 0
    completed_runs_with_failed_verifier: int = 0
    stopped_runs_rendered_complete: int = 0
    status_matches: bool = True
    citations_expected: int = 0
    citations_found: int = 0
    scope_checks: int = 0
    scope_matches: int = 0
    pdf_page_links_checked: int = 0
    pdf_page_links_passed: int = 0
    tool_calls: int = 0
    tool_successes: int = 0
    stop_recovery_checks: int = 0
    stop_recovery_passes: int = 0
    stage_durations: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        _text(self.case_id, "case_id")
        for name in (
            "forbidden_report_ids_hit", "disallowed_sources", "missing_expected_fact_ids",
            "scope_company_mismatches", "scope_report_mismatches",
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple):
                raise ValueError(f"{name} must be a tuple")
            _texts(values, name)
        for name in (
            "unsupported_numeric_claims", "invalid_pdf_page_urls", "external_facts_missing_source",
            "external_facts_missing_as_of", "completed_runs_with_failed_verifier",
            "stopped_runs_rendered_complete", "citations_expected", "citations_found", "scope_checks",
            "scope_matches", "pdf_page_links_checked", "pdf_page_links_passed", "tool_calls",
            "tool_successes", "stop_recovery_checks", "stop_recovery_passes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for numerator, denominator in (
            (self.citations_found, self.citations_expected),
            (self.scope_matches, self.scope_checks),
            (self.pdf_page_links_passed, self.pdf_page_links_checked),
            (self.tool_successes, self.tool_calls),
            (self.stop_recovery_passes, self.stop_recovery_checks),
        ):
            if numerator > denominator:
                raise ValueError("metric numerator must not exceed denominator")
        for name in ("status_matches", "scope_mode_matches"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if not isinstance(self.stage_durations, tuple):
            raise ValueError("stage_durations must be a tuple")
        for duration in self.stage_durations:
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not isfinite(duration) or duration < 0:
                raise ValueError("stage_durations must contain finite non-negative numbers")

    @property
    def citation_coverage(self) -> float:
        return _ratio(self.citations_found, self.citations_expected)

    @property
    def scope_precision(self) -> float:
        return _ratio(self.scope_matches, self.scope_checks)

    @property
    def page_link_pass_rate(self) -> float:
        return _ratio(self.pdf_page_links_passed, self.pdf_page_links_checked)


@dataclass(frozen=True)
class QualitySummary:
    passed: bool
    failures: tuple[str, ...]
    failure_codes: Mapping[str, int]
    case_count: int
    citation_coverage: float
    scope_precision: float
    page_link_pass_rate: float
    tool_success_rate: float
    stop_recovery_pass_rate: float
    p95_stage_duration: float

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise ValueError("passed must be a boolean")
        if not isinstance(self.failures, tuple) or not all(isinstance(item, str) for item in self.failures):
            raise ValueError("failures must be a tuple of strings")
        if not isinstance(self.failure_codes, Mapping):
            raise ValueError("failure_codes must be a mapping")
        unknown = set(self.failure_codes) - FAILURE_CODES
        if unknown:
            raise ValueError(f"failure_codes contains unsupported codes: {sorted(unknown)}")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in self.failure_codes.values()):
            raise ValueError("failure_codes counts must be non-negative integers")

    def to_dict(self) -> dict[str, Any]:
        """Serialize only safe counters, never case ids or raw failure prose."""
        return {
            "passed": self.passed,
            "case_count": self.case_count,
            "citation_coverage": self.citation_coverage,
            "scope_precision": self.scope_precision,
            "page_link_pass_rate": self.page_link_pass_rate,
            "tool_success_rate": self.tool_success_rate,
            "stop_recovery_pass_rate": self.stop_recovery_pass_rate,
            "p95_stage_duration": self.p95_stage_duration,
            "failure_codes": {
                code: self.failure_codes[code] for code in _FAILURE_ORDER if self.failure_codes.get(code)
            },
        }


def quality_summary_payload(
    health: QualitySummary, probe: QualitySummary, generated_at: str,
) -> dict[str, Any]:
    """Build the versioned, safe health/probe sidecar payload."""
    if not isinstance(health, QualitySummary) or not isinstance(probe, QualitySummary):
        raise ValueError("health and probe must be QualitySummary records")
    _text(generated_at, "generated_at")
    probe_payload = probe.to_dict()
    probe_payload["detected_failure_codes"] = probe_payload.pop("failure_codes")
    return {
        "schema_version": QUALITY_SUMMARY_SCHEMA_VERSION,
        "generated_at": generated_at,
        "health": health.to_dict(),
        "probe": probe_payload,
    }


def build_quality_summary(
    fixture: EvaluationFixture, generated_at: str | None = None,
) -> dict[str, Any]:
    """Replay fixture suites independently and return their safe aggregate summary."""
    if not isinstance(fixture, EvaluationFixture):
        raise ValueError("fixture must be an EvaluationFixture")
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).isoformat()
    _text(generated_at, "generated_at")
    evaluator = ChatEvaluator()
    health = QualityGate.evaluate(evaluator.run_suite(fixture, "health"))
    probe_results = evaluator.run_suite(fixture, "probe")
    detected = QualityGate.evaluate(probe_results)
    results_by_id = {result.case_id: result for result in probe_results}
    probes_passed = all(
        set(case.expected_failure_codes).issubset(
            QualityGate.evaluate([results_by_id[case.id]]).failure_codes
        )
        for case in fixture.cases
        if case.suite == "probe"
    )
    probe = QualitySummary(
        passed=probes_passed,
        failures=detected.failures,
        failure_codes=detected.failure_codes,
        case_count=detected.case_count,
        citation_coverage=detected.citation_coverage,
        scope_precision=detected.scope_precision,
        page_link_pass_rate=detected.page_link_pass_rate,
        tool_success_rate=detected.tool_success_rate,
        stop_recovery_pass_rate=detected.stop_recovery_pass_rate,
        p95_stage_duration=detected.p95_stage_duration,
    )
    return quality_summary_payload(health, probe, generated_at)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _artifact_fact_ids(run: AnswerRun) -> set[str]:
    evidence_ids = {identifier for fact in run.facts for identifier in fact.evidence_ids}
    for artifact in run.artifacts:
        evidence_ids.update(artifact_evidence_ids(artifact))
    evidence_ids.update(f"tool:{item.provider}:{item.tool_name}" for item in run.tool_artifacts)
    return evidence_ids


def _external_evidence_ids(run: AnswerRun) -> set[str]:
    """Identifiers an external fact may cite: persisted tool/web artifact identities."""
    identifiers = {f"tool:{item.provider}:{item.tool_name}" for item in run.tool_artifacts}
    for artifact in run.artifacts:
        if artifact.source == "web":
            identifiers.update(artifact_evidence_ids(artifact))
    return identifiers


def _used_sources(run: AnswerRun) -> set[str]:
    sources: set[str] = set()
    if any(item.source == "pdf" for item in run.artifacts):
        sources.add("local_pdf")
    if any(item.source == "web" for item in run.artifacts):
        sources.add("web")
    for item in run.tool_artifacts:
        sources.add("web" if "web" in item.provider.casefold() or "web" in item.tool_name.casefold() else "market_data")
    for fact in run.facts:
        if fact.source_type == "web":
            sources.add("web")
        elif fact.source_type == "tool":
            sources.add("market_data")
    return sources


def _duration_between(started_at: str, finished_at: str) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        duration = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
    except ValueError:
        return None
    return duration if duration >= 0 else None


class ChatEvaluator:
    """Run one controlled case against its fixed fixture output only."""

    def run_suite(self, fixture: EvaluationFixture, suite: Literal["health", "probe"]) -> tuple[EvaluationResult, ...]:
        """Replay exactly one named suite against its fixed offline fixture."""
        if not isinstance(fixture, EvaluationFixture):
            raise ValueError("fixture must be an EvaluationFixture")
        if suite not in _SUITE_NAMES:
            raise ValueError("suite must be health or probe")
        agent = FixtureAgent(fixture)
        return tuple(self.run(case, agent) for case in fixture.cases if case.suite == suite)

    def run(self, case: EvaluationCase, fixture_agent: FixtureAgent) -> EvaluationResult:
        if not isinstance(case, EvaluationCase):
            raise ValueError("case must be an EvaluationCase")
        if not isinstance(fixture_agent, FixtureAgent):
            raise ValueError("evaluation must use a fixture-backed fake agent")
        answer_run, research_run = fixture_agent.run(case)
        found_ids = _artifact_fact_ids(answer_run)
        forbidden = self._forbidden_report_ids(answer_run, research_run, case.forbidden_report_ids)
        missing = tuple(identifier for identifier in case.expected_fact_ids if identifier not in found_ids)
        invalid_urls = sum(
            item.source == "pdf" and bool(item.pdf_url) and validated_pdf_url(item) is None
            for item in answer_run.artifacts
        )
        external_missing_source, external_missing_as_of = self._external_fact_violations(answer_run)
        scoped = self._scope_boundary(answer_run, case)
        report = answer_run.verification_report
        unsupported = sum(issue.code == "unsupported_numeric_claim" for issue in (report.issues if report else ()))
        completed_failed = int(answer_run.status == "completed" and (report is None or report.status != "passed"))
        stopped_rendered_complete = int(
            research_run is not None and research_run.status == "stopped" and answer_run.status == "completed"
        )
        stop_checks = int(research_run is not None and research_run.status in {"stopped", "partial", "failed"})
        durations = self._stage_durations(answer_run, research_run)
        expected = len(case.expected_fact_ids)
        scope_checks = 3 + len(case.forbidden_report_ids)
        scope_matches = sum((
            scoped["scope_mode_matches"],
            not scoped["scope_company_mismatches"],
            not scoped["scope_report_mismatches"],
            len(case.forbidden_report_ids) - len(forbidden),
        ))
        return EvaluationResult(
            case_id=case.id,
            forbidden_report_ids_hit=forbidden,
            disallowed_sources=tuple(sorted(_used_sources(answer_run) - set(case.allowed_sources))),
            missing_expected_fact_ids=missing,
            unsupported_numeric_claims=unsupported,
            invalid_pdf_page_urls=invalid_urls,
            external_facts_missing_source=external_missing_source,
            external_facts_missing_as_of=external_missing_as_of,
            completed_runs_with_failed_verifier=completed_failed,
            stopped_runs_rendered_complete=stopped_rendered_complete,
            status_matches=answer_run.status == case.expected_status,
            citations_expected=expected,
            citations_found=expected - len(missing),
            scope_checks=scope_checks,
            scope_matches=scope_matches,
            pdf_page_links_checked=sum(item.source == "pdf" and bool(item.pdf_url) for item in answer_run.artifacts),
            pdf_page_links_passed=sum(
                item.source == "pdf" and bool(item.pdf_url) and validated_pdf_url(item) is not None
                for item in answer_run.artifacts
            ),
            tool_calls=len(answer_run.tool_artifacts),
            tool_successes=sum(item.status == "success" for item in answer_run.tool_artifacts),
            stop_recovery_checks=stop_checks,
            stop_recovery_passes=stop_checks - stopped_rendered_complete,
            stage_durations=durations,
            **scoped,
        )

    @staticmethod
    def _scope_boundary(answer: AnswerRun, case: EvaluationCase) -> dict[str, Any]:
        """Compare the run's actual Scope company/report boundary with the case."""
        scope = answer.scope
        company_codes = tuple(company.code for company in scope.companies) if scope else ()
        report_ids = tuple(scope.report_ids) if scope else ()
        return {
            "scope_mode_matches": scope is not None and scope.mode == case.scope,
            "scope_company_mismatches": tuple(sorted(set(case.expected_company_codes) ^ set(company_codes))),
            "scope_report_mismatches": tuple(sorted(set(case.expected_report_ids) ^ set(report_ids))),
        }

    @staticmethod
    def _forbidden_report_ids(answer: AnswerRun, research: ResearchRun | None, forbidden: Sequence[str]) -> tuple[str, ...]:
        seen = set(answer.retrieval_report_ids)
        if answer.scope:
            seen.update(answer.scope.report_ids)
        seen.update(item.report_id for item in answer.artifacts if item.report_id)
        if research:
            seen.update(research.plan.scope.report_ids)
            for step in research.plan.steps:
                seen.update(step.report_ids)
        return tuple(identifier for identifier in forbidden if identifier in seen)

    @staticmethod
    def _external_fact_violations(facts_run: AnswerRun) -> tuple[int, int]:
        """External facts must cite a persisted tool/web artifact and carry ``as_of``."""
        mapped = _external_evidence_ids(facts_run)
        external = (fact for fact in facts_run.facts if fact.source_type in {"tool", "web"})
        missing_source = missing_as_of = 0
        for fact in external:
            if not fact.evidence_ids or any(identifier not in mapped for identifier in fact.evidence_ids):
                missing_source += 1
            if not fact.as_of:
                missing_as_of += 1
        return missing_source, missing_as_of

    @staticmethod
    def _stage_durations(answer: AnswerRun, research: ResearchRun | None) -> tuple[float, ...]:
        durations: list[float] = []
        if answer.elapsed_seconds is not None:
            durations.append(float(answer.elapsed_seconds))
        if research:
            for step in research.step_runs:
                duration = _duration_between(step.started_at, step.finished_at)
                if duration is not None:
                    durations.append(duration)
        return tuple(durations)


class QualityGate:
    """Fail closed on trust-contract violations and report aggregate safe metrics."""

    @staticmethod
    def evaluate(results: Sequence[EvaluationResult]) -> QualitySummary:
        if not isinstance(results, (list, tuple)) or not all(isinstance(item, EvaluationResult) for item in results):
            raise ValueError("results must be EvaluationResult records")
        failures: list[str] = []
        counts = {code: 0 for code in _FAILURE_ORDER}
        durations: list[float] = []
        totals = {
            "citations_expected": 0, "citations_found": 0, "scope_checks": 0, "scope_matches": 0,
            "pdf_page_links_checked": 0, "pdf_page_links_passed": 0, "tool_calls": 0,
            "tool_successes": 0, "stop_recovery_checks": 0, "stop_recovery_passes": 0,
        }
        for result in results:
            for name in totals:
                totals[name] += getattr(result, name)
            durations.extend(result.stage_durations)

            def fail(code: str, detail: str) -> None:
                counts[code] += 1
                failures.append(f"{result.case_id}: {detail}")

            if result.forbidden_report_ids_hit:
                fail("scope_leak", f"scope leak ({', '.join(result.forbidden_report_ids_hit)})")
            if not result.scope_mode_matches or result.scope_company_mismatches or result.scope_report_mismatches:
                fail("scope_boundary_mismatch", "scope boundary does not match the case")
            if result.disallowed_sources:
                fail("source_outside_policy", "source outside allowed policy")
            if result.missing_expected_fact_ids:
                fail("expected_citation_missing", "expected citations missing")
            if result.unsupported_numeric_claims:
                fail("unsupported_numeric_claim", "unsupported numeric claim")
            if result.invalid_pdf_page_urls:
                fail("invalid_pdf_page_url", "invalid PDF page URL")
            if result.external_facts_missing_source:
                fail("external_fact_missing_source", "external fact without a tool/web artifact")
            if result.external_facts_missing_as_of:
                fail("external_fact_missing_as_of", "external fact without as_of")
            if result.completed_runs_with_failed_verifier:
                fail("completed_run_with_failed_verifier", "completed run with failed verifier")
            if result.stopped_runs_rendered_complete:
                fail("stopped_run_rendered_complete", "stopped run rendered complete")
            if not result.status_matches:
                fail("run_status_mismatch", "run status does not match expected outcome")
        return QualitySummary(
            passed=not failures,
            failures=tuple(failures),
            failure_codes=MappingProxyType({code: count for code, count in counts.items() if count}),
            case_count=len(results),
            citation_coverage=_ratio(totals["citations_found"], totals["citations_expected"]),
            scope_precision=_ratio(totals["scope_matches"], totals["scope_checks"]),
            page_link_pass_rate=_ratio(totals["pdf_page_links_passed"], totals["pdf_page_links_checked"]),
            tool_success_rate=_ratio(totals["tool_successes"], totals["tool_calls"]),
            stop_recovery_pass_rate=_ratio(totals["stop_recovery_passes"], totals["stop_recovery_checks"]),
            p95_stage_duration=_p95(durations),
        )


def _p95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[ceil(len(ordered) * 0.95) - 1])
