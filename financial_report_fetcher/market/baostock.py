"""Narrow, serialized BaoStock access for fixed A-share index daily bars."""

from __future__ import annotations

import importlib
import math
import re
from datetime import date, timedelta
from threading import Lock
from typing import Any


_INDEX_CODES = {
    "sh000001": "sh.000001",
    "sz399001": "sz.399001",
    "sz399006": "sz.399006",
    "sh000688": "sh.000688",
}
_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,pctChg"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_LOOKBACK_DAYS = 14
_SESSION_LOCK = Lock()


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _number(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _provider_module(value: Any = None) -> Any:
    return value if value is not None else importlib.import_module("baostock")


def load_index_kline(
    symbol: str,
    *,
    start_date: str,
    end_date: str,
    baostock_module: Any = None,
) -> dict[str, Any]:
    """Fetch unadjusted daily bars for one fixed index over an inclusive short window.

    The SDK query extends its end by one calendar day to tolerate provider end-boundary
    semantics; returned rows are always filtered back to the caller's exact closed interval.
    """
    provider_code = _INDEX_CODES.get(symbol)
    if provider_code is None:
        raise ValueError("unsupported_index_symbol")
    if not _valid_date(start_date) or not _valid_date(end_date):
        raise ValueError("invalid_index_date")
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end or (end - start).days > _MAX_LOOKBACK_DAYS:
        raise ValueError("index_date_range_out_of_bounds")
    try:
        provider_end = (end + timedelta(days=1)).isoformat()
    except OverflowError:
        raise ValueError("invalid_index_date") from None

    sdk = _provider_module(baostock_module)
    with _SESSION_LOCK:
        login = sdk.login()
        if str(getattr(login, "error_code", "")) != "0":
            raise ValueError("login_failed")
        try:
            try:
                result_set = sdk.query_history_k_data_plus(
                    provider_code, _FIELDS, start_date=start_date, end_date=provider_end,
                    frequency="d", adjustflag="3",
                )
            except Exception:
                raise ValueError("provider_error") from None
            if str(getattr(result_set, "error_code", "")) != "0":
                raise ValueError("provider_error")

            bars = []
            try:
                while result_set.next():
                    values = result_set.get_row_data()
                    if not isinstance(values, (list, tuple)) or len(values) < 10:
                        continue
                    raw_date, raw_code, *raw_values = values[:10]
                    if (not _valid_date(raw_date) or raw_code != provider_code
                            or not start <= date.fromisoformat(raw_date) <= end):
                        continue
                    names = ("open", "high", "low", "close", "preclose", "volume", "amount", "pctChg")
                    parsed = {name: _number(value) for name, value in zip(names, raw_values)}
                    close = parsed.get("close")
                    if close is None or close <= 0:
                        continue
                    bar = {"date": raw_date, "symbol": symbol, "kline_period": "day"}
                    bar.update({name: value for name, value in parsed.items() if value is not None})
                    bars.append(bar)
            except Exception:
                raise ValueError("provider_error") from None
            if str(getattr(result_set, "error_code", "")) != "0":
                raise ValueError("provider_error")
            if not bars:
                raise ValueError("empty_provider_data")
            bars.sort(key=lambda row: row["date"])
            return {"as_of": bars[-1]["date"], "bars": bars}
        finally:
            try:
                sdk.logout()
            except Exception:
                raise ValueError("logout_failed") from None
