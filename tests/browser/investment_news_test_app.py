"""Launch the real FastAPI app with local fixtures only for news-page QA."""

from __future__ import annotations

import atexit
import json
import os
import sys
import tempfile
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TEMP = tempfile.TemporaryDirectory(prefix="investment-news-browser-")
_TEMP_PATH = Path(_TEMP.name)
os.environ["INVESTMENT_NEWS_DB_PATH"] = str(_TEMP_PATH / "news.sqlite3")
os.environ["INVESTMENT_NEWS_SCHEDULER_ENABLED"] = "0"
_LOG_PATH = Path(os.environ.get("INVESTMENT_NEWS_TEST_LOG", _TEMP_PATH / "calls.json"))
atexit.register(_TEMP.cleanup)

import webapp.server as server  # noqa: E402
from webapp.investment_news import InvestmentNewsService  # noqa: E402
from webapp.investment_news_store import InvestmentNewsStore, NewsItem  # noqa: E402


_CALLS: list[str] = []
_COLLECTION_COUNTER = 0


def _write_calls() -> None:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LOG_PATH.write_text(json.dumps(_CALLS, ensure_ascii=False), encoding="utf-8")


class FakeStockIndex:
    is_ready = True

    def start(self):
        return None

    def wait_ready(self, timeout=5.0):
        return True

    def is_valid_code(self, code):
        return len(code) == 6 and code.isdigit()

    def company_name(self, code):
        return "测试股票" + code if self.is_valid_code(code) else None

    def search(self, query, limit=10):
        candidates = [{"code": "600900", "name": "长江电力"}, {"code": "601398", "name": "工商银行"}]
        return [item for item in candidates if query in item["code"] or query in item["name"]][:limit]


class FakeNewsSource:
    def fetch_market(self):
        global _COLLECTION_COUNTER
        _COLLECTION_COUNTER += 1
        _CALLS.append("collect_market")
        _write_calls()
        now = datetime.now(timezone.utc).isoformat()
        return [NewsItem(
            id=f"fixture-collected-market-{_COLLECTION_COUNTER}", title=f"测试采集的新市场资讯 {_COLLECTION_COUNTER}",
            url="https://example.test/collected-market", snippet="测试来源片段",
            source_id="eastmoney_market", source_name="测试市场来源",
            published_at=now, collected_at=now, kind="market", symbols=(),
        )]

    def fetch_stock(self, code, name):
        _CALLS.append("collect_stock:" + code)
        _write_calls()
        now = datetime.now(timezone.utc).isoformat()
        return [NewsItem(
            id="fixture-collected-stock-" + code, title=name + "测试资讯",
            url="https://example.test/stock/" + code, snippet="测试个股片段",
            source_id="eastmoney_stock", source_name="测试个股来源",
            published_at=now, collected_at=now, kind="watchlist", symbols=(code,),
        )]


class FakeAIClient:
    def chat(self, messages, **kwargs):
        _CALLS.append("digest")
        _write_calls()
        content = json.loads(messages[-1]["content"])
        ids = content.get("allowed_item_ids") or [item["id"] for item in content.get("items", [])]
        response = {
            "market_themes": [{"text": "仅由本地 fixture 生成的综述。", "item_ids": ids[:1]}] if ids else [],
            "policy_regulatory": [], "watchlist": [], "risks": [],
        }
        return {"content": json.dumps(response, ensure_ascii=False), "model": "browser-fixture", "usage": {}}


