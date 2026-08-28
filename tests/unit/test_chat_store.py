"""ChatStore 会话存储单元测试：新建/追加/列表/读取/持久化"""

import json

from webapp.chat_store import ChatStore


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
