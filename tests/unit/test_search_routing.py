import json
from datetime import datetime
from zoneinfo import ZoneInfo

from webapp import server
from webapp.chat_models import Scope
from webapp.source_adapters import SourceAccess
from webapp.source_runtime import CallBudget, SourceRuntime
from webapp.search_usage_store import SearchUsageStore


class FakeSearch:
    def __init__(self, available, result):
        self.available = available
        self.result = result
        self.calls = []

    def search(self, query):
        self.calls.append(query)
        return self.result


def result(payload, *, error=None):
    data = {"source": "web_search", "results": payload}
    if error:
        data["error"] = error
    return json.dumps(data)


def make_runtime(*, web=True, deepseek=True, total=4):
    access = SourceAccess(
        mcp_enabled=False, listed_tools=frozenset(), whitelist=frozenset(),
        mcp_allow=lambda: False, web_enabled=web, tencent_enabled=False,
        deepseek_enabled=deepseek,
    )
    return SourceRuntime(Scope.whole_corpus(), CallBudget(total, total, total), access.permits)


def make_store(tmp_path, limit=1):
    return SearchUsageStore(str(tmp_path / "usage.sqlite3"), monthly_limit=limit,
                            clock=lambda: datetime(2026, 10, 19, 12, tzinfo=ZoneInfo("Asia/Shanghai")))


def test_server_enables_deepseek_only_when_web_search_and_key_are_available(monkeypatch):
    from types import SimpleNamespace

    class AvailableDeepSeek:
        available = True
        def __init__(self, *args, **kwargs):
            pass

    class UnavailableTavily:
        available = False
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(server, "DeepSeekWebSearch", AvailableDeepSeek)
    monkeypatch.setattr(server, "TavilyWebSearch", UnavailableTavily)
    cfg = SimpleNamespace(web_search=True, web_search_timeout=15, mcp_max_tool_calls=2)
    runtime = server._source_runtime_for_run(Scope.whole_corpus(), SimpleNamespace(max_calls=2), cfg,
                                             use_mcp=False, source_mode="general_web")
    assert runtime.authorize(server.SourceCall("deepseek_search", "web_search", "web", {"query": "x"}))

    cfg.web_search = False
    disabled = server._source_runtime_for_run(Scope.whole_corpus(), SimpleNamespace(max_calls=2), cfg,
                                               use_mcp=False, source_mode="general_web")
    assert not disabled.authorize(server.SourceCall("deepseek_search", "web_search", "web", {"query": "x"}))


def test_legacy_tool_executor_also_routes_to_deepseek_then_tavily_at_cap(monkeypatch, tmp_path):
    deepseek = FakeSearch(True, result([{"title": "DS", "url": "https://ds.example/a", "content": "x"}]))
    tavily = FakeSearch(True, result([{"title": "T", "url": "https://tv.example/a", "content": "y"}]))
    monkeypatch.setattr(server, "DeepSeekWebSearch", lambda **_kwargs: deepseek)
    monkeypatch.setattr(server, "TavilyWebSearch", lambda **_kwargs: tavily)
    store = make_store(tmp_path)
    monkeypatch.setattr(server, "_search_usage_store", lambda: store)
    executor = server._build_chat_tool_executor(
        type("Cfg", (), {"web_search": True, "web_search_timeout": 15, "mcp_tools": False})(),
    )

    first = json.loads(executor("web_search", {"query": "first"}))
    second = json.loads(executor("web_search", {"query": "second"}))

    assert first["results"][0]["url"] == "https://ds.example/a"
    assert deepseek.calls == ["first"] and tavily.calls == ["second"]
    assert store.usage_for_month("2026-10")["successful"] == 1
    assert second["results"][0]["url"] == "https://tv.example/a"


def test_deepseek_is_primary_and_billable_result_is_recorded(tmp_path):
    deepseek = FakeSearch(True, result([{"title": "DS", "url": "https://ds.example/a", "content": "x"}]))
    tavily = FakeSearch(True, result([{"title": "Tavily", "url": "https://tv.example/a", "content": "y"}]))
    runtime = make_runtime()

    source = server._execute_routed_web_search(runtime, "query", deepseek, tavily, make_store(tmp_path), "now")

    assert source.provider == "deepseek_search"
    assert deepseek.calls == ["query"] and tavily.calls == []
    assert runtime.budget.snapshot() == {"total": 1, "market": 0, "web": 1}
    assert make_store(tmp_path).usage_for_month("2026-10")["successful"] == 1


