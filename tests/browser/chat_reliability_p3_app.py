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
from webapp.chat_runs import ChatRunRegistry  # noqa: E402


SOURCE_STARTED = threading.Event()
RELEASE_SOURCE = threading.Event()
SOURCE_CALLS = []
RESEARCH_NORMALIZE_STARTED = threading.Event()
RELEASE_RESEARCH_NORMALIZE = threading.Event()
RESEARCH_RETRIEVAL_CALLS = []


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


def _research_state():
    session_id = ""
    research_id = ""
    run = None
    for session in server.chat_store.list_sessions():
        detail = server.chat_store.get_session(session["id"]) or {}
        for message in reversed(detail.get("messages", [])):
            answer = message.get("run") if isinstance(message, dict) else None
            candidate = answer.get("research_run_id") if isinstance(answer, dict) else None
            if candidate:
                run = server.chat_store.get_research_run(session["id"], candidate)
                if run is not None:
                    return run
    return run


def _research_state_payload():
    run = _research_state()
    answer = {}
    for session in server.chat_store.list_sessions():
        detail = server.chat_store.get_session(session["id"]) or {}
        for message in reversed(detail.get("messages", [])):
            candidate = message.get("run") if isinstance(message, dict) else None
            if isinstance(candidate, dict) and candidate.get("research_run_id"):
                answer = {key: candidate.get(key) for key in ("id", "status", "research_run_id", "research_summary")}
                break
        if answer:
            break
    return {
        "normalize_started": RESEARCH_NORMALIZE_STARTED.is_set(),
        "retrieval_calls": len(RESEARCH_RETRIEVAL_CALLS),
        "research_status": run.status if run else "",
        "completed_step_ids": [item.step_id for item in run.step_runs if item.status == "completed"] if run else [],
        "step_ids": [item.step_id for item in run.step_runs] if run else [],
        "step_errors": [item.error for item in run.step_runs if item.error] if run else [],
        "answer_recovery": answer,
    }


def _build_research_resume_app():
    app = visual_test_app.build_app()
    server.ExecutionPlanner = _Planner
    RESEARCH_NORMALIZE_STARTED.clear()
    RELEASE_RESEARCH_NORMALIZE.clear()
    RESEARCH_RETRIEVAL_CALLS.clear()
    server.chat_run_registry = ChatRunRegistry(max_workers=4)
    original_handlers = server._research_step_handlers
    original_qa = server.rag_qa
    original_answer_stream = original_qa.answer_stream

    def counted_answer_stream(question, history=None, filters=None, tools=None,
                              priority_report_id=None, scope=None, run_id=None, tool_policy=None):
        RESEARCH_RETRIEVAL_CALLS.append("retrieve")
        yield from original_answer_stream(
            question=question, history=history, filters=filters, tools=tools,
            priority_report_id=priority_report_id, scope=scope, run_id=run_id,
            tool_policy=tool_policy,
        )

    original_qa.answer_stream = counted_answer_stream
    gate_lock = threading.Lock()
    gate_used = False

    def gated_handlers(*args, **kwargs):
        nonlocal gate_used
        handlers = original_handlers(*args, **kwargs)
        normalize = handlers["normalize"]

        def gated_normalize(*handler_args, **handler_kwargs):
            nonlocal gate_used
            with gate_lock:
                should_gate = not gate_used
                gate_used = True
            if should_gate:
                RESEARCH_NORMALIZE_STARTED.set()
                RELEASE_RESEARCH_NORMALIZE.wait(15)
            return normalize(*handler_args, **handler_kwargs)

        handlers["normalize"] = gated_normalize
        return handlers

    server._research_step_handlers = gated_handlers

    @app.get("/_fixture/p3-state")
    def research_state():
        return _research_state_payload()

    @app.post("/_fixture/p3-release")
    def release_research():
        RELEASE_RESEARCH_NORMALIZE.set()
        return {"released": True}

    return app


def build_app(scenario="lifecycle"):
    if scenario == "research-resume":
        return _build_research_resume_app()
    app = visual_test_app.build_app()
    server.stock_index = p2_fixture._FixtureIndex()
    server.chat_run_registry = ChatRunRegistry(max_workers=4)
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
                "released": RELEASE_SOURCE.is_set(), "saved_statuses": saved_statuses,
                "active_runs": server.chat_run_registry.active_count}

    @app.post("/_fixture/p3-release")
    def release():
        RELEASE_SOURCE.set()
        return {"released": True}

    return app


def main():
    import uvicorn
    scenario = sys.argv[2] if len(sys.argv) > 2 else "lifecycle"
    uvicorn.run(build_app(scenario), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
