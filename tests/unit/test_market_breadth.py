from webapp.market_breadth import (
    MarketSnapshotCache, get_market_snapshot, load_a_share_index_kline, load_market_snapshot,
    summarize_market_snapshot,
)


def test_summarize_market_snapshot_uses_volume_weighted_stock_ratio_and_counts_breadth():
    result = summarize_market_snapshot(
        [
            {"代码": "600001", "涨跌幅": 2.0, "成交量": 100, "成交额": 1000, "量比": 2.0},
            {"代码": "000001", "涨跌幅": -1.0, "成交量": 300, "成交额": 3000, "量比": 1.5},
            {"代码": "430001", "涨跌幅": 0.0, "成交量": 50, "成交额": 400, "量比": 1.0},
        ],
        trade_date="2026-10-15",
        as_of="2026-10-15T15:05:00+08:00",
        limit_up_rows=[{"代码": "600001"}, {"代码": "000001"}],
        limit_down_rows=[{"代码": "430001"}],
        limit_up_date="2026-10-15",
        limit_down_date="2026-10-15",
    )

    assert result["status"] == "success"
    assert result["trade_date"] == "2026-10-15"
    assert result["universe_rows"] == 3
    assert result["classified_rows"] == 3
    assert (result["advancing_count"], result["declining_count"], result["unchanged_count"]) == (1, 1, 1)
    assert result["total_volume"] == 450
    assert result["total_turnover"] == 4400
    assert result["volume_ratio"] == 450 / (100 / 2 + 300 / 1.5 + 50 / 1)
    assert result["volume_ratio_sample_count"] == 3
    assert result["limit_up_count"] == 2
    assert result["limit_down_count"] == 1


def test_invalid_ratio_and_missing_change_are_not_fabricated_as_zero():
    result = summarize_market_snapshot(
        [
            {"代码": "600001", "涨跌幅": 1.0, "成交量": 100, "成交额": 1000, "量比": 0},
            {"代码": "000001", "成交量": 20, "成交额": 200, "量比": 1.0},
        ],
        trade_date="2026-10-15",
        as_of="2026-10-15T15:05:00+08:00",
        limit_up_rows=None,
        limit_down_rows=None,
        limit_up_date=None,
        limit_down_date=None,
    )

    assert result["status"] == "partial"
    assert result["classified_rows"] == 1
    assert result["advancing_count"] is None
    assert result["declining_count"] is None
    assert result["unchanged_count"] is None
    assert result["volume_ratio"] is None
    assert result["limit_up_count"] is None
    assert result["limit_down_count"] is None


