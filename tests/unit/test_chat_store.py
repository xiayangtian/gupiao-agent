"""ChatStore 会话存储单元测试：新建/追加/列表/读取/持久化"""

import json

from webapp.chat_models import AnswerRun, EvidenceArtifact, Scope
from webapp.chat_store import ChatStore


def _stopped_run_with_pdf_artifact() -> AnswerRun:
    return AnswerRun(
        content="现金流分析未完成",
        status="stopped",
        artifacts=(EvidenceArtifact.pdf(
            report_id="601288:2026-06-30:semi_annual",
            pdf_filename="601288-2026H1.pdf",
            page=40,
            snippet="经营活动产生的现金流量净额",
        ),),
    )


def test_create_and_list_session(tmp_path):
    store = ChatStore(str(tmp_path / "sessions.json"))
    s = store.create_session()
    assert s["id"]
    assert s["messages"] == []
    listed = store.list_sessions()
    assert len(listed) == 1
    assert listed[0]["id"] == s["id"]
    assert listed[0]["message_count"] == 0


def test_append_messages_sets_title_and_count(tmp_path):
    store = ChatStore(str(tmp_path / "sessions.json"))
    s = store.create_session()
    updated = store.append_messages(s["id"], [
        {"role": "user", "content": "长江电力今年营收增长情况如何？"},
        {"role": "assistant", "content": "营收增长 12%"},
    ])
    assert len(updated["messages"]) == 2
    # 标题取首条用户消息截断
    assert updated["title"] == "长江电力今年营收增长情况如何？"
    detail = store.get_session(s["id"])
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_append_keeps_existing_title(tmp_path):
    store = ChatStore(str(tmp_path / "sessions.json"))
    s = store.create_session()
    store.append_messages(s["id"], [{"role": "user", "content": "第一个问题"}])
    store.append_messages(s["id"], [{"role": "user", "content": "第二个问题"}])
    assert store.get_session(s["id"])["title"] == "第一个问题"


def test_get_or_create_reuses_existing(tmp_path):
    store = ChatStore(str(tmp_path / "sessions.json"))
    s = store.create_session()
    assert store.get_or_create(s["id"])["id"] == s["id"]
    created = store.get_or_create(None)
    assert created["id"] != s["id"]
    assert len(store.list_sessions()) == 2


def test_persists_across_reload(tmp_path):
    path = tmp_path / "sessions.json"
    store = ChatStore(str(path))
    s = store.create_session()
    store.append_messages(s["id"], [{"role": "user", "content": "问题A"}])
    # 重新加载（模拟重启）
    store2 = ChatStore(str(path))
    assert store2.get_session(s["id"])["title"] == "问题A"
    assert store2.get_session(s["id"])["messages"] == [{"role": "user", "content": "问题A"}]


def test_unknown_session_returns_none(tmp_path):
    store = ChatStore(str(tmp_path / "sessions.json"))
    assert store.get_session("nope") is None
    assert store.get_messages("nope") == []


def test_loads_v1_session_and_marks_legacy_assistant_message(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"sessions": [{
        "id": "s1", "title": "旧会话", "messages": [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "旧回答"},
        ], "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
    }]}), encoding="utf-8")
    detail = ChatStore(str(path)).get_session("s1")
    assert detail["messages"][1]["run"]["legacy_evidence_unavailable"] is True


def test_append_turn_persists_stopped_run_and_artifacts_across_reload(tmp_path):
    path = tmp_path / "sessions.json"
    store = ChatStore(str(path))
    sid = store.create_session()["id"]
    store.append_turn(sid, question="比较现金流", run=_stopped_run_with_pdf_artifact())
    reloaded = ChatStore(str(path)).get_session(sid)
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2
    assert reloaded["messages"][1]["run"]["status"] == "stopped"
    assert reloaded["messages"][1]["run"]["artifacts"][0]["page"] == 40


def test_summary_and_model_messages_use_run_without_exposing_run_metadata(tmp_path):
    store = ChatStore(str(tmp_path / "sessions.json"))
    sid = store.create_session()["id"]
    run = AnswerRun(
        content="已停止",
        status="stopped",
        scope=Scope.company_only("601288", "农业银行", ["601288:2026-06-30:semi_annual"]),
    )
    store.append_turn(sid, question="问题", run=run)
    store.append_turn(sid, question="失败问题", run=AnswerRun(content="", status="failed"))

    summary = store.list_sessions()[0]
    assert summary["status"] == "failed"
    assert summary["scope_mode"] is None
    assert summary["company_codes"] == []
    assert store.get_messages(sid) == [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "已停止"},
        {"role": "user", "content": "失败问题"},
    ]
