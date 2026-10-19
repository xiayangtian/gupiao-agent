"""Provider permission and conservative payload normalization for source execution."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from webapp.source_runtime import SourceCall, SourceCoverage, SourceResult, market_request_context

MAX_SOURCE_CHARS = 2000
MAX_ROWS = 50


@dataclass(frozen=True)
class SourceAccess:
    mcp_enabled: bool
    listed_tools: frozenset[str]
    whitelist: frozenset[str]
    mcp_allow: Callable[[], bool]
    web_enabled: bool
    tencent_enabled: bool
    akshare_enabled: bool = False
    deepseek_enabled: bool = False
    baostock_enabled: bool = False

    def permits(self, call: SourceCall) -> bool:
        if call.provider == "mcp":
            try:
                return (self.mcp_enabled and call.operation in self.listed_tools
                        and (not self.whitelist or call.operation in self.whitelist)
                        and bool(self.mcp_allow()))
            except Exception:
                return False
        if call.provider == "web":
            return self.web_enabled
        if call.provider == "deepseek_search":
            return self.deepseek_enabled and call.category == "web" and call.operation == "web_search"
        if call.provider == "tencent":
            return self.tencent_enabled
        if call.provider == "akshare":
            if not self.akshare_enabled or call.category != "market":
                return False
            if call.operation == "a_share_market_snapshot":
                trade_date = call.arguments.get("trade_date")
                try:
                    datetime.strptime(trade_date, "%Y-%m-%d")
                    valid_date = isinstance(trade_date, str) and len(trade_date) == 10
                except (TypeError, ValueError):
                    valid_date = False
                return set(call.arguments) == {"trade_date"} and valid_date
            if call.operation == "a_share_index_kline":
                if set(call.arguments) != {"symbol", "start_date", "end_date"}:
                    return False
                if call.arguments.get("symbol") not in {"sh000001", "sz399001", "sz399006", "sh000688"}:
                    return False
                start_raw, end_raw = call.arguments.get("start_date"), call.arguments.get("end_date")
                try:
                    start = date.fromisoformat(start_raw)
                    end = date.fromisoformat(end_raw)
                    if start.isoformat() != start_raw or end.isoformat() != end_raw:
                        return False
                except (TypeError, ValueError):
                    return False
                return start <= end and (end - start).days <= 14
            return False
        if call.provider == "baostock":
            if (not self.baostock_enabled or call.category != "market"
                    or call.operation != "a_share_index_kline"
                    or set(call.arguments) != {"symbol", "start_date", "end_date"}
                    or call.arguments.get("symbol") not in {"sh000001", "sz399001", "sz399006", "sh000688"}):
                return False
            start_raw, end_raw = call.arguments.get("start_date"), call.arguments.get("end_date")
            try:
                if not isinstance(start_raw, str) or not isinstance(end_raw, str):
                    return False
                start, end = date.fromisoformat(start_raw), date.fromisoformat(end_raw)
                if start.isoformat() != start_raw or end.isoformat() != end_raw:
                    return False
            except ValueError:
                return False
            return start <= end and (end - start).days <= 14
        if call.category == "local":
            return True
        return False


def _safe_cell(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool)) or (
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    )


def _parse(raw: Any) -> Any:
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        if stripped[:1] not in "[{\"":
            raise ValueError("unsupported_payload")
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            raise ValueError("unsupported_payload") from None
    return raw


_TENCENT_QUOTE_SCALAR_FIELDS = frozenset({
    "symbol", "code", "name", "price", "prev_close", "open", "volume", "time",
    "change", "change_pct", "high", "low", "amount_wan", "turnover_rate", "pe",
    "float_mv_yi", "total_mv_yi", "pb", "limit_up", "limit_down",
})
_TENCENT_QUOTE_ROW_KEYS = ("data", "results", "rows", "quotes")


def _project_tencent_quote(value: Any) -> Any:
    """Discard Tencent's nested order book while retaining known scalar quote fields."""
    def project_row(row: Any) -> Any:
        if not isinstance(row, Mapping):
            return row
        return {key: cell for key, cell in row.items()
                if key in _TENCENT_QUOTE_SCALAR_FIELDS and _safe_cell(cell)}

    if isinstance(value, list):
        return [project_row(row) for row in value]
    if not isinstance(value, Mapping):
        return value
    projected = {key: value[key] for key in ("as_of", "date", "trade_date")
                 if key in value and _safe_cell(value[key])}
    for key in _TENCENT_QUOTE_ROW_KEYS:
        if isinstance(value.get(key), list):
            projected[key] = [project_row(row) for row in value[key]]
            return projected
    return project_row(value)


