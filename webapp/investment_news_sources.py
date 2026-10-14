"""Eastmoney-backed news source adapter using the project's existing AKShare dependency."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from datetime import datetime, timezone
from types import FunctionType
from html.parser import HTMLParser
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from webapp.investment_news_store import NewsItem


MAX_SNIPPET_CHARS = 1200
SOURCE_REQUEST_TIMEOUT = (5, 20)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class SourceSchemaError(ValueError):
    """Raised when a provider response no longer matches the expected field contract."""


class _TextOnlyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _clean_text(value: Any, limit: int = MAX_SNIPPET_CHARS) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value)
    parser = _TextOnlyParser()
    try:
        parser.feed(text)
        parser.close()
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]*>", " ", text)
    text = "".join(
        char for char in text
        if char in "\n\t " or not unicodedata.category(char).startswith("C")
    )
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _published_at(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if str(value).strip().lower() in {"nan", "nat", "none", "null", "<na>"}:
        return None
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text or text.lower() in {"nan", "nat", "none", "null"}:
            return None
        text = text.replace("/", "-")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(timezone.utc).isoformat()


def _validated_url(value: Any) -> str:
    url = _clean_text(value, limit=4096)
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("news URL must be an absolute HTTP(S) URL")
    return url


class _RequestsTimeoutProxy:
    def __init__(self, requests_module: Any) -> None:
        self._requests_module = requests_module

    def get(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", SOURCE_REQUEST_TIMEOUT)
        return self._requests_module.get(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._requests_module, name)


def _with_request_timeout(function: Callable[..., Any]) -> Callable[..., Any]:
    """Clone an AKShare function's globals so its requests.get has a bounded timeout."""
    namespace = function.__globals__.copy()
    requests_module = namespace.get("requests")
    if requests_module is None or not hasattr(requests_module, "get"):
        return function
    namespace["requests"] = _RequestsTimeoutProxy(requests_module)
    wrapped = FunctionType(
        function.__code__, namespace, function.__name__, function.__defaults__, function.__closure__
    )
    wrapped.__kwdefaults__ = function.__kwdefaults__
    return wrapped


def _stable_id(source_id: str, url: str) -> str:
    return hashlib.sha256(f"{source_id}\0{url}".encode("utf-8")).hexdigest()[:32]


def _records(frame: Any, required: tuple[str, ...]) -> list[dict[str, Any]]:
    columns = set(getattr(frame, "columns", ()))
    missing = [column for column in required if column not in columns]
    if missing:
        raise SourceSchemaError("provider response missing columns: " + ", ".join(missing))
    if hasattr(frame, "to_dict"):
        return list(frame.to_dict(orient="records"))
    raise SourceSchemaError("provider response must be a tabular DataFrame")


class EastmoneyNewsSource:
    """Normalize public Eastmoney feeds. Fetch callables can be injected in tests."""

    def __init__(
        self,
        global_fetcher: Callable[[], Any] | None = None,
        stock_fetcher: Callable[[str], Any] | None = None,
    ) -> None:
        self._global_fetcher = global_fetcher
        self._stock_fetcher = stock_fetcher
        self._akshare: Any | None = None

    def _default_global_fetcher(self) -> Any:
        if self._akshare is None:
            import akshare as ak
            self._akshare = ak
        return _with_request_timeout(self._akshare.stock_info_global_em)()

    def _default_stock_fetcher(self, code: str) -> Any:
        if self._akshare is None:
            import akshare as ak
            self._akshare = ak
        return _with_request_timeout(self._akshare.stock_news_em)(symbol=code)

    def fetch_market(self) -> list[NewsItem]:
        frame = (self._global_fetcher or self._default_global_fetcher)()
        rows = _records(frame, ("标题", "摘要", "发布时间", "链接"))
        items = []
        for row in rows:
            title = _clean_text(row.get("标题"), limit=500)
            if not title:
                continue
            url = _validated_url(row.get("链接"))
            source_id = "eastmoney_market"
            items.append(NewsItem(
                id=_stable_id(source_id, url),
                title=title,
                url=url,
                snippet=_clean_text(row.get("摘要")),
                source_id=source_id,
                source_name="东方财富市场快讯",
                published_at=_published_at(row.get("发布时间")),
                collected_at=datetime.now(timezone.utc).isoformat(),
                kind="market",
                symbols=(),
            ))
        return items

    def fetch_stock(self, code: str, name: str) -> list[NewsItem]:
        code = str(code).strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("stock code must be a six-digit A-share code")
        frame = (self._stock_fetcher or self._default_stock_fetcher)(code)
        rows = _records(frame, ("新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"))
        items = []
        for row in rows:
            title = _clean_text(row.get("新闻标题"), limit=500)
            if not title:
                continue
            url = _validated_url(row.get("新闻链接"))
            source_id = "eastmoney_stock"
            source_name = _clean_text(row.get("文章来源"), limit=120) or "东方财富自选股新闻"
            items.append(NewsItem(
                id=_stable_id(source_id, url),
                title=title,
                url=url,
                snippet=_clean_text(row.get("新闻内容")),
                source_id=source_id,
                source_name=source_name,
                published_at=_published_at(row.get("发布时间")),
                collected_at=datetime.now(timezone.utc).isoformat(),
                kind="watchlist",
                symbols=(code,),
            ))
        return items

    def fetch_for_watchlist(
        self, entries: Iterable[dict[str, str]]
    ) -> tuple[list[NewsItem], list[dict[str, Any]]]:
        items: list[NewsItem] = []
        results: list[dict[str, Any]] = []
        for entry in entries:
            code = str(entry.get("code", ""))
            name = str(entry.get("name", ""))
            try:
                stock_items = self.fetch_stock(code, name)
            except Exception as exc:
                results.append({
                    "code": code,
                    "name": name,
                    "status": "failed",
                    "item_count": 0,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                })
                continue
            items.extend(stock_items)
            results.append({
                "code": code,
                "name": name,
                "status": "succeeded",
                "item_count": len(stock_items),
            })
        return items, results
