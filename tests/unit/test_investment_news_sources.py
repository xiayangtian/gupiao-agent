from datetime import datetime, timezone

import pandas as pd
import pytest

from webapp.investment_news_sources import EastmoneyNewsSource, SourceSchemaError, _with_request_timeout


def _market_frame(rows):
    return pd.DataFrame(rows, columns=["标题", "摘要", "发布时间", "链接"])


def _stock_frame(rows):
    return pd.DataFrame(rows, columns=["关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"])


def test_stock_source_normalizes_provider_fields_and_localizes_naive_time():
    source = EastmoneyNewsSource(
        global_fetcher=lambda: _market_frame([]),
        stock_fetcher=lambda symbol: _stock_frame([{
            "关键词": "公告", "新闻标题": "公司临时公告", "新闻内容": "来源片段",
            "发布时间": "2026-10-14 10:00:00", "文章来源": "来源甲",
            "新闻链接": "https://example.test/story/1",
        }]),
    )

    items = source.fetch_stock("600900", "长江电力")

    assert len(items) == 1
    assert items[0].title == "公司临时公告"
    assert items[0].snippet == "来源片段"
    assert items[0].symbols == ("600900",)
    assert datetime.fromisoformat(items[0].published_at) == datetime(2026, 10, 14, 2, tzinfo=timezone.utc)


def test_market_source_maps_expected_fields_and_skips_empty_titles():
    source = EastmoneyNewsSource(
        global_fetcher=lambda: _market_frame([
            {"标题": "市场快讯", "摘要": "摘要内容", "发布时间": "2026-10-14 09:30:00", "链接": "https://example.test/a"},
            {"标题": "  ", "摘要": "无标题", "发布时间": "2026-10-14 09:31:00", "链接": "https://example.test/b"},
        ]),
        stock_fetcher=lambda symbol: _stock_frame([]),
    )

    items = source.fetch_market()

    assert len(items) == 1
    assert items[0].title == "市场快讯"
    assert items[0].snippet == "摘要内容"
    assert items[0].source_name == "东方财富市场快讯"
    assert items[0].kind == "market"


def test_source_rejects_non_http_urls_and_cleans_html_and_truncates_snippet():
    source = EastmoneyNewsSource(
        global_fetcher=lambda: _market_frame([{
            "标题": "标题", "摘要": "<b>" + "片段" * 700 + "</b>",
            "发布时间": "not-a-time", "链接": "javascript:alert(1)",
        }]),
        stock_fetcher=lambda symbol: _stock_frame([]),
    )

    with pytest.raises(ValueError, match="HTTP"):
        source.fetch_market()

    source = EastmoneyNewsSource(
        global_fetcher=lambda: _market_frame([{
            "标题": "标题", "摘要": "<b>" + "片段" * 700 + "</b>",
            "发布时间": "not-a-time", "链接": "https://example.test/a",
        }]),
        stock_fetcher=lambda symbol: _stock_frame([]),
    )
    item = source.fetch_market()[0]
    assert "<b>" not in item.snippet
    assert len(item.snippet) == 1200
    assert item.published_at is None


def test_source_schema_mismatch_is_explicit():
    source = EastmoneyNewsSource(
        global_fetcher=lambda: pd.DataFrame([{"标题": "no expected columns"}]),
        stock_fetcher=lambda symbol: _stock_frame([]),
    )

    with pytest.raises(SourceSchemaError, match="摘要"):
        source.fetch_market()


def test_default_fetcher_timeout_does_not_patch_global_requests_module():
    class RequestsFake:
        def __init__(self):
            self.calls = []
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return "response"

    requests = RequestsFake()
    namespace = {"requests": requests}
    exec("def fetch(url): return requests.get(url)", namespace)
    fetch = namespace["fetch"]
    wrapped = _with_request_timeout(fetch)

    assert wrapped("https://example.test") == "response"
    assert requests.calls[0][1]["timeout"] == (5, 20)
    assert fetch.__globals__["requests"] is requests


def test_watchlist_source_failure_is_isolated_per_security():
    def stock_fetcher(symbol):
        if symbol == "600001":
            raise RuntimeError("provider unavailable")
        return _stock_frame([{
            "关键词": "公告", "新闻标题": "第二只股票新闻", "新闻内容": "内容",
            "发布时间": "2026-10-14 10:00:00", "文章来源": "来源乙",
            "新闻链接": "https://example.test/second",
        }])

    source = EastmoneyNewsSource(
        global_fetcher=lambda: _market_frame([]), stock_fetcher=stock_fetcher,
    )
    items, results = source.fetch_for_watchlist([
        {"code": "600001", "name": "公司一"},
        {"code": "600002", "name": "公司二"},
    ])

    assert [item.title for item in items] == ["第二只股票新闻"]
    assert results[0]["status"] == "failed"
    assert results[1]["status"] == "succeeded"