def _extract_rows(value: Any, *, row_limit: int = MAX_ROWS) -> tuple[list[dict[str, Any]], str]:
    if isinstance(value, Mapping):
        if value.get("error") or value.get("success") is False or value.get("isError") is True:
            raise ValueError("provider_error")
        as_of = ""
        for key in ("as_of", "date", "trade_date", "time"):
            raw_time = value.get(key)
            if isinstance(raw_time, str) and raw_time.strip():
                try:
                    datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                    as_of = raw_time
                    break
                except ValueError:
                    continue
        rows = None
        for key in ("data", "results", "rows", "quotes", "bars"):
            if isinstance(value.get(key), list):
                rows = value[key]
                break
        if rows is None and isinstance(value.get("indices"), Mapping):
            rows = list(value["indices"].values())
        if not as_of and rows:
            row_times = [row.get("as_of") or row.get("date") or row.get("trade_date") or row.get("time")
                         for row in rows if isinstance(row, Mapping)]
            known_times = [value for value in row_times if isinstance(value, str) and value.strip()]
            if known_times and len(set(known_times)) == 1:
                try:
                    datetime.fromisoformat(known_times[0].replace("Z", "+00:00"))
                    as_of = known_times[0]
                except ValueError:
                    pass
        if rows is None and value and all(_safe_cell(v) for v in value.values()):
            rows = [dict(value)]
    elif isinstance(value, list):
        rows, as_of = value, ""
    else:
        rows, as_of = None, ""
    if not rows:
        raise ValueError("empty_payload")
    normalized = []
    for row in rows[:row_limit]:
        if not isinstance(row, Mapping) or not row or not all(
            isinstance(key, str) and _safe_cell(cell) for key, cell in row.items()
        ):
            raise ValueError("unsupported_payload")
        normalized.append({key: value[:2000] if isinstance(value, str) else value
                           for key, value in row.items()})
    if not any(cell is not None and cell != "" for row in normalized for cell in row.values()):
        raise ValueError("empty_payload")
    return normalized, as_of


def _request_coverage(call: SourceCall) -> SourceCoverage:
    arguments = call.arguments
    limit = (arguments.get("count") if call.provider == "tencent" and call.operation == "kline"
             else arguments.get("limit"))
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        limit = None
    query_window = None
    if call.operation == "stock_sector_fund_flow_rank":
        raw_window = arguments.get("days")
        if isinstance(raw_window, str) and len(raw_window) <= 40:
            query_window = raw_window
    elif call.operation == "index_prices":
        raw_window = arguments.get("period")
        if isinstance(raw_window, str) and len(raw_window) <= 40:
            query_window = raw_window
    elif call.provider == "tencent" and call.operation == "kline":
        raw_window = arguments.get("window") or arguments.get("period")
        if isinstance(raw_window, str) and len(raw_window) <= 40:
            query_window = raw_window
    elif call.provider in {"akshare", "baostock"} and call.operation == "a_share_index_kline":
        start, end = arguments.get("start_date"), arguments.get("end_date")
        if isinstance(start, str) and isinstance(end, str) and len(start) == len(end) == 10:
            query_window = f"{start}至{end}"
    return SourceCoverage(limit=limit, query_window=query_window)


