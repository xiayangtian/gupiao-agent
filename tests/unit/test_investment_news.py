from threading import Event
import time

from tests.unit.investment_news_fixtures import (
    FakeNewsSource,
    RecordingAIClient,
    make_item,
    make_service,
)


def _wait_for_terminal(store, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = store.list_runs(1)
        if runs and runs[0]["status"] not in {"queued", "running"}:
            return runs[0]
        time.sleep(0.01)
    raise AssertionError("news run did not finish before test timeout")


def test_manual_collection_never_generates_digest(tmp_path):
    ai = RecordingAIClient()
    source = FakeNewsSource(market_items=[make_item(1)])
    service, store = make_service(tmp_path, source=source, ai_client=ai)

    result = service.collect_now(trigger="manual")

    assert result["status"] == "succeeded"
    assert ai.calls == []
    assert len(store.get_day_items("2026-10-14")) == 1
    service.shutdown()


def test_collection_persists_successes_and_reports_partial_stock_failure(tmp_path):
    market_item = make_item(1)
    stock_item = make_item(2, symbol="600002")
    source = FakeNewsSource(
        market_items=[market_item],
        stock_items_by_code={"600002": [stock_item]},
        failures={"600001": RuntimeError("temporary source failure")},
    )
    service, store = make_service(tmp_path, source=source)
    store.add_watchlist("600001", "公司一")
    store.add_watchlist("600002", "公司二")

    result = service.collect_now(trigger="manual")

    assert result["status"] == "partial"
    assert result["item_count"] == 2
    assert [entry["code"] for entry in result["source_results"] if entry["status"] == "failed"] == ["600001"]
    assert {row["id"] for row in store.get_day_items("2026-10-14")} == {market_item.id, stock_item.id}
    service.shutdown()


def test_failed_collection_keeps_previously_persisted_news(tmp_path):
    class FailedMarketSource:
        def fetch_market(self):
            raise RuntimeError("provider timeout")
        def fetch_stock(self, code, name):
            raise RuntimeError("provider timeout")

    service, store = make_service(tmp_path, source=FailedMarketSource())
    previous = make_item(99)
    store.upsert_items([previous])

    result = service.collect_now(trigger="manual")

    assert result["status"] == "failed"
    assert [row["id"] for row in store.get_day_items("2026-10-14")] == [previous.id]
    service.shutdown()


def test_duplicate_active_collection_reuses_run_id(tmp_path):
    entered = Event()
    release = Event()

    class BlockingSource:
        def fetch_market(self):
            entered.set()
            assert release.wait(3)
            return []
        def fetch_stock(self, code, name):
            return []

    service, store = make_service(tmp_path, source=BlockingSource())
    first = service.submit_collection("manual")
    assert entered.wait(2)
    second = service.submit_collection("manual")

    assert second["run_id"] == first["run_id"]
    release.set()
    finished = _wait_for_terminal(store)
    assert finished["run_id"] == first["run_id"]
    assert finished["status"] == "succeeded"
    service.shutdown()
