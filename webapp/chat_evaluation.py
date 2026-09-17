"""Offline, deterministic quality evaluation for immutable trusted-chat runs.

This module is deliberately a contract evaluator rather than an agent runner.  A
caller supplies a fixture-only collaborator that returns already-created
:class:`AnswerRun` and optional :class:`ResearchRun` records; no model, MCP, web,
or production-data client is imported or invoked here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil, isfinite
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlparse

from webapp.chat_models import AnswerRun, EvidenceArtifact, Fact, ToolArtifact
from webapp.research_models import ResearchRun


class FixedAgent(Protocol):
    """Fixture collaborator accepted by :class:`ChatEvaluator`."""

    def run(self, case: "EvaluationCase") -> Any:
        """Return an ``AnswerRun`` or ``(AnswerRun, ResearchRun | None)`` fixture."""


_CASE_FIELDS = frozenset((
    "id", "question", "scope", "intent", "allowed_sources",
    "expected_fact_ids", "forbidden_report_ids", "expected_status",
))
_ALLOWED_SOURCES = frozenset(("local_pdf", "market_data", "web"))


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _texts(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an array")
    return tuple(_text(item, f"{name}[]") for item in value)


@dataclass(frozen=True)
class EvaluationCase:
    """Versionable input/output expectations for one controlled question."""

    id: str
    question: str
    scope: str
    intent: str
    allowed_sources: tuple[str, ...]
    expected_fact_ids: tuple[str, ...]
    forbidden_report_ids: tuple[str, ...]
    expected_status: str

    def __post_init__(self) -> None:
        for name in ("id", "question", "scope", "intent", "expected_status"):
            _text(getattr(self, name), name)
        for name in ("allowed_sources", "expected_fact_ids", "forbidden_report_ids"):
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
            question=_text(value.get("question"), "question"),
            scope=_text(value.get("scope"), "scope"),
            intent=_text(value.get("intent"), "intent"),
            allowed_sources=_texts(value.get("allowed_sources"), "allowed_sources"),
            expected_fact_ids=_texts(value.get("expected_fact_ids"), "expected_fact_ids"),
            forbidden_report_ids=_texts(value.get("forbidden_report_ids"), "forbidden_report_ids"),
            expected_status=_text(value.get("expected_status"), "expected_status"),
        )


@dataclass(frozen=True)
class EvaluationResult:
    """Per-case observations and safe aggregate inputs; no source prose is retained."""

    case_id: str
    forbidden_report_ids_hit: tuple[str, ...] = ()
    disallowed_sources: tuple[str, ...] = ()
    missing_expected_fact_ids: tuple[str, ...] = ()
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
        for name in ("forbidden_report_ids_hit", "disallowed_sources", "missing_expected_fact_ids"):
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
        if not isinstance(self.status_matches, bool):
            raise ValueError("status_matches must be a boolean")
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
    case_count: int
    citation_coverage: float
    scope_precision: float
    page_link_pass_rate: float
    tool_success_rate: float
    stop_recovery_pass_rate: float
    p95_stage_duration: float


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _artifact_fact_ids(run: AnswerRun) -> set[str]:
    evidence_ids = {identifier for fact in run.facts for identifier in fact.evidence_ids}
    evidence_ids.update(
        f"{artifact.report_id}#p{artifact.page}"
        for artifact in run.artifacts
        if artifact.source == "pdf" and artifact.report_id and artifact.page is not None
    )
    evidence_ids.update(f"tool:{item.provider}:{item.tool_name}" for item in run.tool_artifacts)
    return evidence_ids


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


def _invalid_pdf_url(artifact: EvidenceArtifact) -> bool:
    """Accept only the existing local page route with a matching page fragment."""
    if artifact.source != "pdf" or not artifact.pdf_url:
        return False
    parsed = urlparse(artifact.pdf_url)
    return not (
        not parsed.scheme
        and not parsed.netloc
        and parsed.path.startswith("/api/history-pdf/")
        and parsed.fragment == f"page={artifact.page}"
    )


def _duration_between(started_at: str, finished_at: str) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        duration = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
    except ValueError:
        return None
    return duration if duration >= 0 else None


class ChatEvaluator:
    """Run a case against fixed in-memory AnswerRun/ResearchRun fixtures only."""

    def run(self, case: EvaluationCase, fake_agent: FixedAgent | Callable[[EvaluationCase], Any]) -> EvaluationResult:
        if not isinstance(case, EvaluationCase):
            raise ValueError("case must be an EvaluationCase")
        output = fake_agent.run(case) if hasattr(fake_agent, "run") else fake_agent(case)
        answer_run, research_run = self._fixture_output(output)
        found_ids = _artifact_fact_ids(answer_run)
        forbidden = self._forbidden_report_ids(answer_run, research_run, case.forbidden_report_ids)
        missing = tuple(identifier for identifier in case.expected_fact_ids if identifier not in found_ids)
        invalid_urls = sum(_invalid_pdf_url(item) for item in answer_run.artifacts)
        external_missing_source, external_missing_as_of = self._external_fact_violations(answer_run.facts)
        report = answer_run.verification_report
        unsupported = sum(issue.code == "unsupported_numeric_claim" for issue in (report.issues if report else ()))
        completed_failed = int(answer_run.status == "completed" and (report is None or report.status != "passed"))
        stopped_rendered_complete = int(
            research_run is not None and research_run.status == "stopped" and answer_run.status == "completed"
        )
        stop_checks = int(research_run is not None and research_run.status in {"stopped", "partial", "failed"})
        durations = self._stage_durations(answer_run, research_run)
        expected = len(case.expected_fact_ids)
        scope_checks = len(case.forbidden_report_ids)
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
            scope_matches=scope_checks - len(forbidden),
            pdf_page_links_checked=sum(item.source == "pdf" and bool(item.pdf_url) for item in answer_run.artifacts),
            pdf_page_links_passed=sum(
                item.source == "pdf" and bool(item.pdf_url) and not _invalid_pdf_url(item)
                for item in answer_run.artifacts
            ),
            tool_calls=len(answer_run.tool_artifacts),
            tool_successes=sum(item.status == "success" for item in answer_run.tool_artifacts),
            stop_recovery_checks=stop_checks,
            stop_recovery_passes=stop_checks - stopped_rendered_complete,
            stage_durations=durations,
        )

    @staticmethod
    def _fixture_output(output: Any) -> tuple[AnswerRun, ResearchRun | None]:
        if isinstance(output, AnswerRun):
            return output, None
        if isinstance(output, tuple) and len(output) == 2 and isinstance(output[0], AnswerRun):
            research = output[1]
            if research is not None and not isinstance(research, ResearchRun):
                raise ValueError("fixture research run must be a ResearchRun or null")
            return output[0], research
        raise ValueError("fake_agent must return AnswerRun or (AnswerRun, ResearchRun | None) fixtures")

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
    def _external_fact_violations(facts: Sequence[Fact]) -> tuple[int, int]:
        external = (fact for fact in facts if fact.source_type in {"tool", "web"})
        missing_source = missing_as_of = 0
        for fact in external:
            if not fact.evidence_ids:
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
            if result.forbidden_report_ids_hit:
                failures.append(f"{result.case_id}: scope leak ({', '.join(result.forbidden_report_ids_hit)})")
            if result.disallowed_sources:
                failures.append(f"{result.case_id}: source outside allowed policy")
            if result.missing_expected_fact_ids:
                failures.append(f"{result.case_id}: expected citations missing")
            if result.unsupported_numeric_claims:
                failures.append(f"{result.case_id}: unsupported numeric claim")
            if result.invalid_pdf_page_urls:
                failures.append(f"{result.case_id}: invalid PDF page URL")
            if result.external_facts_missing_source or result.external_facts_missing_as_of:
                failures.append(f"{result.case_id}: external fact missing source/as_of")
            if result.completed_runs_with_failed_verifier:
                failures.append(f"{result.case_id}: completed run with failed verifier")
            if result.stopped_runs_rendered_complete:
                failures.append(f"{result.case_id}: stopped run rendered complete")
            if not result.status_matches:
                failures.append(f"{result.case_id}: run status does not match expected outcome")
        return QualitySummary(
            passed=not failures,
            failures=tuple(failures),
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