def _service_factory():
    store = InvestmentNewsStore(str(_TEMP_PATH / "news.sqlite3"))
    source = FakeNewsSource()
    service = InvestmentNewsService(
        store=store, source=source, ai_client=FakeAIClient(),
        stock_name_lookup=lambda code: "测试股票" + code,
    )
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    for index in range(25):
        timestamp = datetime.now(timezone.utc).isoformat()
        snippet = "<img src=x onerror=alert(1)>" if index == 0 else "市场资讯 fixture 片段 " + str(index)
        store.upsert_items([NewsItem(
            id=f"fixture-market-{index}", title=f"测试市场资讯 {index}",
            url=f"https://example.test/market/{index}", snippet=snippet,
            source_id="eastmoney_market", source_name="测试市场来源",
            published_at=timestamp, collected_at=timestamp, kind="market", symbols=(),
        )])
    store.add_watchlist("600900", "长江电力")
    timestamp = datetime.now(timezone.utc).isoformat()
    store.upsert_items([NewsItem(
        id="fixture-stock-1", title="长江电力测试资讯",
        url="https://example.test/stock/seed", snippet="自选股 fixture 片段",
        source_id="eastmoney_stock", source_name="测试个股来源",
        published_at=timestamp, collected_at=timestamp, kind="watchlist", symbols=("600900",),
    )])
    shanghai_tz = ZoneInfo("Asia/Shanghai")
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    yesterday_published = datetime.combine(date.fromisoformat(yesterday), datetime_time(10, 0), tzinfo=shanghai_tz).isoformat()
    store.upsert_items([NewsItem(
        id="fixture-market-yesterday", title="昨日市场资讯 fixture",
        url="https://example.test/market/yesterday", snippet="昨日市场快讯 fixture",
        source_id="eastmoney_market", source_name="测试市场来源",
        published_at=yesterday_published, collected_at=timestamp, kind="market", symbols=(),
    )])
    for days_ago, item_id, title in (
        (5, "fixture-stock-7day", "长江电力七天内资讯"),
        (20, "fixture-stock-30day", "长江电力三十天内资讯"),
    ):
        published_day = date.fromisoformat(today) - timedelta(days=days_ago)
        published_at = datetime.combine(published_day, datetime_time(10, 0), tzinfo=shanghai_tz).isoformat()
        store.upsert_items([NewsItem(
            id=item_id, title=title, url="https://example.test/stock/" + item_id,
            snippet="历史自选股 fixture 片段", source_id="eastmoney_stock",
            source_name="测试个股来源", published_at=published_at,
            collected_at=timestamp, kind="watchlist", symbols=("600900",),
        )])
    digest_payload = {
        "market_themes": [{
            "text": "本地测试综述，引用链接应可打开且不执行 HTML。",
            "item_ids": ["fixture-market-0"],
            "citation_count": 1,
            "citations": [{
                "item_id": "fixture-market-0", "title": "测试市场资讯 0",
                "source_name": "测试市场来源", "url": "https://example.test/market/0",
            }],
        }],
        "policy_regulatory": [], "watchlist": [], "risks": [],
        "input_count": 26, "source_count": 26, "data_through": timestamp,
    }
    store.save_digest(today, digest_payload, store.daily_input_hash(today), "browser-fixture",
                      input_count=26, source_count=26, data_through=timestamp)
    historical_payload = {
        "market_themes": [{"text": "历史日期综述 fixture", "item_ids": [], "citation_count": 0, "citations": []}],
        "policy_regulatory": [], "watchlist": [], "risks": [],
        "input_count": 0, "source_count": 0,
        "data_through": datetime.combine(date.fromisoformat(yesterday), datetime_time(12, 0), tzinfo=shanghai_tz).isoformat(),
    }
    store.save_digest(yesterday, historical_payload, store.daily_input_hash(yesterday), "browser-fixture",
                      input_count=0, source_count=0, data_through=historical_payload["data_through"])
    return service


server.stock_index = FakeStockIndex()
server.investment_news_service_factory = _service_factory


@server.app.post("/__test__/scheduled-collection")
def _run_scheduled_collection_fixture():
    service = server._investment_news_or_503()
    queued = service.submit_collection("scheduled")
    completed = service.wait_for_run(queued["run_id"], timeout=30)
    return {
        "run": completed or {"run_id": queued["run_id"], "status": "running"},
        "title": f"测试采集的新市场资讯 {_COLLECTION_COUNTER}",
    }


@server.app.post("/__test__/scheduled-digest")
def _run_scheduled_digest_fixture():
    service = server._investment_news_or_503()
    queued = service.submit_digest("scheduled", force=True)
    completed = service.wait_for_run(queued["run_id"], timeout=30)
    return completed or {"run_id": queued["run_id"], "status": "running"}


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1])
    uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")
