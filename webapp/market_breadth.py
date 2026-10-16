"""Bounded A-share breadth aggregation and per-process snapshot cache."""

from __future__ import annotations

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date, datetime
from numbers import Real
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MIN_VOLUME_RATIO_COVERAGE = 0.95
_SNAPSHOT_FETCH_TIMEOUT_SECONDS = 80
_INDEX_SYMBOLS = frozenset({"sh000001", "sz399001", "sz399006", "sh000688"})
_MAX_INDEX_LOOKBACK_DAYS = 14
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _number(row: Mapping[str, Any], *keys: str, non_negative: bool = False) -> float | None:
    value = next((row[key] for key in keys if key in row), None)
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        return None
    number = float(value)
    if non_negative and number < 0:
        return None
    return number


def _code(row: Mapping[str, Any]) -> str:
    raw = row.get("代码", row.get("code", row.get("symbol", "")))
    return str(raw).strip() if raw is not None else ""


def _unique_snapshot_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], bool]:
    unique: dict[str, Mapping[str, Any]] = {}
    duplicate = False
    for row in rows:
        if not isinstance(row, Mapping):
            duplicate = True
            continue
        code = _code(row)
        if not code:
            duplicate = True
            continue
        if code in unique:
            duplicate = True
            continue
        unique[code] = row
    return list(unique.values()), duplicate


def _pool_count(rows: Sequence[Mapping[str, Any]] | None, pool_date: str | None,
                trade_date: str) -> int | None:
    if rows is None or pool_date != trade_date or not rows:
        return None
    codes = {_code(row) for row in rows if isinstance(row, Mapping)}
    if not codes or "" in codes or len(codes) != len(rows):
        return None
    return len(codes)


def summarize_market_snapshot(
    rows: Sequence[Mapping[str, Any]],
    *,
    trade_date: str,
    as_of: str,
    limit_up_rows: Sequence[Mapping[str, Any]] | None,
    limit_down_rows: Sequence[Mapping[str, Any]] | None,
    limit_up_date: str | None,
    limit_down_date: str | None,
) -> dict[str, Any]:
    """Summarize one provider snapshot; expose coverage and never turn missing data into zero."""
    if not _valid_date(trade_date):
        raise ValueError("trade_date must be an ISO date")
    if not isinstance(as_of, str) or not as_of:
        raise ValueError("as_of is required")
    try:
        datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("as_of must be ISO 8601") from exc
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError("rows must be a sequence")

    unique, duplicate_rows = _unique_snapshot_rows(rows)
    total = len(unique)
    classified = 0
    advancing = declining = unchanged = 0
    volume_sum = turnover_sum = 0.0
    valid_volume_rows = valid_turnover_rows = 0
    ratio_volume = ratio_baseline_volume = 0.0
    ratio_samples = active_volume_rows = 0

    for row in unique:
        change = _number(row, "涨跌幅", "change_pct", "change_percent")
        if change is not None:
            classified += 1
            if change > 0:
                advancing += 1
            elif change < 0:
                declining += 1
            else:
                unchanged += 1

        volume = _number(row, "成交量", "volume", non_negative=True)
        turnover = _number(row, "成交额", "turnover", non_negative=True)
        ratio = _number(row, "量比", "volume_ratio", non_negative=True)
        if volume is not None:
            volume_sum += volume
            valid_volume_rows += 1
            if volume > 0:
                active_volume_rows += 1
                if ratio is not None and ratio > 0:
                    ratio_volume += volume
                    ratio_baseline_volume += volume / ratio
                    ratio_samples += 1
        if turnover is not None:
            turnover_sum += turnover
            valid_turnover_rows += 1

    breadth_complete = total > 0 and classified == total and not duplicate_rows
    volume_complete = total > 0 and valid_volume_rows == total and not duplicate_rows
    turnover_complete = total > 0 and valid_turnover_rows == total and not duplicate_rows
    ratio_coverage = ratio_samples / active_volume_rows if active_volume_rows else 0.0
    weighted_ratio = (
        ratio_volume / ratio_baseline_volume
        if ratio_baseline_volume > 0 and ratio_coverage >= _MIN_VOLUME_RATIO_COVERAGE
        else None
    )
    up_count = _pool_count(limit_up_rows, limit_up_date, trade_date)
    down_count = _pool_count(limit_down_rows, limit_down_date, trade_date)

    limitations = []
    if not breadth_complete:
        limitations.append("涨跌分类或证券快照覆盖不完整")
    if not volume_complete:
        limitations.append("成交量字段覆盖不完整")
    if not turnover_complete:
        limitations.append("成交额字段覆盖不完整")
    if weighted_ratio is None:
        limitations.append("有效个股量比覆盖不足")
    if up_count is None:
        limitations.append("涨停池缺失、为空或日期/代码校验不通过")
    if down_count is None:
        limitations.append("跌停池缺失、为空或日期/代码校验不通过")

    status = "success" if not limitations else "partial"
    return {
        "status": status,
        "trade_date": trade_date,
        "as_of": as_of,
        "universe_rows": len(rows),
        "unique_security_rows": total,
        "classified_rows": classified,
        "advancing_count": advancing if breadth_complete else None,
        "declining_count": declining if breadth_complete else None,
        "unchanged_count": unchanged if breadth_complete else None,
        "total_volume": volume_sum if volume_complete else None,
        "total_turnover": turnover_sum if turnover_complete else None,
        "volume_sample_count": valid_volume_rows,
        "turnover_sample_count": valid_turnover_rows,
        "volume_ratio": weighted_ratio,
        "volume_ratio_sample_count": ratio_samples,
        "volume_ratio_active_rows": active_volume_rows,
        "volume_ratio_coverage": ratio_coverage,
        "limit_up_count": up_count,
        "limit_down_count": down_count,
        "limitations": "；".join(limitations),
    }


