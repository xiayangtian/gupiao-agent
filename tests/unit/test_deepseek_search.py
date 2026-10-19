import json

from financial_report_fetcher.rag.deepseek_search import DeepSeekWebSearch


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, *, headers, json, timeout):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self.response


def test_deepseek_search_sends_bounded_request_and_maps_source_fields():
    session = FakeSession(FakeResponse({"results": [{
        "title": "公告标题", "url": "https://example.com/news",
        "summary": "公告摘要", "snippet": "短摘要", "content": "正文内容",
        "date": "2026-10-18T00:00:00+00:00", "authority": 4,
    }]}))
    search = DeepSeekWebSearch("unit-key", session=session, timeout=7)

    result = json.loads(search.search("  某公司 公告  ", count=50))

    assert session.calls == [{
        "url": "https://api.deepseek.com/v1/tools/search",
        "headers": {"Authorization": "Bearer unit-key", "Content-Type": "application/json"},
        "json": {"query": "某公司 公告", "count": 5, "need_content": True},
        "timeout": 7,
    }]
    assert result["source"] == "web_search"
    assert result["provider"] == "deepseek_search"
    assert result["results"] == [{
        "title": "公告标题", "url": "https://example.com/news",
        "content": "正文内容", "snippet": "短摘要", "published_date": "2026-10-18T00:00:00+00:00",
    }]


def test_deepseek_search_caps_query_and_removes_unsafe_results_and_bounds_text():
    long = "内容" * 2_000
    session = FakeSession(FakeResponse({"results": [
        {"title": "bad", "url": "javascript:alert(1)", "content": "unsafe"},
        {"title": "bad credentials", "url": "https://user:pass@example.com/", "content": "unsafe"},
        {"title": "好", "url": "https://example.com", "summary": long, "content": long},
    ]}))
    search = DeepSeekWebSearch("unit-key", session=session)

    result = json.loads(search.search("代码 " + "趋势 " * 100))

    assert len(session.calls[0]["json"]["query"]) <= 128
    assert len(result["results"]) == 1
    assert result["results"][0]["url"] == "https://example.com"
    assert len(result["results"][0]["content"]) <= 1_200
    assert len(result["results"][0]["snippet"]) <= 400


def test_missing_key_does_not_construct_http_session(monkeypatch):
    import financial_report_fetcher.rag.deepseek_search as module

    monkeypatch.setattr(module.requests, "Session", lambda: (_ for _ in ()).throw(
        AssertionError("HTTP session should be lazy when search is disabled")))

    search = module.DeepSeekWebSearch(api_key="")
    assert json.loads(search.search("query"))["error"] == "missing_api_key"


def test_deepseek_search_empty_query_never_calls_network():
    session = FakeSession(FakeResponse({"results": []}))

    result = json.loads(DeepSeekWebSearch("unit-key", session=session).search("  \n  "))

    assert result["error"] == "empty_query"
    assert session.calls == []


def test_deepseek_search_uses_environment_key_and_missing_key_never_calls_network(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    session = FakeSession(FakeResponse({"results": []}))
    assert DeepSeekWebSearch(session=session).available is True

    monkeypatch.delenv("DEEPSEEK_API_KEY")
    empty_session = FakeSession(FakeResponse({"results": []}))
    result = json.loads(DeepSeekWebSearch(session=empty_session).search("query"))
    assert result["error"] == "missing_api_key"
    assert empty_session.calls == []


def test_deepseek_nonempty_unsafe_provider_results_still_count_as_billable():
    session = FakeSession(FakeResponse({"results": [{
        "title": "Unsafe", "url": "javascript:alert(1)", "content": "not retained",
    }]}))

    result = json.loads(DeepSeekWebSearch("unit-key", session=session).search("query"))

    assert result["error"] == "invalid_results"
    assert result["results"] == []
    assert result["billable_success"] is True


def test_deepseek_search_empty_or_invalid_response_is_controlled_and_does_not_expose_exception(monkeypatch):
    class BrokenSession:
        def post(self, *args, **kwargs):
            raise RuntimeError("Bearer secret-detail")

    broken = json.loads(DeepSeekWebSearch("unit-key", session=BrokenSession()).search("query"))
    empty = json.loads(DeepSeekWebSearch("unit-key", session=FakeSession(FakeResponse({"results": []}))).search("query"))

    assert broken["error"] == "provider_error"
    assert "secret-detail" not in json.dumps(broken)
    assert empty["results"] == []
    assert empty["error"] == "empty_results"
