"""Deterministic M4 quality evaluation uses only fixed local AnswerRun fixtures."""

import json
from pathlib import Path

import pytest

from webapp.chat_models import (
    AnswerRun,
    EvidenceArtifact,
    Fact,
    Scope,
    ToolArtifact,
    VerificationReport,
)
from webapp.research_models import ResearchPlan, ResearchRun, ResearchStep, ResearchStepRun


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "chat_eval_cases.json"


def _scope() -> Scope:
    return Scope.company_only("601288", "农业银行", ("601288:2026-06-30:semi_annual",))


def _fact(*, external: bool = False) -> Fact:
    return Fact(
        "price" if external else "revenue", 3.2 if external else 100.0,
        "元/股" if external else "亿元", "as_of" if external else "2026-06-30",
        "point_in_time" if external else "semi_annual_cumulative", "consolidated",
        "601288", "tool" if external else "pdf",
        ("tool:market:quote",) if external else ("pdf-1",),
        "reference" if external else "verified",
        "2026-09-16T10:00:00+08:00" if external else "",
    )


def _run(*, status: str = "completed", verification: str = "passed", artifacts=(), facts=(), tools=(), retrieval=()) -> AnswerRun:
    return AnswerRun(
        id="fixed-answer", content="固定回答", status=status, scope=_scope(),
        facts=tuple(facts), artifacts=tuple(artifacts), tool_artifacts=tuple(tools),
        retrieval_report_ids=tuple(retrieval),
        verification_report=VerificationReport(verification, supported_fact_ids=("pdf-1",)),
        elapsed_seconds=1.5,
    )


class _FixedFakeAgent:
    """Fixture-only collaborator: its return values contain no model or network calls."""

    def __init__(self, outputs):
        self.outputs = outputs

    def run(self, case):
        return self.outputs[case.id]


def test_case_schema_requires_scope_and_expected_outcome():
    from webapp.chat_evaluation import EvaluationCase

    with pytest.raises(ValueError, match="expected_status"):
        EvaluationCase.from_dict({"id": "bad", "question": "x"})


def test_controlled_fixture_covers_all_required_scenarios():
    from webapp.chat_evaluation import EvaluationCase

    cases = [EvaluationCase.from_dict(raw) for raw in json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))]

    assert {case.id for case in cases} >= {
        "report-number", "cross-period", "local-industry", "realtime-market",
        "news-attribution", "scope-leak", "pdf-without-page", "tool-failure",
        "fact-conflict", "stop-recovery",
    }


def test_evaluator_runs_fixed_answer_and_research_fixtures_without_real_collaborators():
    from webapp.chat_evaluation import ChatEvaluator, EvaluationCase, QualityGate

    case = EvaluationCase.from_dict({
        "id": "report-number", "question": "营收是多少？", "scope": "company_only",
        "intent": "report_fact", "allowed_sources": ["local_pdf"],
        "expected_fact_ids": ["pdf-1"], "forbidden_report_ids": [],
        "expected_status": "completed",
    })
    artifact = EvidenceArtifact.pdf(
        "601288:2026-06-30:semi_annual", "report.pdf", 40, "营收 100 亿元",
        pdf_url="/api/history-pdf/report.pdf#page=40",
    )
    plan = ResearchPlan("核验", _scope(), (ResearchStep("verify", "verify", "核验"),), ("有证据",))
    research = ResearchRun("fixed-research", plan, "completed", (ResearchStepRun("verify", "completed"),))
    result = ChatEvaluator().run(case, _FixedFakeAgent({
        case.id: (_run(artifacts=(artifact,), facts=(_fact(),)), research),
    }))

    assert result.case_id == case.id
    assert result.citation_coverage == 1.0
    assert result.scope_precision == 1.0
    assert result.page_link_pass_rate == 1.0
    assert result.status_matches is True
    assert QualityGate.evaluate([result]).passed is True


def test_scope_leak_fails_quality_gate():
    from webapp.chat_evaluation import EvaluationResult, QualityGate

    result = EvaluationResult(case_id="company-only", forbidden_report_ids_hit=("600900:2026-06-30:semi_annual",))
    assert QualityGate.evaluate([result]).passed is False


def test_external_fact_without_as_of_fails_quality_gate():
    from webapp.chat_evaluation import EvaluationResult, QualityGate

    result = EvaluationResult(case_id="market", external_facts_missing_as_of=1)
    assert QualityGate.evaluate([result]).passed is False


@pytest.mark.parametrize(
    ("result", "failure"),
    [
        (lambda: __import__("webapp.chat_evaluation", fromlist=["EvaluationResult"]).EvaluationResult(
            case_id="numeric", unsupported_numeric_claims=1), "unsupported numeric"),
        (lambda: __import__("webapp.chat_evaluation", fromlist=["EvaluationResult"]).EvaluationResult(
            case_id="pdf", invalid_pdf_page_urls=1), "invalid PDF"),
        (lambda: __import__("webapp.chat_evaluation", fromlist=["EvaluationResult"]).EvaluationResult(
            case_id="verifier", completed_runs_with_failed_verifier=1), "failed verifier"),
        (lambda: __import__("webapp.chat_evaluation", fromlist=["EvaluationResult"]).EvaluationResult(
            case_id="stopped", stopped_runs_rendered_complete=1), "rendered complete"),
    ],
)
def test_gate_rejects_every_required_failure(result, failure):
    from webapp.chat_evaluation import QualityGate

    assert failure in " ".join(QualityGate.evaluate([result()]).failures)


def test_quality_summary_reports_weighted_metrics_and_p95_stage_duration():
    from webapp.chat_evaluation import EvaluationResult, QualityGate

    summary = QualityGate.evaluate([
        EvaluationResult(
            case_id="one", citations_expected=2, citations_found=1,
            scope_checks=2, scope_matches=2, pdf_page_links_checked=1,
            pdf_page_links_passed=1, tool_calls=2, tool_successes=1,
            stop_recovery_checks=1, stop_recovery_passes=1, stage_durations=(1.0, 2.0),
        ),
        EvaluationResult(
            case_id="two", citations_expected=2, citations_found=2,
            scope_checks=2, scope_matches=1, pdf_page_links_checked=1,
            pdf_page_links_passed=0, tool_calls=1, tool_successes=1,
            stop_recovery_checks=1, stop_recovery_passes=0, stage_durations=(3.0, 100.0),
        ),
    ])

    assert summary.citation_coverage == 0.75
    assert summary.scope_precision == 0.75
    assert summary.page_link_pass_rate == 0.5
    assert summary.tool_success_rate == pytest.approx(2 / 3)
    assert summary.stop_recovery_pass_rate == 0.5
    assert summary.p95_stage_duration == 100.0