def _frame_rows(value: Any) -> list[Mapping[str, Any]]:
    if value is None:
        raise ValueError("empty_provider_data")
    if hasattr(value, "to_dict"):
        try:
            rows = value.to_dict(orient="records")
        except TypeError:
            rows = value.to_dict("records")
    else:
        rows = value
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError("unsupported_provider_data")
    return [row for row in rows if isinstance(row, Mapping)]


def load_market_snapshot(trade_date: str, *, akshare_module: Any = None,
                         now: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Read the fixed, read-only AkShare snapshot and both date-bounded limit pools."""
    if not _valid_date(trade_date):
        raise ValueError("trade_date must be an ISO date")
    if akshare_module is None:
        import akshare as akshare_module  # type: ignore[no-redef]

    quote_rows = _frame_rows(akshare_module.stock_zh_a_spot_em())
    if not quote_rows:
        raise ValueError("empty_provider_data")
    fetched_at = (now or (lambda: datetime.now(_SHANGHAI)))()
    if not isinstance(fetched_at, datetime):
        raise ValueError("now must return datetime")
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=_SHANGHAI)
    else:
        fetched_at = fetched_at.astimezone(_SHANGHAI)
    date_arg = trade_date.replace("-", "")
    try:
        up_rows = _frame_rows(akshare_module.stock_zt_pool_em(date_arg))
        up_date = trade_date if up_rows else None
    except Exception:
        up_rows, up_date = None, None
    try:
        down_rows = _frame_rows(akshare_module.stock_zt_pool_dtgc_em(date_arg))
        down_date = trade_date if down_rows else None
    except Exception:
        down_rows, down_date = None, None
    return summarize_market_snapshot(
        quote_rows, trade_date=trade_date, as_of=fetched_at.isoformat(timespec="seconds"),
        limit_up_rows=up_rows, limit_down_rows=down_rows,
        limit_up_date=up_date, limit_down_date=down_date,
    )


def load_a_share_index_kline(symbol: str, *, start_date: str, end_date: str,
                              akshare_module: Any = None) -> dict[str, Any]:
    """Fetch a bounded daily history for one fixed A-share index using AkShare."""
    if symbol not in _INDEX_SYMBOLS:
        raise ValueError("unsupported_index_symbol")
    if not _valid_date(start_date) or not _valid_date(end_date):
        raise ValueError("invalid_index_date")
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end or (end - start).days > _MAX_INDEX_LOOKBACK_DAYS:
        raise ValueError("index_date_range_out_of_bounds")
    if akshare_module is None:
        import akshare as akshare_module  # type: ignore[no-redef]

    frame = akshare_module.index_zh_a_hist(
        symbol=symbol[2:], period="daily", start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
    )
    rows = _frame_rows(frame)
    bars = []
    for row in rows:
        raw_date = row.get("日期", row.get("date"))
        day = raw_date.strip() if isinstance(raw_date, str) else ""
        close = _number(row, "收盘", "close", non_negative=True)
        if not _valid_date(day) or not start <= date.fromisoformat(day) <= end or close is None:
            continue
        bar = {"date": day, "close": close, "symbol": symbol, "kline_period": "day"}
        volume = _number(row, "成交量", "volume", non_negative=True)
        if volume is not None:
            bar["volume"] = volume
        bars.append(bar)
    if not bars:
        raise ValueError("empty_provider_data")
    bars.sort(key=lambda item: item["date"])
    return {"as_of": bars[-1]["date"], "bars": bars}


class MarketSnapshotCache:
    """Single-flight cache keyed by trading date; results never cross trading dates."""

    def __init__(self, fetch: Callable[[str], Mapping[str, Any]], *, ttl_seconds: float = 900,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not callable(fetch) or not callable(clock):
            raise ValueError("fetch and clock must be callable")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._fetch = fetch
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._value: tuple[str, float, Mapping[str, Any]] | None = None

    def get(self, trade_date: str) -> Mapping[str, Any]:
        if not _valid_date(trade_date):
            raise ValueError("trade_date must be an ISO date")
        with self._lock:
            now = self._clock()
            cached = self._value
            if cached and cached[0] == trade_date and now - cached[1] < self._ttl_seconds:
                return dict(cached[2])
            result = dict(self._fetch(trade_date))
            if result.get("trade_date") != trade_date:
                raise ValueError("snapshot date mismatch")
            self._value = (trade_date, self._clock(), dict(result))
            return dict(result)


_default_cache = MarketSnapshotCache(load_market_snapshot)
_snapshot_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="a-share-snapshot")


def get_market_snapshot(trade_date: str, *, timeout_seconds: float = _SNAPSHOT_FETCH_TIMEOUT_SECONDS,
                        cache: MarketSnapshotCache | None = None, executor: Any = None) -> Mapping[str, Any]:
    """Process-wide cache with a bounded wait; a slow provider may finish warming the cache later."""
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    fetcher = cache or _default_cache
    worker = executor or _snapshot_executor
    future = worker.submit(fetcher.get, trade_date)
    try:
        return future.result(timeout=float(timeout_seconds))
    except FutureTimeoutError:
        return {
            "status": "unavailable", "trade_date": trade_date, "as_of": "",
            "limitations": f"全市场快照超过 {int(timeout_seconds)} 秒等待上限；后台获取可能仍在进行",
        }
