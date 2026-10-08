import json
from pathlib import Path

import pytest

from webapp.chat_runtime_eval_cases import RuntimeSuite
from webapp.chat_runtime_eval_report import build_report, compare, score, write_report
from webapp.chat_runtime_eval_runner import RuntimeObservation

SUITE = Path(__file__).resolve().parents[1] / "fixtures/chat_runtime_eval_cases.json"


def _case(case_id: str):
    return next(case for case in RuntimeSuite.load(SUITE).cases if case.id == case_id)


def _observation(**overrides):
    base = dict(
        events=("session", "run_started", "done"), persisted_status="completed", answer="固定回答",
        session_id="s1", run_id="r1", call_attempts={"model": 1, "retrieval": 1},
        first_frame_seconds=None, first_content_seconds=None, total_seconds=0.1,
        usage=None, facts=(), artifacts=(), terminal={},
    )
    base.update(overrides)
    return RuntimeObservation(**base)


def test_evidence_exists_but_wrong_period_is_blocking():
    case = _case("report-number-after-300")
    observed = _observation(facts=(
        {"metric": "revenue", "value": 100.0, "unit": "亿元", "period": "2025-12-31",
         "company_code": "601288", "evidence_ids": ["601288:2026-06-30:semi_annual#p40"]},
    ), artifacts=(
        {"source": "pdf", "report_id": "601288:2026-06-30:semi_annual", "page": 40},
    ))

    result = score(case, observed)

    assert "unsupported_claim" in result.blocking_codes
    assert result.citation_support_found == 0
    assert result.citation_support_expected == 1
    assert result.citation_support == 0.0


def test_supported_claim_scores_full_support():
    case = _case("report-number-after-300")
    observed = _observation(facts=(
        {"metric": "revenue", "value": 100.0, "unit": "亿元", "period": "2026-06-30",
         "company_code": "601288", "evidence_ids": ["601288:2026-06-30:semi_annual#p40"]},
    ), artifacts=(
        {"source": "pdf", "report_id": "601288:2026-06-30:semi_annual", "page": 40},
    ))

    result = score(case, observed)

    assert result.blocking_codes == ()
    assert result.citation_support == 1.0
    assert result.cost == "unknown" and result.usage_reported is False


def test_cross_company_artifact_is_a_scope_violation_block():
    case = _case("report-number-after-300")
    observed = _observation(facts=(), artifacts=(
        {"source": "pdf", "report_id": "600900:2026-06-30:semi_annual", "page": 30},
    ))

    result = score(case, observed)

    assert "scope_violation" in result.blocking_codes


def test_unauthorized_source_call_and_overclaimed_status_are_blocking():
    case = _case("source-failure")
    observed = _observation(persisted_status="completed", call_attempts={"model": 1, "market": 2})

    result = score(case, observed)

    assert "unauthorized_source" in result.blocking_codes
    assert "overclaimed_status" in result.blocking_codes


def test_no_expected_claims_reports_not_applicable_support():
    case = _case("general-knowledge")
    result = score(case, _observation(call_attempts={"model": 1}))

    assert result.citation_support is None
    assert result.citation_support_expected == 0


def _report(cases):
    return {"suite_id": "s", "corpus_version": "c", "harness_version": "h", "clock": "2026-10-08",
            "cases": list(cases)}


def test_compare_rejects_undeclared_or_incomparable_reports():
    base = _report([{"id": "a", "calls": {"model": 1}, "blocking_codes": [], "citation_support_found": 1}])
    other_corpus = _report([{"id": "a", "calls": {"model": 1}, "blocking_codes": [], "citation_support_found": 1}])
    other_corpus["corpus_version"] = "c2"

    assert compare(base, other_corpus, declared_change="retrieval window").comparable is False
    assert "incomparable_corpus_version" in compare(base, other_corpus, declared_change="x").reasons
    assert compare(base, dict(base), declared_change="  ").comparable is False

    regressed = _report([{"id": "a", "calls": {"model": 1}, "blocking_codes": ["scope_violation"],
                          "citation_support_found": 0}])
    comparison = compare(base, regressed, declared_change="retrieval window")
    assert comparison.comparable is False
    assert any(item.startswith("blocking:a") for item in comparison.regressions)


def test_report_payload_is_whitelisted_and_writer_is_atomic(tmp_path):
    case = _case("report-number-after-300")
    observation = _observation(facts=(), artifacts=(), usage={"total_tokens": 10})
    result = score(case, observation)

    payload = build_report("s", "c", "rev", [result], elapsed_seconds=1.0, clock="2026-10-08")
    serialized = json.dumps(payload, ensure_ascii=False)
    for leaked in ("answer", "snippet", "prompt", "arguments"):
        assert leaked not in payload["cases"][0]
        assert leaked not in serialized or leaked == "answer"
    assert payload["cases"][0]["usage_reported"] is True

    target = tmp_path / "report.json"
    write_report(target, payload)
    assert json.loads(target.read_text(encoding="utf-8"))["totals"]["cases"] == 1
    with pytest.raises(ValueError, match="quality sidecar"):
        write_report(tmp_path / "research_quality_summary.json", payload)
