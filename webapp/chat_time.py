"""Deterministic financial-period and market-window parsing for chat."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATE_RANGE_RE = re.compile(
    r"(?P<start>\d{4}-\d{1,2}-\d{1,2})\s*(?:至|到|~|～|—|-)\s*"
    r"(?P<end>\d{4}-\d{1,2}-\d{1,2})"
)
_SINGLE_DATE_RE = re.compile(r"(?<!\d)(?P<date>\d{4}-\d{1,2}-\d{1,2})(?!\d)")
_FINANCIAL_RANGE_RE = re.compile(
    r"(?P<start>\d{4})\s*年?\s*(?:至|到|~|～|—|-)\s*(?P<end>\d{4})\s*年"
)
_ROLLING_YEAR_RE = re.compile(r"(?:近|最近|过去|近来)\s*(?P<count>[一二三四五六七八九十\d]+)\s*年")
_YEAR_RE = re.compile(r"(?<!\d)(?P<year>\d{4})\s*年(?:度|报|年报|财报)?")
_QUARTER_RE = re.compile(r"(?P<year>\d{4})\s*年?\s*(?:第\s*)?(?P<quarter>[一二三四1234])\s*季度")


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_SHANGHAI)
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _request_date(now: datetime | None) -> date:
    current = now or datetime.now(_SHANGHAI)
    if not isinstance(current, datetime):
        raise ValueError("now must be a datetime")
    if current.tzinfo is None:
        current = current.replace(tzinfo=_SHANGHAI)
    else:
        current = current.astimezone(_SHANGHAI)
    return current.date()


@dataclass(frozen=True)
class MarketWindow:
    label: str
    kind: Literal["calendar_week", "rolling_trading_days", "explicit"]
    start_date: date | None
    end_date: date | None
    trading_days: int | None
    timezone: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("window label must not be empty")
        if self.kind not in {"calendar_week", "rolling_trading_days", "explicit"}:
            raise ValueError("unsupported market window kind")
        if self.start_date is not None and not isinstance(self.start_date, date):
            raise ValueError("start_date must be a date or null")
        if self.end_date is not None and not isinstance(self.end_date, date):
            raise ValueError("end_date must be a date or null")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("window start_date must not exceed end_date")
        if self.kind == "rolling_trading_days" and (self.trading_days is None or self.trading_days < 1):
            raise ValueError("rolling window requires positive trading_days")
        if self.timezone != "Asia/Shanghai":
            raise ValueError("market windows use Asia/Shanghai")


@dataclass(frozen=True)
class WindowSelection:
    bars: tuple[Mapping[str, Any], ...]
    requested_label: str
    covered_dates: tuple[str, ...]
    status: Literal["complete", "partial", "unavailable"]


def resolve_financial_period(question: str) -> str | None:
    """Return an explicit fiscal period expression; never invent a default period."""
    text = question or ""
    match = _FINANCIAL_RANGE_RE.search(text)
    if match:
        return f"{match.group('start')}年至{match.group('end')}年"
    match = _ROLLING_YEAR_RE.search(text)
    if match:
        return f"近{match.group('count')}年"
    match = _QUARTER_RE.search(text)
    if match:
        q = {"一": "1", "二": "2", "三": "3", "四": "4"}.get(match.group("quarter"), match.group("quarter"))
        return f"{match.group('year')}年第{q}季度"
    match = _YEAR_RE.search(text)
    if match:
        return f"{match.group('year')}年"
    return None


def resolve_market_window(question: str, now: datetime | None = None) -> MarketWindow | None:
    """Resolve only explicit, bounded market periods in Asia/Shanghai local dates."""
    text = question or ""
    today = _request_date(now)
    match = _DATE_RANGE_RE.search(text)
    if match:
        start = _parse_date(match.group("start"))
        end = _parse_date(match.group("end"))
        if start is None or end is None or start > end:
            return None
        clipped_end = min(end, today)
        if start > clipped_end:
            return None
        return MarketWindow(match.group(0).strip(), "explicit", start, clipped_end, None)
    match = _SINGLE_DATE_RE.search(text)
    if match:
        target = _parse_date(match.group("date"))
        if target is None or target > today:
            return None
        return MarketWindow(match.group("date"), "explicit", target, target, None)
    if "今天" in text or "今日" in text:
        return MarketWindow("今日", "explicit", today, today, None)
    if "上周" in text:
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(days=7)
        return MarketWindow("上周", "calendar_week", start, start + timedelta(days=6), None)
    if "本周" in text or "这周" in text:
        start = today - timedelta(days=today.weekday())
        return MarketWindow("本周", "calendar_week", start, today, None)
    if re.search(r"(?:近|最近|过去)\s*(?:五|5)\s*(?:个)?交易日", text):
        return MarketWindow("近五交易日", "rolling_trading_days", None, today, 5)
    return None


def _bar_date(row: Mapping[str, Any]) -> date | None:
    for key in ("date", "trade_date", "datetime", "time", "day"):
        if key in row:
            parsed = _parse_date(row.get(key))
            if parsed is not None:
                return parsed
    return None


def select_market_bars(rows: Sequence[Mapping[str, Any]], window: MarketWindow) -> WindowSelection:
    """Filter/sort actual dated bars and describe conservative coverage."""
    if not isinstance(window, MarketWindow):
        raise ValueError("window must be a MarketWindow")
    valid: dict[date, Mapping[str, Any]] = {}
    invalid_seen = False
    for row in rows if isinstance(rows, (list, tuple)) else ():
        if not isinstance(row, Mapping):
            invalid_seen = True
            continue
        day = _bar_date(row)
        if day is None:
            invalid_seen = True
            continue
        if window.start_date is not None and day < window.start_date:
            continue
        if window.end_date is not None and day > window.end_date:
            continue
        valid.setdefault(day, dict(row))
    ordered_days = sorted(valid)
    if window.kind == "rolling_trading_days":
        ordered_days = ordered_days[-(window.trading_days or 0):]
    bars = tuple(valid[day] for day in ordered_days)
    dates = tuple(day.isoformat() for day in ordered_days)
    if not bars:
        status: Literal["complete", "partial", "unavailable"] = "unavailable"
    else:
        expected = window.trading_days if window.kind == "rolling_trading_days" else 5 if window.kind == "calendar_week" else None
        status = "complete" if (expected is None or len(bars) >= expected) and not invalid_seen else "partial"
    return WindowSelection(bars, window.label, dates, status)
