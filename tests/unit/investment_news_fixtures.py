from datetime import datetime, timezone
import json
import re

from webapp.investment_news_store import InvestmentNewsStore, NewsItem


def make_item(index, *, day="2026-10-14", url=None, title=None, symbol=None, collected_at=None):
    published = datetime.fromisoformat(f"{day}T10:00:00+08:00").astimezone(timezone.utc).isoformat()
    return NewsItem(
        id=f"item-{index}",
        title=title or f"测试新闻{index}",
        url=url or f"https://news.example/{index}",
        snippet="来源片段",
        source_id="eastmoney_test",
        source_name="测试来源",
        published_at=published,
        collected_at=collected_at or published,
        kind="watchlist" if symbol else "market",
        symbols=(symbol,) if symbol else (),
    )


class FakeNewsSource:
    def __init__(self, market_items=(), stock_items_by_code=None, failures=None):
        self.market_items = list(market_items)
        self.stock_items_by_code = dict(stock_items_by_code or {})
        self.failures = dict(failures or {})
        self.calls = []

    def fetch_market(self):
        self.calls.append(("market", None))
        return list(self.market_items)

    def fetch_stock(self, code, name):
        self.calls.append(("stock", code))
        if code in self.failures:
            raise self.failures[code]
        return list(self.stock_items_by_code.get(code, []))


class RecordingAIClient:
    def __init__(self):
        self.calls = []
        self.seen_item_ids = set()

    def chat(self, messages, **kwargs):
        ids = sorted(set(re.findall(r"item-[0-9]+", json.dumps(messages, ensure_ascii=False))))
        self.calls.append({"messages": messages, "kwargs": kwargs})
        self.seen_item_ids.update(ids)
        content = json.dumps({
            "market_themes": [{"text": "测试综述", "item_ids": ids}] if ids else [],
            "policy_regulatory": [],
            "watchlist": [],
            "risks": [],
        }, ensure_ascii=False)
        return {"content": content, "model": "test-model", "usage": {}}


def make_service(tmp_path, source=None, ai_client=None):
    from webapp.investment_news import InvestmentNewsService

    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    service = InvestmentNewsService(
        store=store,
        source=source or FakeNewsSource(),
        ai_client=ai_client or RecordingAIClient(),
        stock_name_lookup=lambda code: f"测试股票{code}",
    )
    return service, store
