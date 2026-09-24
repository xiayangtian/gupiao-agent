"""Isolated real FastAPI harness for run-scoped source evidence browser checks."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import visual_test_app  # noqa: E402
import webapp.server as server  # noqa: E402
from financial_report_fetcher.rag.qa import RagQA  # noqa: E402
from webapp.execution_plan import ExecutionPlan  # noqa: E402
from webapp.execution_planner import PlanningResult  # noqa: E402


class _NoStore:
    def query(self, *_args, **_kwargs):
        raise AssertionError("validated source plan must not retrieve again")


class _AnswerAI:
    def chat_stream(self, **_kwargs):
        yield {"type": "done", "answer": "行情来源已取得，可查看数据时间和获取时间。",
               "model": "source-runtime-fixture", "usage": {}}


CURRENT_SCENARIO = "success"


class _FakeWeb:
    def __init__(self, *, timeout):
        self.timeout = timeout
        self.available = True

    def search(self, _query):
        raise AssertionError("web search must go through the local fixture executor")


class _FixedPlanner:
    def __init__(self, _json_planner):
        pass

    def plan(self, question, *_args):
        global CURRENT_SCENARIO
        CURRENT_SCENARIO = ("all_failed" if "全部失败" in question else
                            "web_failed" if "网页失败" in question else "success")
        plan = ExecutionPlan.from_dict({
            "objective": "来源展示验收", "source_mode": "market_recap",
            "steps": [
                {"id": "indices", "kind": "market_indices", "required": True},
                {"id": "web", "kind": "web_search", "required": False,
                 "depends_on": ["indices"]},
                {"id": "answer", "kind": "answer", "depends_on": ["indices", "web"]},
            ], "acceptance": ["四个指数及网页来源状态可见"],
        })
        return PlanningResult("validated", plan)


def build_app():
    app = visual_test_app.build_app()
    server._mcp_tool_defs = lambda: None
    server._mcp_tool_defs_cache = None
    server.ExecutionPlanner = _FixedPlanner
    cfg = server.RagConfig.load()
    cfg.web_search = True
    server.RagConfig.load = staticmethod(lambda: cfg)
    server.TavilyWebSearch = _FakeWeb
    def fixture_executor(_cfg, *, retry=True):
        def execute(name, arguments):
            if name != "web_search":
                raise AssertionError(f"unexpected tool: {name}")
            if CURRENT_SCENARIO == "web_failed":
                return {"error": "fixture web failure"}
            return {"as_of": "2026-09-24T10:00:00+08:00", "results": [
                {"url": "https://fixture.invalid/market", "title": "Fixture market source",
                 "published_date": "2026-09-24", "content": "Fixture source result"}
            ]}
        return execute
    server._build_chat_tool_executor = fixture_executor
    calls = []

    def fake_kline(symbol, *, period, count, adjust):
        calls.append((symbol, period, count, adjust))
        if CURRENT_SCENARIO == "all_failed":
            return []
        return [{"date": "2026-09-24", "close": 10.0}]

    server.tencent_quote.kline = fake_kline
    server.rag_qa = RagQA(_NoStore(), _AnswerAI())
    server.SOURCE_RUNTIME_FIXTURE_CALLS = calls
    return app


def main() -> None:
    import uvicorn
    uvicorn.run(build_app(), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
