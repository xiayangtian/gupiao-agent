"""Explicit research-memory saves keep only eligible immutable run evidence."""

import json
from datetime import datetime

import pytest

from webapp.chat_models import AnswerRun, EvidenceArtifact, Fact, VerificationReport
from webapp.research_memory import ResearchMemoryStore


REPORT_ID = "601288:2026-06-30:semi_annual"
PDF_FILENAME = "601288-2026-semi.pdf"
PDF_URL = f"/api/history-pdf/{PDF_FILENAME}?jump=0#page=40"
PDF_EVIDENCE_ID = f"{REPORT_ID}#p40"


def _pdf_artifact() -> EvidenceArtifact:
    return EvidenceArtifact.pdf(
        REPORT_ID,
        PDF_FILENAME,
        40,
        "营业收入为 100 亿元",
        pdf_url=PDF_URL,
    )


def _web_artifact() -> EvidenceArtifact:
    return EvidenceArtifact.web(
        "https://example.com/news", "新闻", "摘要", fetched_at="2026-09-16T10:00:00+00:00",
    )


def _fact(*, verification: str = "verified", evidence_ids: tuple[str, ...] = (PDF_EVIDENCE_ID,)) -> Fact:
    return Fact(
        "营业收入", 100, "亿元", "2026-06-30", "semi_annual_cumulative",
        "consolidated", "601288", "pdf", evidence_ids, verification,
    )


