"""Deterministic M4 quality evaluation replays only fixed, versioned fixtures."""

import copy
import json
from pathlib import Path

import pytest

from scripts.run_chat_evaluation import main as quality_command_main
from webapp.chat_evaluation import (
    FAILURE_CODES,
    FIXTURE_SCHEMA_VERSION,
    ChatEvaluator,
    EvaluationCase,
    EvaluationFixture,
    EvaluationResult,
    FixtureAgent,
    QualityGate,
    build_quality_summary,
    quality_summary_payload,
)


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "chat_eval_cases.json"
REQUIRED_CASE_IDS = {
    "report-number", "cross-period", "local-industry", "realtime-market",
    "news-attribution", "scope-leak", "pdf-without-page", "tool-failure",
    "fact-conflict", "stop-recovery",
}
SEMI_REPORT_ID = "601288:2026-06-30:semi_annual"
PEER_REPORT_ID = "600036:2026-06-30:semi_annual"
LEAK_REPORT_ID = "600900:2026-06-30:semi_annual"


def _payload() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _fixture_with(case_id: str, **case_overrides) -> EvaluationFixture:
    payload = _payload()
    for entry in payload["cases"]:
        if entry["id"] == case_id:
            entry.update(case_overrides)
            break
    else:  # pragma: no cover - fixture always contains the requested case
        raise AssertionError(f"fixture has no case {case_id}")
    return EvaluationFixture.from_dict(payload)


def _fixture_outputs(fixture: EvaluationFixture) -> list[EvaluationResult]:
    evaluator = ChatEvaluator()
    agent = FixtureAgent(fixture)
    return [evaluator.run(case, agent) for case in fixture.cases]


def test_case_schema_requires_scope_and_expected_outcome():
    with pytest.raises(ValueError, match="expected_status"):
        EvaluationCase.from_dict({"id": "bad", "question": "x"})


def test_fixture_is_versioned_and_carries_one_fixed_serialized_output_per_case():
    raw = _payload()
    fixture = EvaluationFixture.load(FIXTURE_PATH)

    assert raw["schema_version"] == fixture.schema_version == FIXTURE_SCHEMA_VERSION == 2
    assert {case.id for case in fixture.cases} >= REQUIRED_CASE_IDS
    assert {case.suite for case in fixture.cases} == {"health", "probe"}
    assert set(fixture.outputs) == {case.id for case in fixture.cases}
    for case in fixture.cases:
        answer_run, research_run = fixture.outputs[case.id]
        assert answer_run.id
        assert answer_run.scope is not None
        assert FixtureAgent(fixture).run(case) is fixture.outputs[case.id]
        if case.id == "stop-recovery":
            assert research_run is not None and research_run.status == "stopped"
        else:
            assert research_run is None
    serialized = json.dumps(raw, ensure_ascii=False)
    for internal in ("prompt", "reasoning", "chain_of_thought", "api_key"):
        assert internal not in serialized


def test_fixture_requires_a_suite_for_each_case():
    payload = _payload()
    payload["cases"][0].pop("suite")
    with pytest.raises(ValueError, match="suite"):
        EvaluationFixture.from_dict(payload)


def test_fixture_requires_a_supported_schema_version_and_fixed_outputs():
    payload = _payload()
    payload.pop("schema_version")
    with pytest.raises(ValueError, match="schema_version"):
        EvaluationFixture.from_dict(payload)

    payload = _payload()
    payload["schema_version"] = 99
    with pytest.raises(ValueError, match="unsupported fixture schema_version"):
        EvaluationFixture.from_dict(payload)

    payload = _payload()
    payload["cases"][0].pop("answer_run")
    with pytest.raises(ValueError, match="fixed output"):
        EvaluationFixture.from_dict(payload)

    payload = _payload()
    payload["cases"][0]["unexpected"] = True
    with pytest.raises(ValueError, match="unsupported keys"):
        EvaluationFixture.from_dict(payload)


