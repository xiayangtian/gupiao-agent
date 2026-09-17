"""Explicit research-memory saves keep only eligible immutable run evidence."""

from datetime import datetime

import pytest

from webapp.chat_models import AnswerRun, EvidenceArtifact, Fact, VerificationReport
from webapp.research_memory import ResearchMemoryStore


def _pdf_artifact() -> EvidenceArtifact:
    return EvidenceArtifact.pdf(
        "601288:2026-06-30:semi_annual",
        "601288-2026-semi.pdf",
        40,
        "营业收入为 100 亿元",
        pdf_url="/api/history-pdf/601288-2026-semi.pdf#page=40",
    )


def _fact(*, verification: str = "verified") -> Fact:
    return Fact(
        "营业收入", 100, "亿元", "2026-06-30", "semi_annual_cumulative",
        "consolidated", "601288", "pdf", ("pdf-1",), verification,
    )


def _run(
    run_id: str,
    *,
    status: str = "completed",
    fact: Fact | None = None,
    artifact: EvidenceArtifact | None = None,
    verification_status: str = "passed",
) -> AnswerRun:
    fact = fact or _fact()
    artifact = artifact or _pdf_artifact()
    return AnswerRun(
        id=run_id,
        content="已核验的结论",
        status=status,
        facts=(fact,),
        artifacts=(artifact,),
        verification_report=VerificationReport(
            verification_status,
            supported_fact_ids=fact.evidence_ids,
        ),
    )


def _store(tmp_path, *runs: AnswerRun) -> ResearchMemoryStore:
    by_id = {run.id: run for run in runs}
    return ResearchMemoryStore(
        str(tmp_path / "research_memory.json"),
        run_lookup=by_id.get,
    )


def test_verified_pdf_fact_can_be_saved_with_source_and_expiry(tmp_path):
    fact = _fact()
    store = _store(tmp_path, _run("run-1", fact=fact))

    entry = store.save_fact(fact, "run-1")

    assert entry.kind == "fact"
    assert entry.source_run_id == "run-1"
    assert entry.payload["evidence_ids"] == ["pdf-1"]
    assert entry.expires_at is not None
    assert datetime.fromisoformat(entry.expires_at) > datetime.fromisoformat(entry.created_at)


def test_reference_conflict_unavailable_and_stopped_fragment_cannot_be_saved_as_fact(tmp_path):
    reference = _fact(verification="reference")
    conflict = _fact(verification="conflict")
    unavailable = _fact(verification="unavailable")
    stopped = _fact()
    store = _store(
        tmp_path,
        _run("reference", fact=reference),
        _run("conflict", fact=conflict),
        _run("unavailable", fact=unavailable),
        _run("stopped", status="stopped", fact=stopped),
    )

    for fact, run_id in ((reference, "reference"), (conflict, "conflict"), (unavailable, "unavailable"), (stopped, "stopped")):
        with pytest.raises(ValueError, match="已验证"):
            store.save_fact(fact, run_id)


def test_fact_requires_its_verified_positive_page_pdf_evidence_and_eligible_source_run(tmp_path):
    fact = _fact()
    no_pdf = _run("no-pdf", fact=fact, artifact=EvidenceArtifact.web(
        "https://example.com/news", "新闻", "摘要", fetched_at="2026-09-16T10:00:00+00:00",
    ))
    blocked = _run("blocked", fact=fact, verification_status="blocked")
    partial_verified = _run("partial", status="partial", fact=fact, verification_status="partial")
    store = _store(tmp_path, no_pdf, blocked, partial_verified)

    with pytest.raises(ValueError, match="PDF"):
        store.save_fact(fact, "no-pdf")
    with pytest.raises(ValueError, match="已验证"):
        store.save_fact(fact, "blocked")

    assert store.save_fact(fact, "partial").source_run_id == "partial"


def test_fact_save_fails_closed_without_a_source_run_lookup(tmp_path):
    with pytest.raises(ValueError, match="来源运行"):
        ResearchMemoryStore(str(tmp_path / "research_memory.json")).save_fact(_fact(), "run-1")


def test_artifacts_and_decisions_require_explicit_traceable_inputs(tmp_path):
    artifact = _pdf_artifact()
    store = _store(tmp_path, _run("run-1", artifact=artifact))

    pdf_entry = store.save_artifact(artifact, "run-1")
    web_entry = store.save_artifact(EvidenceArtifact.web(
        "https://example.com/news", "新闻", "摘要", fetched_at="2026-09-16T10:00:00+00:00",
    ), "run-1")
    decision = store.save_decision("关注减值变化", "run-1", ("pdf-1",))

    assert pdf_entry.expires_at is None
    assert web_entry.expires_at is not None
    assert decision.payload == {"text": "关注减值变化", "evidence_ids": ["pdf-1"]}
    with pytest.raises(ValueError, match="不能为空"):
        store.save_decision("  ", "run-1", ("pdf-1",))
    with pytest.raises(ValueError, match="证据"):
        store.save_decision("关注减值变化", "run-1", ())


def test_revoked_memory_survives_reload_but_is_excluded_by_default(tmp_path):
    path = tmp_path / "research_memory.json"
    store = _store(tmp_path, _run("run-1"))
    entry = store.save_decision("关注减值变化", "run-1", ("pdf-1",))

    assert store.revoke(entry.id) is True
    reloaded = ResearchMemoryStore(str(path), run_lookup=lambda _: None)
    assert reloaded.list_active() == []
    assert len(reloaded.list_entries()) == 1
    assert reloaded.list_entries()[0].id == entry.id
    assert reloaded.list_entries()[0].revoked_at is not None
