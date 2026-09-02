"""网页搜索工具：仅通过环境变量提供 Tavily 密钥，不持久化敏感信息。"""

import json
import os
from typing import Any, Optional

import requests


class TavilyWebSearch:
    """Tavily Search 的最小适配层，向模型返回可引用的受控结果。"""

    endpoint = "https://api.tavily.com/search"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        session: Optional[Any] = None,
        timeout: int = 15,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("TAVILY_API_KEY", "")).strip()
        self.session = session or requests.Session()
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str, max_results: int = 5, search_depth: str = "basic") -> str:
        query = str(query or "").strip()
        if not self.available:
            return "工具调用失败：网页搜索未配置 TAVILY_API_KEY"
        if not query:
            return "工具调用失败：网页搜索关键词不能为空"
        try:
            max_results = max(1, min(int(max_results), 5))
        except (TypeError, ValueError):
            max_results = 5
        if search_depth not in {"basic", "advanced"}:
            search_depth = "basic"

        payload = {
            "query": query,
            "search_depth": search_depth,
            "max_results": max_results,
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            response = self.session.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            return f"工具调用失败：网页搜索请求失败：{exc}"

        rows = body.get("results", []) if isinstance(body, dict) else []
        results = []
        for row in rows[:max_results]:
            if not isinstance(row, dict) or not row.get("url"):
                continue
            results.append({
                "title": str(row.get("title") or row["url"]),
                "url": str(row["url"]),
                "content": str(row.get("content") or "")[:1200],
                "published_date": str(row.get("published_date") or ""),
            })
        return json.dumps({"source": "web_search", "query": query, "results": results}, ensure_ascii=False)
