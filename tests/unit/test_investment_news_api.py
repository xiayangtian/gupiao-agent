from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import webapp.server as server
from tests.unit.investment_news_fixtures import FakeNewsSource, RecordingAIClient, make_item, make_service


class FakeStockIndex:
    is_ready = True

    def start(self):
        return None

    def wait_ready(self, timeout=5.0):
        return True

    def is_valid_code(self, code):
        return len(code) == 6 and code.isdigit()

    def company_name(self, code):
        return f"测试股票{code}" if self.is_valid_code(code) else None


@pytest.fixture
def news_client(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTMENT_NEWS_SCHEDULER_ENABLED", "0")
    service, store = make_service(
        tmp_path,
        source=FakeNewsSource(market_items=[make_item(1)]),
        ai_client=RecordingAIClient(),
    )
    service.clock = lambda: datetime(2026, 10, 14, 3, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(server, "stock_index", FakeStockIndex())
    monkeypatch.setattr(server, "investment_news_service", service)
    monkeypatch.setattr(server, "investment_news_scheduler", None)
    with TestClient(server.app) as client:
        yield client, service, store
    service.shutdown()


def test_app_lifecycle_starts_and_stops_investment_news_components(tmp_path, monkeypatch):
    service, store = make_service(tmp_path)
    calls = []

    class FakeScheduler:
        def start(self):
            calls.append("start")
        def stop(self):
            calls.append("stop")

    monkeypatch.setattr(server, "investment_news_service", None)
    monkeypatch.setattr(server, "investment_news_scheduler", None)
    monkeypatch.setattr(server, "investment_news_service_factory", lambda: service)
    monkeypatch.setattr(server, "investment_news_scheduler_factory", lambda active: FakeScheduler())
    monkeypatch.setenv("INVESTMENT_NEWS_SCHEDULER_ENABLED", "1")

    server._startup_investment_news()
    assert server.investment_news_service is service
    assert calls == ["start"]
    server._shutdown_investment_news()

    assert calls == ["start", "stop"]
    assert server.investment_news_service is None


def test_list_api_validates_day_kind_pagination_and_limits_page_to_twenty(news_client):
    client, service, store = news_client
    store.upsert_items([make_item(index) for index in range(25)])

    response = client.get("/api/investment-news", params={
        "day": "2026-10-14", "kind": "market", "limit": 20, "offset": 0,
    })
    assert response.status_code == 200
    assert response.json()["total"] == 25
    assert len(response.json()["items"]) == 20

    assert client.get("/api/investment-news", params={"day": "not-a-day"}).status_code == 422
    assert client.get("/api/investment-news", params={"kind": "unrecognized"}).status_code == 422
    assert client.get("/api/investment-news", params={"limit": 21}).status_code == 422


def test_watchlist_api_enforces_a_share_code_and_twenty_item_cap(news_client):
    client, service, store = news_client
    added = client.post("/api/investment-news/watchlist", json={"code": "600900"})
    assert added.status_code == 201
    assert added.json()["code"] == "600900"
    assert client.post("/api/investment-news/watchlist", json={"code": "bad"}).status_code == 422

    store.remove_watchlist("600900")
    for index in range(20):
        store.add_watchlist(f"{600000 + index}", f"公司{index}")
    response = client.post("/api/investment-news/watchlist", json={"code": "600999"})
    assert response.status_code == 409
    assert client.delete("/api/investment-news/watchlist/600000").status_code == 204


def test_manual_collection_and_digest_are_distinct_api_operations(news_client):
    client, service, store = news_client

    collect = client.post("/api/investment-news/collect")
    assert collect.status_code == 202
    service.wait_for_run(collect.json()["run_id"], timeout=3)
    assert service.ai_client.calls == []
    source_calls_after_collection = len(service.source.calls)

    digest = client.post("/api/investment-news/digest")
    assert digest.status_code == 202
    service.wait_for_run(digest.json()["run_id"], timeout=3)

    assert service.ai_client.calls
    assert len(service.source.calls) == source_calls_after_collection
    assert client.get("/api/investment-news/runs").status_code == 200
    status = client.get("/api/investment-news/status")
    assert status.status_code == 200
    latest = status.json()["latest_successful_runs"]
    assert latest["collection"]["status"] == "succeeded"
    assert latest["digest"]["status"] == "succeeded"
    assert latest["collection"]["finished_at"]
    assert latest["digest"]["finished_at"]
    assert client.get("/api/investment-news/digest", params={"day": "2026-10-14"}).status_code == 200