def test_evaluator_rejects_any_non_fixture_collaborator():
    """A production evaluation must never accept an arbitrary (possibly online) fake."""
    fixture = EvaluationFixture.load(FIXTURE_PATH)
    case = fixture.cases[0]
    answer_run = fixture.outputs[case.id][0]

    with pytest.raises(ValueError, match="fixture-backed"):
        ChatEvaluator().run(case, lambda _case: answer_run)
    with pytest.raises(ValueError, match="fixture-backed"):
        ChatEvaluator().run(case, type("FakeAgent", (), {"run": lambda self, _case: answer_run})())
    with pytest.raises(ValueError, match="EvaluationFixture"):
        FixtureAgent({"report-number": (answer_run, None)})
    with pytest.raises(ValueError, match="fixture-backed"):
        ChatEvaluator().run(case, {"report-number": (answer_run, None)})
    with pytest.raises(ValueError, match="EvaluationCase"):
        ChatEvaluator().run({"id": "report-number"}, FixtureAgent(fixture))


def test_evaluator_reuses_shared_pdf_url_validation_for_unsafe_urls():
    import webapp.chat_evaluation as chat_evaluation
    from webapp.evidence_identity import validated_pdf_url

    assert chat_evaluation.validated_pdf_url is validated_pdf_url


def test_health_summary_can_pass_while_probe_records_expected_detections():
    fixture = EvaluationFixture.load(FIXTURE_PATH)
    results = {result.case_id: result for result in _fixture_outputs(fixture)}

    assert results["report-number"].status_matches is True
    assert results["report-number"].citation_coverage == 1.0
    assert results["scope-leak"].forbidden_report_ids_hit == (LEAK_REPORT_ID,)
    assert results["pdf-without-page"].pdf_page_links_checked == 0
    assert results["pdf-without-page"].pdf_page_links_passed == 0
    assert results["tool-failure"].tool_calls == 1
    assert results["tool-failure"].tool_successes == 0
    assert results["stop-recovery"].stop_recovery_checks == 1
    assert results["stop-recovery"].stop_recovery_passes == 1
    assert results["realtime-market"].disallowed_sources == ()
    assert results["news-attribution"].disallowed_sources == ()

    payload = build_quality_summary(fixture, generated_at="2026-09-17T00:00:00+00:00")
    assert payload["health"]["passed"] is True
    assert payload["probe"]["passed"] is True
    assert payload["probe"]["detected_failure_codes"] == {"scope_leak": 1}
    assert "scope-leak" not in json.dumps(payload, ensure_ascii=False)


def test_probe_fails_when_a_declared_failure_is_not_detected():
    payload = _payload()
    scope_leak = next(case for case in payload["cases"] if case["id"] == "scope-leak")
    scope_leak["expected_failure_codes"].append("invalid_pdf_page_url")

    summary = build_quality_summary(
        EvaluationFixture.from_dict(payload), generated_at="2026-09-17T00:00:00+00:00",
    )

    assert summary["health"]["passed"] is True
    assert summary["probe"]["passed"] is False
    assert summary["probe"]["detected_failure_codes"] == {"scope_leak": 1}


def test_scope_boundary_mismatch_fails_the_gate():
    """An answer whose actual Scope boundary differs from the case must fail closed."""
    fixture = _fixture_with("report-number", expected_company_codes=[])
    case = next(candidate for candidate in fixture.cases if candidate.id == "report-number")

    result = ChatEvaluator().run(case, FixtureAgent(fixture))
    summary = QualityGate.evaluate([result])

    assert result.scope_company_mismatches == ("601288",)
    assert result.scope_mode_matches is True
    assert summary.passed is False
    assert summary.failure_codes["scope_boundary_mismatch"] == 1

    report_fixture = _fixture_with("report-number", expected_report_ids=[PEER_REPORT_ID])
    report_case = next(candidate for candidate in report_fixture.cases if candidate.id == "report-number")
    report_result = ChatEvaluator().run(report_case, FixtureAgent(report_fixture))

    assert report_result.scope_report_mismatches == tuple(sorted({PEER_REPORT_ID, SEMI_REPORT_ID}))
    assert QualityGate.evaluate([report_result]).failure_codes["scope_boundary_mismatch"] == 1


def test_external_fact_evidence_must_map_to_a_persisted_tool_or_web_artifact():
    fixture = _fixture_with("realtime-market")
    entry = next(case for case in fixture.cases if case.id == "realtime-market")
    payload = copy.deepcopy(_payload())
    for raw in payload["cases"]:
        if raw["id"] == "realtime-market":
            raw["answer_run"]["facts"][0]["evidence_ids"] = ["tool:not-persisted:quote"]
    unbacked = EvaluationFixture.from_dict(payload)

    result = ChatEvaluator().run(entry, FixtureAgent(unbacked))
    summary = QualityGate.evaluate([result])

    assert result.external_facts_missing_source == 1
    assert result.external_facts_missing_as_of == 0
    assert summary.passed is False
    assert summary.failure_codes["external_fact_missing_source"] == 1
    assert FixtureAgent(fixture).run(entry)[0].facts[0].evidence_ids == ("tool:market:quote",)


