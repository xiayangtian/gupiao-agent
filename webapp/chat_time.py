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
_ROLLING_YEAR_RE = re.compile(r"(?:近|最近|过去|近来)\s*(?P<count>[一二两三四五六七八九十\d]+)\s*年")
_YEAR_RE = re.compile(r"(?<!\d)(?P<year>\d{4})\s*年(?:度|报|年报|财报)?")
_QUARTER_RE = re.compile(r"(?P<year>\d{4})\s*年?\s*(?:第\s*)?(?P<quarter>[一二三四1234])\s*季度")
_MARKET_DURATION_RE = re.compile(
    r"(?:(?:最近|近来|近|过去(?:的)?)\s*)?"
    r"(?P<count>[0-9零〇一二两三四五六七八九十百千]+)\s*"
    r"(?P<unit>个?交易日|个?自然日|个?工作日|天|日|个?星期|周|个月|月|个?季度|年)"
    r"(?:之内|内|以来)?"
)
_HALF_YEAR_RE = re.compile(r"(?:(?:最近|近来|近|过去(?:的)?)\s*)?半年(?:之内|内|以来)?")
_FUZZY_MARKET_HORIZONS = ("短期", "短线", "中期", "中线", "长期", "长线")
_MAX_MARKET_WINDOW_MONTHS = 800
_MAX_MARKET_TRADING_DAYS = 800


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


