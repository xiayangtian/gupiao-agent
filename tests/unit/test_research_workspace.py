"""Research workspace indexes immutable run metadata without indexing raw evidence text."""

import json

from webapp.chat_models import (
    AnswerRun,
    CompanyRef,
    EvidenceArtifact,
    IndustryRef,
    IntentDecision,
    Scope,
    ToolArtifact,
)
from webapp.chat_store import ChatStore
from webapp.research_workspace import ResearchWorkspaceQuery, ResearchWorkspaceStore


PDF_EVIDENCE_ID = "601288:2026-06-30:semi_annual#p40"


def _run(
    run_id: str,
    *,
    code: str,
    name: str,
    status: str = "completed",
    title: str = "",
    industry: IndustryRef | None = None,
    period: str = "2026-06-30",
    intent: str = "report_fact",
    artifact_snippet: str = "原始 PDF 片段不应进入搜索索引",
    created_at: str = "2026-09-16T10:00:00",
    completed_at: str = "2026-09-16T10:00:00",
    artifacts: tuple[EvidenceArtifact, ...] | None = None,
    tool_artifacts: tuple[ToolArtifact, ...] = (),
) -> AnswerRun:
    scope = Scope(
        mode="company_industry" if industry else "company_only",
        companies=(CompanyRef(code, name),),
        report_ids=(
            f"{code}:{period}:semi_annual",
            "600900:2026-06-30:semi_annual",
        ) if industry else (f"{code}:{period}:semi_annual",),
        industry=industry,
    )
    return AnswerRun(
        id=run_id,
        content="回答正文不应进入工作台搜索索引",
        status=status,
        scope=scope,
        intent_decision=IntentDecision(intent=intent),
        artifacts=artifacts if artifacts is not None else (EvidenceArtifact.pdf(
            report_id=f"{code}:{period}:semi_annual",
            pdf_filename=f"{code}-{period}.pdf",
            page=40,
            snippet=artifact_snippet,
        ),),
        tool_artifacts=tool_artifacts,
        created_at=created_at,
        completed_at=completed_at,
    )


def _workspace_with_runs(tmp_path, runs_by_session: list[tuple[str, list[AnswerRun]]]):
    chats = ChatStore(str(tmp_path / "sessions.json"))
    session_ids = {}
    for label, runs in runs_by_session:
        session = chats.create_session()
        chats.rename_session(session["id"], label)
        session_ids[label] = session["id"]
        for run in runs:
            chats.append_turn(session["id"], question=f"{label} 问题", run=run)
    return (
        ResearchWorkspaceStore(chats, str(tmp_path / "research_workspace.json")),
        chats,
        session_ids,
    )


def test_workspace_filters_by_persisted_company_and_status_not_title(tmp_path):
    bank_completed = _run("bank-completed", code="601288", name="农业银行")
    bank_stopped = _run("bank-stopped", code="601288", name="农业银行", status="stopped")
    yangtze_completed = _run("yangtze-completed", code="600900", name="长江电力")
    store, _, _ = _workspace_with_runs(tmp_path, [
        ("标题错误地写着长江电力", [bank_completed, bank_stopped]),
        ("农业银行", [yangtze_completed]),
    ])

    items = store.list_items(ResearchWorkspaceQuery(company_code="601288", status="completed"))

    assert [item.run_id for item in items] == ["bank-completed"]


def test_workspace_text_search_matches_title_but_not_raw_artifact(tmp_path):
    title_run = _run("title-run", code="601288", name="农业银行")
    artifact_run = _run("artifact-run", code="600900", name="长江电力")
    store, _, _ = _workspace_with_runs(tmp_path, [
        ("现金流研究", [title_run]),
        ("长江电力研究", [artifact_run]),
    ])

    items = store.list_items(ResearchWorkspaceQuery(text="现金流"))

    assert [item.run_id for item in items] == ["title-run"]
    assert items[0].searchable_summary == ""
    assert store.list_items(ResearchWorkspaceQuery(text="原始 PDF 片段")) == []


def test_workspace_filters_by_industry_period_intent_and_favorite_then_sorts_by_update_time(tmp_path):
    bank = _run(
        "bank", code="601288", name="农业银行", period="2025-12-31", intent="industry_benchmark",
        industry=IndustryRef("银行", "fixture"),
    )
    other = _run("other", code="600900", name="长江电力")
    store, _, sessions = _workspace_with_runs(tmp_path, [("银行研究", [bank]), ("其他研究", [other])])
    store.set_favorite(sessions["其他研究"], "other", True)

    items = store.list_items()
    filtered = store.list_items(ResearchWorkspaceQuery(
        industry="银行", period="2025-12-31", intent="industry_benchmark",
    ))

    assert [item.run_id for item in items] == ["other", "bank"]
    assert [item.run_id for item in filtered] == ["bank"]
    assert [item.run_id for item in store.list_items(ResearchWorkspaceQuery(favorite_only=True))] == ["other"]


def test_deleting_session_removes_workspace_items_but_not_favorites_in_other_sessions(tmp_path):
    first = _run("first", code="601288", name="农业银行")
    second = _run("second", code="600900", name="长江电力")
    store, chats, sessions = _workspace_with_runs(tmp_path, [("first", [first]), ("second", [second])])
    store.set_favorite(sessions["first"], "first", True)
    store.set_favorite(sessions["second"], "second", True)

    assert chats.delete_session(sessions["first"]) is True
    sidecar = json.loads((tmp_path / "research_workspace.json").read_text(encoding="utf-8"))
    items = store.list_items()

    assert [item.run_id for item in items] == ["second"]
    assert items[0].favorite is True
    assert sidecar["favorites"] == [{"session_id": sessions["second"], "run_id": "second"}]