def test_loader_uses_only_fixed_date_bounded_read_only_akshare_calls():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    class Frame(list):
        def to_dict(self, orient=None):
            assert orient == "records"
            return list(self)

    class FakeAkShare:
        def __init__(self):
            self.calls = []

        def stock_zh_a_spot_em(self):
            self.calls.append(("spot",))
            return Frame([
                {"代码": "600001", "涨跌幅": 1, "成交量": 100, "成交额": 1000, "量比": 2},
                {"代码": "000001", "涨跌幅": -1, "成交量": 100, "成交额": 2000, "量比": 1},
            ])

        def stock_zt_pool_em(self, day):
            self.calls.append(("up", day))
            return Frame([{"代码": "600001"}])

        def stock_zt_pool_dtgc_em(self, day):
            self.calls.append(("down", day))
            return Frame([{"代码": "000001"}])

    akshare = FakeAkShare()
    result = load_market_snapshot(
        "2026-10-15", akshare_module=akshare,
        now=lambda: datetime(2026, 10, 15, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["status"] == "success"
    assert result["trade_date"] == "2026-10-15"
    assert result["limit_up_count"] == 1 and result["limit_down_count"] == 1
    assert akshare.calls == [("spot",), ("up", "20261015"), ("down", "20261015")]


def test_load_a_share_index_kline_normalizes_only_fixed_daily_symbol_and_window():
    class Frame(list):
        def to_dict(self, orient=None):
            assert orient == "records"
            return list(self)

    class FakeAkShare:
        def __init__(self):
            self.calls = []

        def index_zh_a_hist(self, **kwargs):
            self.calls.append(kwargs)
            return Frame([
                {"日期": "2026-10-15", "收盘": 3707.48, "成交量": 390000000},
                {"日期": "2026-10-16", "收盘": 3692.45, "成交量": 394000000},
            ])

    akshare = FakeAkShare()
    result = load_a_share_index_kline(
        "sh000001", start_date="2026-10-06", end_date="2026-10-16", akshare_module=akshare,
    )

    assert result["as_of"] == "2026-10-16"
    assert result["bars"] == [
        {"date": "2026-10-15", "close": 3707.48, "volume": 390000000,
         "symbol": "sh000001", "kline_period": "day"},
        {"date": "2026-10-16", "close": 3692.45, "volume": 394000000,
         "symbol": "sh000001", "kline_period": "day"},
    ]
    assert akshare.calls == [{
        "symbol": "000001", "period": "daily", "start_date": "20261006", "end_date": "20261016",
    }]


def test_load_a_share_index_kline_rejects_unapproved_symbols_and_wide_windows():
    class UnusedAkShare:
        def index_zh_a_hist(self, **_kwargs):
            raise AssertionError("invalid call must be rejected before provider access")

    for symbol, start, end in (
        ("600000", "2026-10-06", "2026-10-16"),
        ("sh000001", "2026-09-01", "2026-10-16"),
        ("sh000001", "2026-10-17", "2026-10-16"),
    ):
        try:
            load_a_share_index_kline(symbol, start_date=start, end_date=end, akshare_module=UnusedAkShare())
        except ValueError:
            pass
        else:
            raise AssertionError("invalid index request must fail closed")


def test_cache_single_flights_by_date_and_does_not_reuse_prior_trading_day():
    now = [100.0]
    calls = []

    def fetch(day):
        calls.append(day)
        return {"trade_date": day, "as_of": f"{day}T15:00:00+08:00"}

    cache = MarketSnapshotCache(fetch, ttl_seconds=60, clock=lambda: now[0])
    first = cache.get("2026-10-15")
    now[0] += 20
    second = cache.get("2026-10-15")
    third = cache.get("2026-10-16")

    assert first == second
    assert third["trade_date"] == "2026-10-16"
    assert calls == ["2026-10-15", "2026-10-16"]


def test_snapshot_wait_is_bounded_and_a_slow_provider_is_reported_as_timeout():
    from concurrent.futures import TimeoutError as FutureTimeoutError

    class PendingFuture:
        def result(self, timeout):
            assert timeout == 80
            raise FutureTimeoutError()

    class PendingExecutor:
        def submit(self, function, trade_date):
            assert callable(function)
            assert trade_date == "2026-10-15"
            return PendingFuture()

    class UnusedCache:
        def get(self, _trade_date):
            raise AssertionError("wait timeout must not run this synchronously")

    result = get_market_snapshot("2026-10-15", cache=UnusedCache(), executor=PendingExecutor())
    assert result["status"] == "unavailable"
    assert "80 秒" in result["limitations"]


def test_cache_single_flights_concurrent_requests_for_the_same_date():
    import threading
    import time

    calls = []

    def fetch(day):
        calls.append(day)
        time.sleep(0.02)
        return {"trade_date": day, "as_of": day + "T15:00:00+08:00"}

    cache = MarketSnapshotCache(fetch)
    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.get("2026-10-15"))) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert len(results) == 4
    assert all(item["trade_date"] == "2026-10-15" for item in results)


def test_cache_does_not_store_failed_or_mismatched_snapshots():
    calls = []

    def fetch(day):
        calls.append(day)
        if len(calls) == 1:
            raise RuntimeError("provider unavailable")
        return {"trade_date": "2026-10-14"}

    cache = MarketSnapshotCache(fetch, ttl_seconds=60)
    try:
        cache.get("2026-10-15")
    except RuntimeError:
        pass
    else:
        raise AssertionError("provider exception should propagate")
    try:
        cache.get("2026-10-15")
    except ValueError as error:
        assert "date mismatch" in str(error)
    else:
        raise AssertionError("mismatched data date should not be cached")
    assert calls == ["2026-10-15", "2026-10-15"]


def test_mismatched_pool_dates_and_duplicate_or_invalid_rows_do_not_claim_full_coverage():
    result = summarize_market_snapshot(
        [
            {"代码": "600001", "涨跌幅": 1.0, "成交量": 100, "成交额": 1000, "量比": 2.0},
            {"代码": "600001", "涨跌幅": 1.0, "成交量": 100, "成交额": 1000, "量比": 2.0},
            {"代码": "000001", "涨跌幅": float("nan"), "成交量": -1, "成交额": 200, "量比": 1.0},
        ],
        trade_date="2026-10-15",
        as_of="2026-10-15T15:05:00+08:00",
        limit_up_rows=[{"代码": "600001"}],
        limit_down_rows=[{"代码": "000001"}],
        limit_up_date="2026-10-14",
        limit_down_date="2026-10-15",
    )

    assert result["status"] == "partial"
    assert result["universe_rows"] == 3
    assert result["unique_security_rows"] == 2
    assert result["limit_up_count"] is None
    assert result["limit_down_count"] == 1
