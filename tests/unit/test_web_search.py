import json

from financial_report_fetcher.rag.web_search import TavilyWebSearch


class FakeResponse:
    status_code = 200

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


def test_tavily_search_returns_sanitized_sources():
    session = FakeSession(FakeResponse({"results": [{
        "title": "公司公告", "url": "https://example.com/report",
        "content": "公告摘要", "published_date": "2026-09-01", "score": 0.9,
    }]}))
    search = TavilyWebSearch("test-key", session=session, timeout=9)

    result = json.loads(search.search("某公司最新公告", max_results=10))

    assert session.calls[0]["url"] == "https://api.tavily.com/search"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert session.calls[0]["json"] == {
        "query": "某公司最新公告", "search_depth": "basic", "max_results": 5,
        "topic": "general", "include_answer": False, "include_raw_content": False,
    }
    assert session.calls[0]["timeout"] == 9
    assert result["source"] == "web_search"
    assert result["results"] == [{
        "title": "公司公告", "url": "https://example.com/report",
        "content": "公告摘要", "published_date": "2026-09-01",
    }]


def test_tavily_search_missing_key_does_not_send_request():
    session = FakeSession(FakeResponse({}))
    result = TavilyWebSearch("", session=session).search("测试")

    assert result.startswith("工具调用失败：网页搜索未配置 TAVILY_API_KEY")
    assert session.calls == []