def test_workspace_item_reports_evidence_availability_from_persisted_artifacts(tmp_path):
    """A row may only claim evidence is available when a persisted artifact reopens.

    Legacy runs migrated from bare messages keep no artifact, and an unavailable PDF
    cannot send a reader back to the page, so both must report no usable evidence.
    """
    with_pdf = _run("with-pdf", code="601288", name="农业银行")
    web_only = _run(
        "web-only", code="600900", name="长江电力",
        artifacts=(EvidenceArtifact.web(
            url="https://example.test/disclosure", title="外部披露",
            snippet="外部原文片段", fetched_at="2026-09-16T10:00:00+08:00",
        ),),
    )
    no_artifact = _run("no-artifact", code="601288", name="农业银行", artifacts=())
    unavailable_pdf = _run(
        "unavailable-pdf", code="601288", name="农业银行",
        artifacts=(EvidenceArtifact.pdf(
            report_id="601288:2026-06-30:semi_annual", pdf_filename="601288.pdf", page=40,
            snippet="原文件已不可用", availability="unavailable",
        ),),
    )
    store, _, _ = _workspace_with_runs(tmp_path, [
        ("农业银行研究", [with_pdf, no_artifact, unavailable_pdf]),
        ("长江电力研究", [web_only]),
    ])

    items = {item.run_id: item for item in store.list_items()}

    assert items["with-pdf"].evidence_available is True
    assert items["web-only"].evidence_available is True
    assert items["no-artifact"].evidence_available is False
    assert items["unavailable-pdf"].evidence_available is False
    assert items["with-pdf"].to_dict()["evidence_available"] is True
    assert items["no-artifact"].to_dict()["evidence_available"] is False


def test_workspace_normalizes_mixed_naive_and_aware_timestamps_to_utc(tmp_path):
    """Naive and offset timestamps must be compared as instants, not as strings.

    ``ChatStore`` writes naive local ``updated_at`` while ``AnswerRun`` carries
    offset-aware timestamps, so a lexicographic maximum can pick the older run.
    """
    naive_newer = _run(
        "naive-newer", code="601288", name="农业银行",
        created_at="2099-09-16T23:30:00", completed_at="2099-09-16T23:30:00",
    )
    aware_older = _run(
        "aware-older", code="601288", name="农业银行",
        created_at="2099-09-17T02:00:00+08:00", completed_at="2099-09-17T02:00:00+08:00",
    )
    store, _, _ = _workspace_with_runs(tmp_path, [("农业银行研究", [aware_older, naive_newer])])

    items = store.list_items()

    assert [item.run_id for item in items] == ["naive-newer", "aware-older"]
    assert items[0].updated_at == "2099-09-16T23:30:00+00:00"
    assert items[1].updated_at == "2099-09-16T18:00:00+00:00"


def test_workspace_favorites_reload_from_sidecar_and_prune_orphan_runs(tmp_path):
    """Favorites persist across reloads; entries without a live run are pruned."""
    store, chats, sessions = _workspace_with_runs(
        tmp_path, [("农业银行研究", [_run("first", code="601288", name="农业银行")])],
    )
    store.set_favorite(sessions["农业银行研究"], "first", True)
    sidecar_path = tmp_path / "research_workspace.json"

    reloaded = ResearchWorkspaceStore(chats, str(sidecar_path))

    assert [item.run_id for item in reloaded.list_items(ResearchWorkspaceQuery(favorite_only=True))] == ["first"]

    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["favorites"].append({"session_id": sessions["农业银行研究"], "run_id": "removed-run"})
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")

    orphaned = ResearchWorkspaceStore(chats, str(sidecar_path))
    items = orphaned.list_items(ResearchWorkspaceQuery(favorite_only=True))

    assert [item.run_id for item in items] == ["first"]
    assert json.loads(sidecar_path.read_text(encoding="utf-8"))["favorites"] == [
        {"session_id": sessions["农业银行研究"], "run_id": "first"},
    ]


def test_workspace_text_search_ignores_raw_tool_and_web_bodies(tmp_path):
    """Only title and saved decision summaries are searchable, never raw evidence text."""
    run = _run(
        "raw-bodies", code="601288", name="农业银行",
        artifacts=(
            EvidenceArtifact.pdf(
                report_id="601288:2026-06-30:semi_annual",
                pdf_filename="601288-2026-06-30.pdf",
                page=40,
                snippet="原始 PDF 片段不应进入搜索索引",
            ),
            EvidenceArtifact.web(
                "https://example.com/news", "异动新闻", "独家涨停原因解读",
                fetched_at="2026-09-16T10:00:00+00:00",
            ),
        ),
        tool_artifacts=(
            ToolArtifact(
                "market", "quote", "2026-09-16T10:00:00+08:00", "success",
                result_summary="机构专用席位净买入",
            ),
        ),
    )
    store, _, _ = _workspace_with_runs(tmp_path, [("农业银行研究", [run])])

    assert store.list_items(ResearchWorkspaceQuery(text="机构专用席位净买入")) == []
    assert store.list_items(ResearchWorkspaceQuery(text="独家涨停原因解读")) == []
    assert store.list_items(ResearchWorkspaceQuery(text="原始 PDF 片段")) == []
    assert [item.run_id for item in store.list_items(ResearchWorkspaceQuery(text="农业银行"))] == ["raw-bodies"]
