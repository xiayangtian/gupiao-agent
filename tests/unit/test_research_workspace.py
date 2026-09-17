"""Research workspace indexes immutable run metadata without indexing raw evidence text."""

import json

from webapp.chat_models import AnswerRun, CompanyRef, EvidenceArtifact, IndustryRef, IntentDecision, Scope
from webapp.chat_store import ChatStore
from webapp.research_workspace import ResearchWorkspaceQuery, ResearchWorkspaceStore


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
    decision_summaries: tuple[str, ...] = (),
    artifact_snippet: str = "原始 PDF 片段不应进入搜索索引",
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
        research_summary={"decision_summaries": list(decision_summaries)},
        artifacts=(EvidenceArtifact.pdf(
            report_id=f"{code}:{period}:semi_annual",
            pdf_filename=f"{code}-{period}.pdf",
            page=40,
            snippet=artifact_snippet,
        ),),
        created_at="2026-09-16T10:00:00",
        completed_at="2026-09-16T10:00:00",
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
    return ResearchWorkspaceStore(chats, str(tmp_path / "research_workspace.json")), chats, session_ids


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


def test_workspace_text_search_matches_title_and_saved_decision_but_not_raw_artifact(tmp_path):
    decision_run = _run(
        "decision-run", code="601288", name="农业银行", decision_summaries=("关注现金流改善",),
    )
    artifact_run = _run("artifact-run", code="600900", name="长江电力")
    store, _, _ = _workspace_with_runs(tmp_path, [
        ("农业银行研究", [decision_run]),
        ("长江电力研究", [artifact_run]),
    ])

    items = store.list_items(ResearchWorkspaceQuery(text="现金流"))

    assert [item.run_id for item in items] == ["decision-run"]
    assert all("现金流" in (item.title + item.searchable_summary) for item in items)
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
