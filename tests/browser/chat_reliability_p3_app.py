"""Local deterministic P3 lifecycle fixture; never accesses providers or external services."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import visual_test_app  # noqa: E402
import chat_reliability_p2_app as p2_fixture  # noqa: E402
import webapp.server as server  # noqa: E402
from financial_report_fetcher.rag.qa import RagQA  # noqa: E402
from webapp.execution_plan import ExecutionPlan  # noqa: E402
from webapp.execution_planner import PlanningResult  # noqa: E402


SOURCE_STARTED = threading.Event()
RELEASE_SOURCE = threading.Event()
SOURCE_CALLS = []


class _NoStore:
    def query(self, *_args, **_kwargs):
        raise AssertionError("P3 fixture must not access a real vector store")


class _AnswerAI:
    def chat_stream(self, **_kwargs):
        yield {"type": "done", "answer": "本地替身回答", "model": "p3-fixture", "usage": {}}


class _Planner:
    def __init__(self, _planner):
        pass

    def plan(self, *_args, **_kwargs):
        return PlanningResult("validated", ExecutionPlan.from_dict({
            "objective": "P3 lifecycle fixture", "source_mode": "external_market",
            "steps": [{"id": "quote", "kind": "market_kline", "required": True},
                      {"id": "indices", "kind": "market_indices", "required": False},
                      {"id": "answer", "kind": "answer", "depends_on": ["quote", "indices"]}],
            "acceptance": ["fixture source completes"],
        }))


def build_app():
    app = visual_test_app.build_app()
    server.stock_index = p2_fixture._FixtureIndex()
    server.ExecutionPlanner = _Planner
    server.rag_qa = RagQA(_NoStore(), _AnswerAI())
    server._build_chat_tool_defs = lambda _cfg: None
    server.RagConfig.load = staticmethod(lambda: type("Cfg", (), {
        "web_search": False, "mcp_max_tool_calls": 2, "mcp_tools": False,
    })())
    SOURCE_STARTED.clear()
    RELEASE_SOURCE.clear()
    SOURCE_CALLS.clear()

    def kline(symbol, *, period, count, adjust):
        SOURCE_CALLS.append({"symbol": symbol, "period": period})
        SOURCE_STARTED.set()
        RELEASE_SOURCE.wait(15)
        return [{"date": "2026-09-24", "close": 10.5}]

    server.tencent_quote.kline = kline

    @app.get("/_fixture/p3-state")
    def state():
        saved_statuses = []
        for session in server.chat_store.list_sessions():
            detail = server.chat_store.get_session(session["id"]) or {}
            saved_statuses.extend(
                str(message.get("run", {}).get("status"))
                for message in detail.get("messages", [])
                if isinstance(message, dict) and isinstance(message.get("run"), dict)
            )
        return {"calls": list(SOURCE_CALLS), "started": SOURCE_STARTED.is_set(),
                "released": RELEASE_SOURCE.is_set(), "saved_statuses": saved_statuses}

    @app.post("/_fixture/p3-release")
    def release():
        RELEASE_SOURCE.set()
        return {"released": True}

    return app


def main():
    import uvicorn
    uvicorn.run(build_app(), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