def _response_coverage(call: SourceCall, value: Any, rows: list[dict[str, Any]], base: SourceCoverage) -> SourceCoverage:
    total = (
        value.get("total_rows")
        if call.provider == "mcp" and call.operation in {"stock_zt_pool", "stock_sector_fund_flow_rank"}
        and isinstance(value, Mapping) else None
    )
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        total = None
    dates: set[str] = set()
    for row in rows:
        for key in ("date", "trade_date", "as_of", "time"):
            raw_date = row.get(key)
            if isinstance(raw_date, str):
                try:
                    dates.add(date.fromisoformat(raw_date[:10]).isoformat())
                    break
                except ValueError:
                    continue
    if not dates and isinstance(value, Mapping):
        for key in ("as_of", "date", "trade_date"):
            raw_date = value.get(key)
            if isinstance(raw_date, str):
                try:
                    dates.add(date.fromisoformat(raw_date[:10]).isoformat())
                    break
                except ValueError:
                    continue
    data_window = None
    if dates:
        ordered = sorted(dates)
        data_window = ordered[0] if len(ordered) == 1 else f"{ordered[0]}至{ordered[-1]}"
    return SourceCoverage(
        returned_rows=len(rows), limit=base.limit, total_rows=total,
        query_window=base.query_window, data_window=data_window,
    )


def normalize_source(call: SourceCall, raw: Any, *, fetched_at: str = "") -> SourceResult:
    """Return only bounded structured rows; unsupported text is never declared successful."""
    base_coverage = _request_coverage(call)
    try:
        value = _parse(raw)
        if call.provider == "tencent" and call.operation == "quote":
            value = _project_tencent_quote(value)
        if isinstance(value, Mapping) and (value.get("error") or value.get("success") is False):
            return SourceResult("", call.provider, call.operation, call.category, "failed",
                                fetched_at=fetched_at, error_code="provider_error", coverage=base_coverage)
        row_limit = 800 if call.provider == "tencent" and call.operation == "kline" else MAX_ROWS
        rows, as_of = _extract_rows(value, row_limit=row_limit)
        if call.provider in {"web", "deepseek_search"}:
            rows = [row for row in rows if isinstance(row.get("url"), str)
                    and urlparse(row["url"]).scheme in {"http", "https"}]
            if not rows:
                return SourceResult("", call.provider, call.operation, call.category, "failed",
                                    fetched_at=fetched_at, error_code="empty_payload", coverage=base_coverage)
        coverage = _response_coverage(call, value, rows, base_coverage)
        payload = tuple(rows)
        if call.provider == "tencent" and call.operation == "kline" and rows:
            close_rows = [row for row in rows
                          if isinstance(row.get("close"), (int, float)) and not isinstance(row.get("close"), bool)]
            summary_rows = []
            for row in (close_rows[:2] + close_rows[-2:]):
                summary_rows.append({key: row[key] for key in ("symbol", "date", "close") if key in row})
            summary = {
                "symbol": call.arguments.get("symbol"),
                "period": call.arguments.get("period"),
                "returned_rows": len(rows),
                "data_window": coverage.data_window,
                "sampled_bars": summary_rows,
            }
            content = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))[:1000]
        else:
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:MAX_SOURCE_CHARS]
        status = "success" if as_of else "partial"
        if (call.provider == "akshare" and call.operation == "a_share_market_snapshot"
                and isinstance(value, Mapping) and value.get("status") in {"partial", "unavailable"}):
            status = str(value["status"])
        return SourceResult("", call.provider, call.operation, call.category, status,
                            content=content, as_of=as_of, fetched_at=fetched_at,
                            payload=payload, coverage=coverage,
                            request_context=market_request_context(call))
    except ValueError as exc:
        raw_text = raw if isinstance(raw, str) else ""
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if len(lines) >= 3 and "|" in lines[0] and re.fullmatch(r"\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?", lines[1]):
            return SourceResult("", call.provider, call.operation, call.category, "partial",
                                content=raw_text[:MAX_SOURCE_CHARS], fetched_at=fetched_at,
                                error_code="structured_reference_only", coverage=base_coverage)
        code = str(exc) if str(exc) in {"provider_error", "empty_payload", "unsupported_payload"} else "unsupported_payload"
        state = "failed" if code in {"provider_error", "empty_payload"} else "unavailable"
        return SourceResult("", call.provider, call.operation, call.category, state,
                            fetched_at=fetched_at, error_code=code, coverage=base_coverage)
