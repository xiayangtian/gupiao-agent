"""ChatStore 会话存储单元测试：新建/追加/列表/读取/持久化"""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

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


def _pending_clarification(now):
    return {
        "version": "pending-v1",
        "created_at": now.isoformat(),
        "missing_field": "window",
        "intent": "market_trend",
        "company_code": "600660",
        "market_window": {"kind": "rolling_trading_days", "trading_days": 5,
                          "label": "近五交易日", "end_date": "2026-10-09"},
        "financial_period": None,
        "question": "福耀玻璃股价走势",
    }


def test_pending_clarification_is_one_shot_across_store_reload(tmp_path):
    path = str(tmp_path / "sessions.json")
    store = ChatStore(path)
    sid = store.create_session()["id"]
    now = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)

    store.save_pending(sid, _pending_clarification(now))
    reopened = ChatStore(path)

    pending = reopened.consume_pending(sid, now=now, expected_version="pending-v1")
    assert pending["company_code"] == "600660"
    assert pending["market_window"]["trading_days"] == 5
    assert reopened.consume_pending(sid, now=now, expected_version="pending-v1") is None


def test_pending_clarification_rejects_expired_wrong_version_and_foreign_session(tmp_path):
    store = ChatStore(str(tmp_path / "sessions.json"))
    sid = store.create_session()["id"]
    now = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
    store.save_pending(sid, _pending_clarification(now))

    assert store.consume_pending(sid, now=now, expected_version="wrong") is None
    assert store.consume_pending("other-session", now=now, expected_version="pending-v1") is None
    assert store.consume_pending(sid, now=now + timedelta(minutes=16), expected_version="pending-v1") is None


def test_pending_clarification_has_one_atomic_consumer(tmp_path):
    store = ChatStore(str(tmp_path / "sessions.json"))
    sid = store.create_session()["id"]
    now = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
    store.save_pending(sid, _pending_clarification(now))
    barrier = threading.Barrier(3)
    results = []

    def consume():
        barrier.wait()
        results.append(store.consume_pending(sid, now=now, expected_version="pending-v1"))

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sum(result is not None for result in results) == 1


def test_append_clarification_persists_messages_and_pending_atomically(tmp_path):
    path = str(tmp_path / "sessions.json")
    store = ChatStore(path)
    sid = store.create_session()["id"]
    now = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)

    saved = store.append_clarification(
        sid, "福耀玻璃股价走势", "请补充时间范围。", _pending_clarification(now),
    )
    reloaded = ChatStore(path).get_session(sid)

    assert [message["content"] for message in saved["messages"]] == ["福耀玻璃股价走势", "请补充时间范围。"]
    assert reloaded["pending_clarification"]["version"] == "pending-v1"


def test_old_sessions_without_pending_slot_remain_readable(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"sessions": [{"id": "old", "title": "old", "messages": [],
                                                "created_at": "2026-01-01", "updated_at": "2026-01-01"}]}))

    session = ChatStore(str(path)).get_session("old")

    assert "pending_clarification" not in session


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
    message = store2.get_session(s["id"])["messages"][0]
    assert message["role"] == "user" and message["content"] == "问题A"
    assert datetime.fromisoformat(message["created_at"]).utcoffset() == timedelta(hours=8)


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


def test_new_messages_persist_shanghai_timestamps_while_legacy_messages_stay_untimed(tmp_path):
    path = tmp_path / "timestamps.json"
    store = ChatStore(str(path))
    sid = store.create_session()["id"]
    store.append_turn(sid, question="分析行情", run=AnswerRun(content="回答", status="completed"))

    messages = ChatStore(str(path)).get_session(sid)["messages"]
    assert len(messages) == 2
    for message in messages:
        stamp = datetime.fromisoformat(message["created_at"])
        assert stamp.utcoffset() == timedelta(hours=8)

    added = store.append_messages(sid, [{"role": "user", "content": "新增消息"}])["messages"][-1]
    assert datetime.fromisoformat(added["created_at"]).utcoffset() == timedelta(hours=8)

    from webapp.chat_models import ChatMessage
    legacy = ChatMessage.from_dict({"role": "user", "content": "旧消息"})
    assert legacy.to_dict() == {"role": "user", "content": "旧消息"}


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