def _chinese_count(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    digit_values = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
                    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    unit_values = {"十": 10, "百": 100, "千": 1000}
    total = section = number = 0
    for char in text:
        if char in digit_values:
            number = digit_values[char]
        elif char in unit_values:
            unit = unit_values[char]
            section += (number or 1) * unit
            number = 0
        else:
            return None
    return total + section + number


def _parse_market_duration(text: str) -> tuple[int, str] | None:
    if _HALF_YEAR_RE.search(text):
        return 6, "month"
    match = _MARKET_DURATION_RE.search(text)
    if not match:
        return None
    count = _chinese_count(match.group("count"))
    if count is None or count < 1:
        return None
    unit_text = match.group("unit")
    if "交易日" in unit_text or "工作日" in unit_text:
        return count, "trading_day"
    if unit_text == "日" and count == 5:
        return count, "trading_day"  # Keep the existing "近五日/近5日" five-trading-day contract.
    if unit_text in {"日", "天", "个自然日"}:
        return count, "day"
    if "周" in unit_text or "星期" in unit_text:
        return count, "week"
    if "季度" in unit_text:
        return count, "quarter"
    if unit_text in {"月", "个月"}:
        return count, "month"
    return count, "year"


def has_market_period_expression(question: str) -> bool:
    """Recognize explicit horizon syntax, including a validly recognized but over-limit span."""
    text = question or ""
    return bool(_HALF_YEAR_RE.search(text) or _MARKET_DURATION_RE.search(text))


def is_fuzzy_market_horizon(question: str) -> bool:
    text = question or ""
    if has_market_period_expression(text):
        return False
    return any(term in text for term in (*_FUZZY_MARKET_HORIZONS, "近期", "最近", "近来"))


def is_orphan_market_horizon(question: str) -> bool:
    text = (question or "").strip()
    if not text:
        return False
    generic = r"(?:(?:短期|短线|中期|中线|长期|长线)|(?:最近|近来|近|过去(?:的)?)?半年(?:之内|内|以来)?|(?:最近|近来|近|过去(?:的)?)?[0-9零〇一二两三四五六七八九十百千]+\s*(?:个?交易日|个?自然日|个?工作日|天|日|个?星期|周|个月|月|个?季度|年)(?:之内|内|以来)?)"
    return bool(re.fullmatch(rf"(?:(?:分析|看看|查看|请分析)\s*)?{generic}\s*(?:行情|走势|趋势|表现|股价)?[？?。]?", text))


def market_window_error(question: str) -> str | None:
    duration = _parse_market_duration(question or "")
    if duration is None:
        return None
    count, unit = duration
    months = count * {"year": 12, "quarter": 3, "month": 1}.get(unit, 0)
    if unit == "trading_day" and count > _MAX_MARKET_TRADING_DAYS:
        return "market_window_too_long"
    if unit == "day" and count > _MAX_MARKET_WINDOW_MONTHS * 31:
        return "market_window_too_long"
    if unit == "week" and count > _MAX_MARKET_WINDOW_MONTHS * 31 // 7:
        return "market_window_too_long"
    if months > _MAX_MARKET_WINDOW_MONTHS:
        return "market_window_too_long"
    return None


def _subtract_months(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


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


def resolve_financial_period_range(expression: str, now: datetime | None = None) -> tuple[date, date] | None:
    """Resolve supported financial-period expressions to bounded calendar dates."""
    text = expression or ""
    today = _request_date(now)
    date_range = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})至(\d{4})-(\d{2})-(\d{2})", text)
    year_range = re.fullmatch(r"(\d{4})年至(\d{4})年", text)
    year = re.fullmatch(r"(\d{4})年", text)
    rolling = re.fullmatch(r"近([一二两三四五六七八九十\d]+)年", text)
    quarter = re.fullmatch(r"(\d{4})年第([1-4])季度", text)
    if date_range:
        try:
            start = date(int(date_range.group(1)), int(date_range.group(2)), int(date_range.group(3)))
            end = date(int(date_range.group(4)), int(date_range.group(5)), int(date_range.group(6)))
        except ValueError:
            return None
        return (start, end) if start <= end else None
    if year_range:
        start_year, end_year = int(year_range.group(1)), int(year_range.group(2))
        return (date(start_year, 1, 1), date(end_year, 12, 31)) if start_year <= end_year else None
    if year:
        number = int(year.group(1))
        return date(number, 1, 1), date(number, 12, 31)
    if rolling:
        digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        count = min(20, max(1, digits.get(rolling.group(1), int(rolling.group(1)) if rolling.group(1).isdigit() else 1)))
        try:
            start = today.replace(year=today.year - count)
        except ValueError:
            start = today.replace(year=today.year - count, day=28)
        return start, today
    if quarter:
        number, q = int(quarter.group(1)), int(quarter.group(2))
        return (date(number, 3 * q - 2, 1), date(number, 3 * q, (date(number, 3 * q + 1, 1) - timedelta(days=1)).day)) if q < 4 else (date(number, 10, 1), date(number, 12, 31))
    return None


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
    """Resolve bounded market periods in Asia/Shanghai local dates.

    Vague recent wording defaults to five returned trading days only when the
    question is about market data; financial periods remain a separate contract.
    """
    text = question or ""
    financial_words = ("营收", "营业收入", "净利润", "净利", "财报", "年报", "半年报", "季报", "ROE", "毛利率", "现金流")
    market_words = ("股价", "股票", "个股", "行情", "走势", "大盘", "指数", "A股", "涨跌", "K线")
    if any(word.lower() in text.lower() for word in financial_words) and not any(word in text for word in market_words):
        return None
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
    duration = _parse_market_duration(text)
    if duration is not None:
        if market_window_error(text):
            return None
        count, unit = duration
        if unit == "trading_day":
            return MarketWindow(f"近{count}交易日", "rolling_trading_days", None, today, count)
        if unit == "day":
            return MarketWindow(f"近{count}自然日", "explicit", today - timedelta(days=count - 1), today, None)
        if unit == "week":
            days = count * 7
            return MarketWindow(f"近{count}周", "explicit", today - timedelta(days=days - 1), today, None)
        months = count * {"quarter": 3, "year": 12}.get(unit, 1)
        return MarketWindow(f"近{count}{'季度' if unit == 'quarter' else '年' if unit == 'year' else '个月'}",
                            "explicit", _subtract_months(today, months), today, None)

    fuzzy = next((term for term in _FUZZY_MARKET_HORIZONS if term in text), None)
    if fuzzy:
        if fuzzy in {"短期", "短线"}:
            return MarketWindow("短期（近五交易日）", "rolling_trading_days", None, today, 5)
        months = 6 if fuzzy in {"中期", "中线"} else 36
        label = "中期（近六个月）" if months == 6 else "长期（近三年）"
        return MarketWindow(label, "explicit", _subtract_months(today, months), today, None)
    if any(word in text for word in ("近期", "最近", "近来")) and any(word in text for word in market_words):
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
