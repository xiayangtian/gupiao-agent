"""Provider permission and conservative payload normalization for source execution."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from webapp.source_runtime import SourceCall, SourceCoverage, SourceResult

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
        if call.provider == "tencent":
            return self.tencent_enabled
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


def _extract_rows(value: Any) -> tuple[list[dict[str, Any]], str]:
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
    for row in rows[:MAX_ROWS]:
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
    limit = arguments.get("limit")
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
    if isinstance(value, Mapping):
        for key in ("as_of", "date", "trade_date"):
            raw_date = value.get(key)
            if isinstance(raw_date, str):
                try:
                    dates.add(date.fromisoformat(raw_date[:10]).isoformat())
                    break
                except ValueError:
                    continue
    for row in rows:
        for key in ("date", "trade_date", "as_of", "time"):
            raw_date = row.get(key)
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
        if isinstance(value, Mapping) and (value.get("error") or value.get("success") is False):
            return SourceResult("", call.provider, call.operation, call.category, "failed",
                                fetched_at=fetched_at, error_code="provider_error", coverage=base_coverage)
        rows, as_of = _extract_rows(value)
        if call.provider == "web":
            rows = [row for row in rows if isinstance(row.get("url"), str)
                    and urlparse(row["url"]).scheme in {"http", "https"}]
            if not rows:
                return SourceResult("", call.provider, call.operation, call.category, "failed",
                                    fetched_at=fetched_at, error_code="empty_payload", coverage=base_coverage)
        coverage = _response_coverage(call, value, rows, base_coverage)
        payload = tuple(rows)
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:MAX_SOURCE_CHARS]
        status = "success" if as_of else "partial"
        return SourceResult("", call.provider, call.operation, call.category, status,
                            content=content, as_of=as_of, fetched_at=fetched_at,
                            payload=payload, coverage=coverage)
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