def _supplement_request(sid: str) -> dict:
    return {
        "id": "sup-1",
        "session_id": sid,
        "status": "proposed",
        "question_digest": "digest-1",
        "candidates": [{
            "id": "candidate-1",
            "report_id": "601288:2025-06-30:semi_annual",
            "code": "601288",
            "company": "农业银行",
            "period": "2025-06-30",
            "report_type": "semi_annual",
            "source": "巨潮资讯",
        }],
    }


def test_saved_supplement_survives_reload_and_stays_bound_to_its_session(tmp_path):
    path = tmp_path / "sessions.json"
    store = ChatStore(str(path))
    sid = store.create_session()["id"]

    saved = store.save_supplement(_supplement_request(sid))

    assert saved["session_id"] == sid
    reloaded = ChatStore(str(path))
    assert reloaded.get_supplement("sup-1")["session_id"] == sid
    assert reloaded.get_supplement("sup-1")["status"] == "proposed"
    assert reloaded.get_supplement("missing") is None


def test_supplement_requires_existing_session_and_cannot_be_rebound(tmp_path):
    store = ChatStore(str(tmp_path / "sessions.json"))
    other = store.create_session()["id"]
    assert store.save_supplement(_supplement_request("nope")) is None

    sid = store.create_session()["id"]
    assert store.save_supplement(_supplement_request(sid))["session_id"] == sid
    with pytest.raises(ValueError, match="session"):
        store.save_supplement(_supplement_request(other))
    with pytest.raises(ValueError):
        store.save_supplement({"session_id": sid})


def test_delete_session_removes_its_supplements(tmp_path):
    store = ChatStore(str(tmp_path / "sessions.json"))
    sid = store.create_session()["id"]
    store.save_supplement(_supplement_request(sid))

    assert store.delete_session(sid) is True

    assert store.get_supplement("sup-1") is None


def test_waiting_consent_run_with_supplement_summary_survives_reload(tmp_path):
    path = tmp_path / "sessions.json"
    store = ChatStore(str(path))
    sid = store.create_session()["id"]
    run = AnswerRun(
        content="",
        status="waiting_consent",
        scope=Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"]),
        supplement={
            "status": "proposed",
            "reason": "缺少 2025 半年报",
            "candidates": [{
                "id": "candidate-1", "company": "农业银行", "code": "601288",
                "period": "2025-06-30", "report_type": "semi_annual",
            }],
        },
    )
    store.append_turn(sid, question="2025 上半年情况？", run=run)

    reloaded = ChatStore(str(path)).get_session(sid)

    assert reloaded["messages"][1]["run"]["status"] == "waiting_consent"
    assert reloaded["messages"][1]["run"]["supplement"]["status"] == "proposed"
    assert reloaded["messages"][1]["run"]["supplement"]["candidates"][0]["code"] == "601288"


def test_load_survives_non_mapping_supplements_field(tmp_path):
    """supplements 是非空数组时也不影响会话读取（`or {}` 兜不住真值列表）。"""
    path = tmp_path / "sessions.json"
    path.write_text(
        json.dumps({
            "sessions": [{
                "id": "s1",
                "title": "旧会话",
                "messages": [
                    {"role": "user", "content": "问题"},
                    {"role": "assistant", "content": "旧回答"},
                ],
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }],
            "supplements": [{"id": "sup-1", "session_id": "s-x"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    store = ChatStore(str(path))

    assert [session["id"] for session in store.list_sessions()] == ["s1"]
    assert store.get_session("s1")["messages"][0]["content"] == "问题"
    assert store.get_supplement("sup-1") is None

    sid = store.create_session()["id"]
    assert store.save_supplement(_supplement_request(sid))["session_id"] == sid

    reloaded = ChatStore(str(path))
    assert sorted(session["id"] for session in reloaded.list_sessions()) == sorted(["s1", sid])
    assert reloaded.get_supplement("sup-1")["session_id"] == sid
