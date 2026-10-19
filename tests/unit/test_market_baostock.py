from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import sleep

import pytest

from financial_report_fetcher.market.baostock import load_index_kline


class _Reply:
    def __init__(self, error_code="0", error_msg=""):
        self.error_code = error_code
        self.error_msg = error_msg


class _ResultSet:
    def __init__(self, rows, error_code="0"):
        self.rows = list(rows)
        self.error_code = error_code
        self._index = 0
        self._current = None

    def next(self):
        if self._index >= len(self.rows):
            return False
        self._current = self.rows[self._index]
        self._index += 1
        return True

    def get_row_data(self):
        return self._current


class _FakeBaoStock:
    def __init__(self, rows, *, login_code="0", query_code="0", query_error=False):
        self.rows = rows
        self.login_code = login_code
        self.query_code = query_code
        self.query_error = query_error
        self.login_calls = 0
        self.logout_calls = 0
        self.query_calls = []
        self.active_sessions = 0
        self.max_active_sessions = 0
        self._state_lock = Lock()

    def login(self):
        with self._state_lock:
            self.login_calls += 1
            self.active_sessions += 1
            self.max_active_sessions = max(self.max_active_sessions, self.active_sessions)
        sleep(0.01)
        return _Reply(self.login_code, "login failed" if self.login_code != "0" else "")

    def query_history_k_data_plus(self, code, fields, **kwargs):
        self.query_calls.append((code, fields, kwargs))
        sleep(0.01)
        if self.query_error:
            raise RuntimeError("query exploded")
        return _ResultSet(self.rows, self.query_code)

    def logout(self):
        with self._state_lock:
            self.logout_calls += 1
            self.active_sessions -= 1


def _row(day, code="sh.000001", close="3692.45"):
    return [day, code, "3700", "3710", "3680", close, "3680", "123456", "789000", "0.34"]


def test_baostock_index_mapping_query_fields_and_closed_date_window():
    sdk = _FakeBaoStock([
        _row("2026-10-14"), _row("2026-10-15"), _row("2026-10-16"), _row("2026-10-17"),
    ])

    result = load_index_kline(
        "sh000001", start_date="2026-10-15", end_date="2026-10-16", baostock_module=sdk,
    )

    assert result["as_of"] == "2026-10-16"
    assert [bar["date"] for bar in result["bars"]] == ["2026-10-15", "2026-10-16"]
    assert result["bars"][-1]["close"] == 3692.45
    code, fields, kwargs = sdk.query_calls[0]
    assert code == "sh.000001"
    assert fields == "date,code,open,high,low,close,preclose,volume,amount,pctChg"
    assert kwargs == {
        "start_date": "2026-10-15", "end_date": "2026-10-17",
        "frequency": "d", "adjustflag": "3",
    }
    assert sdk.login_calls == sdk.logout_calls == 1


@pytest.mark.parametrize("symbol,provider_code", [
    ("sh000001", "sh.000001"),
    ("sz399001", "sz.399001"),
    ("sz399006", "sz.399006"),
    ("sh000688", "sh.000688"),
])
def test_baostock_maps_only_fixed_index_codes(symbol, provider_code):
    sdk = _FakeBaoStock([_row("2026-10-16", code=provider_code)])

    result = load_index_kline(
        symbol, start_date="2026-10-16", end_date="2026-10-16", baostock_module=sdk,
    )

    assert result["bars"][0]["symbol"] == symbol
    assert sdk.query_calls[0][0] == provider_code


@pytest.mark.parametrize("kwargs", [
    {"symbol": "600900", "start_date": "2026-10-15", "end_date": "2026-10-16"},
    {"symbol": "sh000001", "start_date": "2026-10-16", "end_date": "2026-10-15"},
    {"symbol": "sh000001", "start_date": "2026-10-01", "end_date": "2026-10-16"},
    {"symbol": "sh000001", "start_date": "2026-02-30", "end_date": "2026-03-01"},
])
def test_baostock_rejects_non_allowlisted_or_unbounded_requests(kwargs):
    with pytest.raises(ValueError):
        load_index_kline(**kwargs, baostock_module=_FakeBaoStock([]))


def test_baostock_sh000688_empty_rows_are_reported_as_unavailable_not_fabricated():
    sdk = _FakeBaoStock([])
    with pytest.raises(ValueError, match="empty_provider_data"):
        load_index_kline(
            "sh000688", start_date="2026-10-16", end_date="2026-10-16", baostock_module=sdk,
        )
    assert sdk.query_calls[0][0] == "sh.000688"
    assert sdk.logout_calls == 1


def test_baostock_empty_rows_and_provider_errors_never_return_market_data():
    with pytest.raises(ValueError, match="empty_provider_data"):
        load_index_kline(
            "sh000001", start_date="2026-10-16", end_date="2026-10-16",
            baostock_module=_FakeBaoStock([]),
        )

    failed_login = _FakeBaoStock([_row("2026-10-16")], login_code="10001001")
    with pytest.raises(ValueError, match="login_failed"):
        load_index_kline(
            "sh000001", start_date="2026-10-16", end_date="2026-10-16",
            baostock_module=failed_login,
        )
    assert failed_login.query_calls == []

    failed_query = _FakeBaoStock([_row("2026-10-16")], query_code="10001001")
    with pytest.raises(ValueError, match="provider_error"):
        load_index_kline(
            "sh000001", start_date="2026-10-16", end_date="2026-10-16",
            baostock_module=failed_query,
        )
    assert failed_query.logout_calls == 1


def test_baostock_logout_runs_when_query_raises():
    sdk = _FakeBaoStock([], query_error=True)
    with pytest.raises(ValueError, match="provider_error"):
        load_index_kline(
            "sh000001", start_date="2026-10-16", end_date="2026-10-16", baostock_module=sdk,
        )
    assert sdk.logout_calls == 1


def test_baostock_login_query_logout_sections_are_serialized():
    sdk = _FakeBaoStock([_row("2026-10-16")])

    def call():
        return load_index_kline(
            "sh000001", start_date="2026-10-16", end_date="2026-10-16", baostock_module=sdk,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: call(), range(2)))

    assert len(results) == 2
    assert sdk.max_active_sessions == 1
    assert sdk.login_calls == sdk.logout_calls == 2
