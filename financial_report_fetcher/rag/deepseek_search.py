"""DeepSeek Search API adapter with bounded, citation-ready web results."""

from __future__ import annotations

import json
import os
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from financial_report_fetcher.rag.web_search import normalize_search_query


class DeepSeekWebSearch:
    """Call DeepSeek's standalone Search API without exposing raw provider errors."""

    endpoint = "https://api.deepseek.com/v1/tools/search"
    max_results = 5
    max_query_chars = 128
    max_title_chars = 300
    max_content_chars = 1_200
    max_snippet_chars = 400

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        session: Optional[Any] = None,
        timeout: int = 15,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("DEEPSEEK_API_KEY", "")).strip()
        self.session = session
        self.timeout = max(1, int(timeout))

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def _clean_results(cls, rows: Any, count: int) -> list[dict[str, str]]:
        if not isinstance(rows, list):
            raise ValueError("invalid_results")
        results: list[dict[str, str]] = []
        for row in rows[:count]:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            parsed = urlparse(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username
                    or parsed.password or len(url) > 2_000):
                continue
            title = str(row.get("title") or url).strip()[:cls.max_title_chars]
            content = str(row.get("content") or row.get("summary") or "").strip()[:cls.max_content_chars]
            snippet = str(row.get("snippet") or row.get("summary") or content or title).strip()
            published = str(row.get("date") or row.get("published_date") or "").strip()[:80]
            results.append({
                "title": title or url,
                "url": url[:2_000],
                "content": content,
                "snippet": snippet[:cls.max_snippet_chars],
                "published_date": published,
            })
        return results

    @staticmethod
    def _failure(reason: str, query: str = "", *, billable_success: bool = False) -> str:
        return json.dumps({
            "source": "web_search", "provider": "deepseek_search",
            "query": query, "results": [], "error": reason,
            "billable_success": billable_success,
        }, ensure_ascii=False)

    def search(self, query: str, *, count: int = 5, need_content: bool = True) -> str:
        normalized = normalize_search_query(query)[:self.max_query_chars]
        if not self.available:
            return self._failure("missing_api_key", normalized)
        if not normalized:
            return self._failure("empty_query")
        try:
            bounded_count = max(1, min(int(count), self.max_results))
        except (TypeError, ValueError):
            bounded_count = self.max_results
        payload = {"query": normalized, "count": bounded_count, "need_content": bool(need_content)}
        billable_success = False
        try:
            session = self.session or requests.Session()
            response = session.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                return self._failure("invalid_response", normalized)
            raw_results = body.get("results")
            billable_success = isinstance(raw_results, list) and bool(raw_results)
            results = self._clean_results(raw_results, bounded_count)
        except ValueError:
            return self._failure("invalid_response", normalized, billable_success=billable_success)
        except Exception:
            # Do not include exception text: HTTP libraries may embed request headers or response data.
            return self._failure("provider_error", normalized)
        if not results:
            reason = "invalid_results" if billable_success else "empty_results"
            return self._failure(reason, normalized, billable_success=billable_success)
        return json.dumps({
            "source": "web_search", "provider": "deepseek_search",
            "query": normalized, "results": results, "billable_success": True,
        }, ensure_ascii=False)