def test_usage_database_error_fails_closed_to_tavily(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file")
    store = SearchUsageStore(str(blocked / "usage.sqlite3"), monthly_limit=1)
    deepseek = FakeSearch(True, result([{"title": "D", "url": "https://ds.example/a", "content": "x"}]))
    tavily = FakeSearch(True, result([{"title": "T", "url": "https://tv.example/a", "content": "y"}]))

    source = server._execute_routed_web_search(make_runtime(), "query", deepseek, tavily, store, "now")

    assert source.provider == "web"
    assert deepseek.calls == [] and tavily.calls == ["query"]


def test_monthly_cap_and_missing_key_route_to_tavily(tmp_path):
    store = make_store(tmp_path)
    reservation = store.reserve_deepseek_slot()
    store.finish_deepseek_slot(reservation, billable_success=True)
    capped_ds = FakeSearch(True, result([]))
    tavily = FakeSearch(True, result([{"title": "T", "url": "https://tv.example/a", "content": "y"}]))
    source = server._execute_routed_web_search(make_runtime(), "query", capped_ds, tavily, store, "now")
    assert source.provider == "web"
    assert capped_ds.calls == [] and tavily.calls == ["query"]

    missing_ds = FakeSearch(False, result([]))
    tavily2 = FakeSearch(True, result([{"title": "T", "url": "https://tv.example/b", "content": "y"}]))
    source = server._execute_routed_web_search(make_runtime(), "query", missing_ds, tavily2, make_store(tmp_path / "other"), "now")
    assert source.provider == "web"
    assert missing_ds.calls == [] and tavily2.calls == ["query"]


def test_deepseek_failure_falls_back_and_each_attempt_consumes_run_budget(tmp_path):
    deepseek = FakeSearch(True, result([], error="provider_error"))
    tavily = FakeSearch(True, result([{"title": "T", "url": "https://tv.example/a", "content": "y"}]))
    runtime = make_runtime(total=2)

    source = server._execute_routed_web_search(runtime, "query", deepseek, tavily, make_store(tmp_path), "now")

    assert source.provider == "web"
    assert deepseek.calls == tavily.calls == ["query"]
    assert runtime.budget.snapshot() == {"total": 2, "market": 0, "web": 2}


def test_fallback_does_not_send_tavily_when_attempt_budget_is_exhausted(tmp_path):
    deepseek = FakeSearch(True, result([], error="provider_error"))
    tavily = FakeSearch(True, result([]))
    runtime = make_runtime(total=1)

    source = server._execute_routed_web_search(runtime, "query", deepseek, tavily, make_store(tmp_path), "now")

    assert source.error_code == "budget_exhausted"
    assert deepseek.calls == ["query"] and tavily.calls == []
    assert runtime.budget.snapshot() == {"total": 1, "market": 0, "web": 1}


def test_tavily_unavailable_is_returned_as_controlled_source_state(tmp_path):
    deepseek = FakeSearch(False, result([]))
    tavily = FakeSearch(False, result([]))

    source = server._execute_routed_web_search(make_runtime(web=False, deepseek=False), "query",
                                               deepseek, tavily, make_store(tmp_path), "now")

    assert source.status == "unavailable"
    assert source.error_code == "not_authorized"
    assert deepseek.calls == tavily.calls == []


def test_failed_deepseek_with_unavailable_tavily_is_honest_and_does_not_count_failure(tmp_path):
    store = make_store(tmp_path)
    deepseek = FakeSearch(True, result([], error="provider_error"))
    tavily = FakeSearch(False, result([]))

    source = server._execute_routed_web_search(
        make_runtime(web=False, deepseek=True), "query", deepseek, tavily, store, "now",
    )

    assert source.status == "unavailable"
    assert deepseek.calls == ["query"] and tavily.calls == []
    assert store.usage_for_month("2026-10") == {"successful": 0, "in_flight": 0, "limit": 1}


def test_timed_out_deepseek_attempt_releases_reservation_without_fallback(tmp_path):
    import pytest
    import time
    from webapp.chat_runs import ChatRunControl, ChatRunDeadlineExceeded
    from webapp.source_runtime import CallBudget, SourceRuntime

    control = ChatRunControl("session", "run", timeout_seconds=1, started_at=time.monotonic() - 10)
    runtime = SourceRuntime(Scope.whole_corpus(), CallBudget(2, 0, 2),
                            lambda _call: True, control=control)
    store = make_store(tmp_path)
    deepseek = FakeSearch(True, result([]))
    tavily = FakeSearch(True, result([]))

    with pytest.raises(ChatRunDeadlineExceeded):
        server._execute_routed_web_search(runtime, "query", deepseek, tavily, store, "now")

    assert deepseek.calls == tavily.calls == []
    assert store.usage_for_month("2026-10")["in_flight"] == 0


def test_cancelled_deepseek_attempt_releases_reservation_without_fallback(tmp_path):
    import pytest
    from webapp.chat_runs import ChatRunCancelled, ChatRunControl
    from webapp.source_runtime import CallBudget, SourceRuntime

    control = ChatRunControl("session", "run", timeout_seconds=10)
    runtime = SourceRuntime(Scope.whole_corpus(), CallBudget(2, 0, 2),
                            lambda _call: True, control=control)
    control.cancel("user")
    store = make_store(tmp_path)
    deepseek = FakeSearch(True, result([]))
    tavily = FakeSearch(True, result([]))

    with pytest.raises(ChatRunCancelled):
        server._execute_routed_web_search(runtime, "query", deepseek, tavily, store, "now")

    assert deepseek.calls == tavily.calls == []
    assert store.usage_for_month("2026-10")["in_flight"] == 0
