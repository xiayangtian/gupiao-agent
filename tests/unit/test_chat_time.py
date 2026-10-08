from datetime import date, datetime
import importlib
from zoneinfo import ZoneInfo

import pytest

try:
    chat_time = importlib.import_module("webapp.chat_time")
except ModuleNotFoundError:
    chat_time = None


def _api():
    assert chat_time is not None, "webapp.chat_time must be implemented before date windows can be resolved"
    return chat_time


SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 5, 10, 0, tzinfo=SH)


def test_last_week_is_previous_calendar_week_in_shanghai():
    api = _api()
    window = api.resolve_market_window("A股上周复盘", NOW)

    assert window == api.MarketWindow(
        label="上周", kind="calendar_week", start_date=date(2026, 9, 28),
        end_date=date(2026, 10, 4), trading_days=None,
    )


def test_this_week_ends_at_request_date_and_excludes_future_days():
    window = _api().resolve_market_window("本周股价走势", NOW)

    assert window.kind == "calendar_week"
    assert window.start_date == date(2026, 10, 5)
    assert window.end_date == date(2026, 10, 5)


def test_today_is_a_point_in_time_window():
    window = _api().resolve_market_window("今日复盘", NOW)

    assert window is not None
    assert window.kind == "explicit"
    assert window.start_date == date(2026, 10, 5)
    assert window.end_date == date(2026, 10, 5)


def test_recent_five_trading_days_requires_actual_returned_bars():
    window = _api().resolve_market_window("近五交易日", NOW)

    assert window.kind == "rolling_trading_days"
    assert window.trading_days == 5
    assert window.end_date == date(2026, 10, 5)


def test_explicit_date_range_is_inclusive():
    window = _api().resolve_market_window("2026-09-29至2026-10-02股价", NOW)

    assert window.kind == "explicit"
    assert window.start_date == date(2026, 9, 29)
    assert window.end_date == date(2026, 10, 2)


def test_market_bar_selection_sorts_deduplicates_and_ignores_outside_dates():
    api = _api()
    window = api.resolve_market_window("上周", NOW)
    rows = [
        {"date": "2026-10-02", "close": 5},
        {"date": "2026-09-28", "close": 1},
        {"date": "2026-09-30", "close": 3},
        {"date": "2026-10-01", "close": 4},
        {"date": "2026-09-29", "close": 2},
        {"date": "2026-10-02", "close": 99},
        {"date": "2026-10-05", "close": 6},
    ]

    selected = api.select_market_bars(rows, window)

    assert selected.status == "complete"
    assert selected.covered_dates == (
        "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02",
    )
    assert len(selected.bars) == 5
    assert selected.bars[-1]["close"] == 5


def test_market_bar_selection_marks_short_or_invalid_coverage_partial():
    api = _api()
    window = api.resolve_market_window("近五交易日", NOW)

    selected = api.select_market_bars([
        {"date": "2026-10-02", "close": 5},
        {"date": "2026-09-30", "close": 3},
        {"date": "not-a-date", "close": 8},
        {"date": "2026-10-06", "close": 9},
    ], window)

    assert selected.status == "partial"
    assert selected.covered_dates == ("2026-09-30", "2026-10-02")


def test_financial_period_requires_explicit_period_or_range():
    api = _api()
    assert api.resolve_financial_period("2025年营收趋势") == "2025年"
    assert api.resolve_financial_period("近三年净利润趋势") == "近三年"
    assert api.resolve_financial_period("2024年至2025年营收同比") == "2024年至2025年"
    assert api.resolve_financial_period("营收趋势") is None


def test_naive_now_is_interpreted_in_shanghai_timezone():
    window = _api().resolve_market_window("上周", datetime(2026, 10, 5, 10, 0))

    assert window.start_date == date(2026, 9, 28)
    assert window.end_date == date(2026, 10, 4)
    assert window.timezone == "Asia/Shanghai"
