from dataclasses import replace
from datetime import datetime, timezone

import pytest

from webapp.investment_news_store import InvestmentNewsStore, NewsItem, WatchlistLimitError
from tests.unit.investment_news_fixtures import make_item


def test_url_dedup_preserves_distinct_related_stories(tmp_path):
    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    first = make_item(1, url="https://news.example/a", title="市场公告", symbol="600900")
    same_url = make_item(2, url="https://news.example/a?utm_source=feed", title="市场公告", symbol="600900")
    related_url = make_item(3, url="https://other.example/b", title="市场公告", symbol="600900")

    store.upsert_items([first, same_url, related_url])

    result = store.list_items("2026-10-14", "watchlist", "600900", "latest", 20, 0)
    assert result["total"] == 2
    assert len(result["items"]) == 2
    assert result["items"][0]["story_group_id"] == result["items"][1]["story_group_id"]
    assert {row["url"] for row in result["items"]} == {
        "https://news.example/a", "https://other.example/b"
    }


def test_daily_digest_keeps_only_the_latest_snapshot_per_day(tmp_path):
    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    store.save_digest("2026-10-14", {"summary": "morning"}, "hash-a", "model-a")
    store.save_digest("2026-10-14", {"summary": "afternoon"}, "hash-b", "model-b")
    store.save_digest("2026-10-13", {"summary": "previous day"}, "hash-c", "model-c")

    assert store.get_digest("2026-10-14")["payload"] == {"summary": "afternoon"}
    assert store.get_digest("2026-10-14")["model"] == "model-b"
    assert store.get_digest("2026-10-13")["payload"] == {"summary": "previous day"}


def test_daily_digest_hash_includes_watchlist_scope_changes(tmp_path):
    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    market_item = make_item(31)
    store.upsert_items([market_item])
    before = store.daily_input_hash("2026-10-14")

    store.upsert_items([replace(market_item, kind="watchlist", symbols=("600900",))])

    assert store.daily_input_hash("2026-10-14") != before


def test_list_filters_by_shanghai_day_kind_and_symbol_and_paginates(tmp_path):
    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    store.upsert_items([
        make_item(1, day="2026-10-14", symbol="600900"),
        make_item(2, day="2026-10-14", symbol="601398"),
        make_item(3, day="2026-10-13", symbol="600900"),
        make_item(4, day="2026-10-14"),
    ])

    first_page = store.list_items("2026-10-14", "watchlist", "600900", "latest", 1, 0)
    second_page = store.list_items("2026-10-14", "watchlist", "600900", "latest", 1, 1)

    assert first_page["total"] == 1
    assert len(first_page["items"]) == 1
    assert second_page["items"] == []
    market = store.list_items("2026-10-14", "market", None, "latest", 20, 0)
    assert market["total"] == 1
    assert market["items"][0]["id"] == "item-4"


def test_watchlist_limit_is_twenty_and_duplicate_is_idempotent(tmp_path):
    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    for code in range(20):
        store.add_watchlist(f"{600000 + code}", f"公司{code}")

    store.add_watchlist("600000", "公司0")
    assert len(store.get_watchlist()) == 20
    with pytest.raises(WatchlistLimitError):
        store.add_watchlist("600999", "超出上限")


def test_cleanup_removes_items_older_than_ninety_days(tmp_path):
    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    old = make_item(
        1,
        day="2026-07-15",
        collected_at="2026-07-15T02:00:00+00:00",
    )
    recent = make_item(
        2,
        day="2026-10-14",
        collected_at="2026-10-14T02:00:00+00:00",
    )
    store.upsert_items([old, recent])
    store.save_digest("2026-07-15", {"market_themes": [{"text": "old content"}]}, "old-hash", "test-model")
    store.save_digest("2026-10-14", {"market_themes": [{"text": "recent content"}]}, "new-hash", "test-model")
    run_id = store.create_run("collection", "manual")
    store.update_run(run_id, status="failed", result={"item_count": 0}, finished_at=now.isoformat())

    deleted = store.cleanup_expired(now)

    assert deleted == 1
    result = store.list_items("2026-07-15", "market", None, "latest", 20, 0)
    assert result["total"] == 0
    assert store.list_items("2026-10-14", "market", None, "latest", 20, 0)["total"] == 1
    assert store.get_digest("2026-07-15") is None
    assert store.get_digest("2026-10-14") is not None
    assert any(run["run_id"] == run_id for run in store.list_runs())


def test_digest_input_fingerprint_ignores_recollection_time_but_tracks_content(tmp_path):
    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    first = make_item(1)
    store.upsert_items([first])
    initial = store.daily_input_hash("2026-10-14")

    recollected = make_item(1, collected_at="2026-10-14T03:00:00+00:00")
    store.upsert_items([recollected])
    assert store.daily_input_hash("2026-10-14") == initial

    changed = NewsItem(
        id="item-1", title=first.title, url=first.url, snippet="更新后的片段",
        source_id=first.source_id, source_name=first.source_name,
        published_at=first.published_at, collected_at="2026-10-14T04:00:00+00:00",
        kind=first.kind, symbols=first.symbols,
    )
    store.upsert_items([changed])
    assert store.daily_input_hash("2026-10-14") != initial


def test_digest_input_is_unpaged_and_unknown_publication_time_stays_unknown(tmp_path):
    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    items = [make_item(index) for index in range(35)]
    unknown = NewsItem(
        id="item-unknown", title="发布时间未知", url="https://news.example/unknown",
        snippet="未知时间片段", source_id="eastmoney_test", source_name="测试来源",
        published_at=None, collected_at="2026-10-14T02:00:00+00:00",
        kind="market", symbols=(),
    )
    store.upsert_items([*items, unknown])

    page = store.list_items("2026-10-14", "market", None, "latest", 20, 0)
    digest_input = store.get_day_items("2026-10-14")
    unknown_row = next(row for row in digest_input if row["id"] == "item-unknown")

    assert len(page["items"]) == 20
    assert len(digest_input) == 36
    assert unknown_row["published_at"] is None