def _run(
    run_id: str,
    *,
    status: str = "completed",
    fact: Fact | None = None,
    artifact: EvidenceArtifact | None = None,
    extra_artifacts: tuple[EvidenceArtifact, ...] = (),
    verification_status: str = "passed",
) -> AnswerRun:
    fact = fact or _fact()
    artifact = artifact or _pdf_artifact()
    return AnswerRun(
        id=run_id,
        content="已核验的结论",
        status=status,
        facts=(fact,),
        artifacts=(artifact,) + extra_artifacts,
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
    assert entry.payload["evidence_ids"] == [PDF_EVIDENCE_ID]
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
    no_pdf = _run("no-pdf", fact=fact, artifact=_web_artifact())
    blocked = _run("blocked", fact=fact, verification_status="blocked")
    partial_verified = _run("partial", status="partial", fact=fact, verification_status="partial")
    store = _store(tmp_path, no_pdf, blocked, partial_verified)

    with pytest.raises(ValueError, match="PDF"):
        store.save_fact(fact, "no-pdf")
    with pytest.raises(ValueError, match="已验证"):
        store.save_fact(fact, "blocked")

    assert store.save_fact(fact, "partial").source_run_id == "partial"


def test_every_fact_evidence_id_must_map_to_a_positive_page_pdf_artifact(tmp_path):
    """A fact whose evidence id has no persisted positive-page PDF mapping is rejected."""
    unmapped = _fact(evidence_ids=("pdf-1",))
    store = _store(tmp_path, _run("run-1", fact=unmapped))

    with pytest.raises(ValueError, match="PDF"):
        store.save_fact(unmapped, "run-1")

    mapped = _fact(evidence_ids=(PDF_URL,))
    mapped_store = _store(tmp_path, _run("run-2", fact=mapped))

    assert mapped_store.save_fact(mapped, "run-2").payload["evidence_ids"] == [PDF_URL]


def test_fact_save_fails_closed_without_a_source_run_lookup(tmp_path):
    with pytest.raises(ValueError, match="来源运行"):
        ResearchMemoryStore(str(tmp_path / "research_memory.json")).save_fact(_fact(), "run-1")


def test_artifact_and_decision_saves_require_the_immutable_source_run(tmp_path):
    """Artifacts and decisions are as traceable as facts: no lookup means no save."""
    store = ResearchMemoryStore(str(tmp_path / "research_memory.json"))

    with pytest.raises(ValueError, match="来源运行"):
        store.save_artifact(_pdf_artifact(), "run-1")
    with pytest.raises(ValueError, match="来源运行"):
        store.save_decision("关注减值变化", "run-1", (PDF_EVIDENCE_ID,))
    with pytest.raises(ValueError, match="来源运行"):
        store.save_artifact(_web_artifact(), "run-1")


def test_web_and_pdf_artifacts_must_belong_to_the_source_run(tmp_path):
    run = _run("run-1", extra_artifacts=(_web_artifact(),))
    store = _store(tmp_path, run)

    assert store.save_artifact(_web_artifact(), "run-1").source_run_id == "run-1"

    foreign = EvidenceArtifact.web(
        "https://example.com/other", "其他新闻", "摘要", fetched_at="2026-09-16T10:00:00+00:00",
    )
    with pytest.raises(ValueError, match="artifact"):
        store.save_artifact(foreign, "run-1")
    with pytest.raises(ValueError, match="来源运行"):
        store.save_artifact(_pdf_artifact(), "unknown-run")


def test_decision_evidence_ids_must_resolve_to_the_source_run(tmp_path):
    store = _store(tmp_path, _run("run-1"))

    with pytest.raises(ValueError, match="证据"):
        store.save_decision("关注减值变化", "run-1", ("not-in-run",))

    assert store.save_decision("关注减值变化", "run-1", (PDF_EVIDENCE_ID,)).payload == {
        "text": "关注减值变化", "evidence_ids": [PDF_EVIDENCE_ID],
    }


def test_artifacts_and_decisions_require_explicit_traceable_inputs(tmp_path):
    artifact = _pdf_artifact()
    store = _store(tmp_path, _run("run-1", artifact=artifact, extra_artifacts=(_web_artifact(),)))

    pdf_entry = store.save_artifact(artifact, "run-1")
    web_entry = store.save_artifact(_web_artifact(), "run-1")
    decision = store.save_decision("关注减值变化", "run-1", (PDF_EVIDENCE_ID,))

    assert pdf_entry.expires_at is None
    assert web_entry.expires_at is not None
    assert decision.payload == {"text": "关注减值变化", "evidence_ids": [PDF_EVIDENCE_ID]}
    with pytest.raises(ValueError, match="不能为空"):
        store.save_decision("  ", "run-1", (PDF_EVIDENCE_ID,))
    with pytest.raises(ValueError, match="证据"):
        store.save_decision("关注减值变化", "run-1", ())


def test_saved_memory_rejects_external_mutation_of_defensive_copies(tmp_path):
    """Callers must not be able to rewrite persisted memory through returned values."""
    path = tmp_path / "research_memory.json"
    store = _store(tmp_path, _run("run-1"))
    entry = store.save_fact(_fact(), "run-1")

    entry.payload["evidence_ids"].append("injected")
    entry.payload["value"] = 999
    entry.to_dict()["payload"]["evidence_ids"].append("injected-by-read")
    listed = store.list_entries()
    mutation = getattr(listed, "append", None)
    if mutation is not None:
        mutation("not-an-entry")
    assert len(store.list_entries()) == 1

    on_disk = json.loads(path.read_text(encoding="utf-8"))["entries"][0]["payload"]
    reloaded = ResearchMemoryStore(str(path), run_lookup=lambda _: None)

    assert on_disk["evidence_ids"] == [PDF_EVIDENCE_ID]
    assert on_disk["value"] == 100
    assert len(reloaded.list_entries()) == 1
    assert reloaded.list_entries()[0].to_dict()["payload"]["evidence_ids"] == [PDF_EVIDENCE_ID]
    assert entry.id == reloaded.list_entries()[0].id


def test_revoked_memory_survives_reload_but_is_excluded_by_default(tmp_path):
    path = tmp_path / "research_memory.json"
    store = _store(tmp_path, _run("run-1"))
    entry = store.save_decision("关注减值变化", "run-1", (PDF_EVIDENCE_ID,))

    assert store.revoke(entry.id) is True
    reloaded = ResearchMemoryStore(str(path), run_lookup=lambda _: None)
    assert reloaded.list_active() == []
    assert len(reloaded.list_entries()) == 1
    assert reloaded.list_entries()[0].id == entry.id
    assert reloaded.list_entries()[0].revoked_at is not None