def test_external_web_identity_uses_shared_artifact_identity(monkeypatch):
    """External web-fact validation must use the shared artifact identity rule."""
    import webapp.chat_evaluation as chat_evaluation

    payload = _payload()
    market_fact = next(
        raw["answer_run"]["facts"][0]
        for raw in payload["cases"]
        if raw["id"] == "realtime-market"
    )
    web_fact = copy.deepcopy(market_fact)
    web_fact.update({
        "metric": "网页归因",
        "source_type": "web",
        "evidence_ids": ["shared:web-identity"],
    })
    news_entry = next(raw for raw in payload["cases"] if raw["id"] == "news-attribution")
    news_entry["answer_run"]["facts"].append(web_fact)
    fixture = EvaluationFixture.from_dict(payload)
    case = next(candidate for candidate in fixture.cases if candidate.id == "news-attribution")
    calls = []

    def shared_identity(artifact):
        calls.append(artifact)
        return ("shared:web-identity",)

    monkeypatch.setattr(chat_evaluation, "artifact_evidence_ids", shared_identity)

    result = ChatEvaluator().run(case, FixtureAgent(fixture))

    assert result.external_facts_missing_source == 0
    assert calls
    assert all(artifact.source == "web" for artifact in calls)


def test_quality_summary_payload_exposes_only_fixed_codes_and_metrics():
    fixture = EvaluationFixture.load(FIXTURE_PATH)
    payload = build_quality_summary(fixture, generated_at="2026-09-17T00:00:00+00:00")

    assert set(payload) == {"schema_version", "generated_at", "health", "probe"}
    assert payload["health"]["failure_codes"] == {}
    assert payload["probe"]["detected_failure_codes"]["scope_leak"] == 1
    assert set(payload["probe"]["detected_failure_codes"]) <= FAILURE_CODES
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "scope leak" not in serialized
    for case_id in REQUIRED_CASE_IDS:
        assert case_id not in serialized


def test_quality_command_failure_does_not_replace_existing_summary(tmp_path):
    output = tmp_path / "research_quality_summary.json"
    previous = '{"schema_version": 2, "health": {"passed": true}}'
    output.write_text(previous, encoding="utf-8")

    assert quality_command_main(["--fixture", str(tmp_path / "missing.json"), "--output", str(output)]) != 0
    assert output.read_text(encoding="utf-8") == previous


def test_quality_command_writes_a_safe_summary_atomically(tmp_path):
    output = tmp_path / "research_quality_summary.json"

    assert quality_command_main(["--fixture", str(FIXTURE_PATH), "--output", str(output)]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["health"]["passed"] is True
    assert payload["probe"]["passed"] is True
    assert not (tmp_path / "research_quality_summary.json.tmp").exists()


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (EvaluationResult(case_id="numeric", unsupported_numeric_claims=1), "unsupported_numeric_claim"),
        (EvaluationResult(case_id="pdf", invalid_pdf_page_urls=1), "invalid_pdf_page_url"),
        (EvaluationResult(case_id="verifier", completed_runs_with_failed_verifier=1), "completed_run_with_failed_verifier"),
        (EvaluationResult(case_id="stopped", stopped_runs_rendered_complete=1), "stopped_run_rendered_complete"),
        (EvaluationResult(case_id="status", status_matches=False), "run_status_mismatch"),
        (EvaluationResult(case_id="source", disallowed_sources=("web",)), "source_outside_policy"),
        (EvaluationResult(case_id="citation", missing_expected_fact_ids=("pdf-1",)), "expected_citation_missing"),
        (EvaluationResult(case_id="asof", external_facts_missing_as_of=1), "external_fact_missing_as_of"),
        (EvaluationResult(case_id="leak", forbidden_report_ids_hit=("600900:2026-06-30:semi_annual",)), "scope_leak"),
    ],
)
def test_gate_rejects_every_required_failure(result, code):
    summary = QualityGate.evaluate([result])

    assert summary.passed is False
    assert summary.failure_codes[code] == 1
    assert code in FAILURE_CODES
    assert summary.failures


def test_quality_summary_reports_weighted_metrics_and_p95_stage_duration():
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
