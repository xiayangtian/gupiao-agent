"""Execute offline evaluation cases through the real chat ASGI/SSE route.

The runner drives the *actual* ``POST /api/chat/stream`` orchestration with frozen
Scope, clock, model, retrieval and market providers. Every provider entry point
counts its physical attempts, and outbound TCP is denied for the whole run, so a
missing injection fails the case instead of silently reaching a real service.
"""
from __future__ import annotations

import datetime as dt
import json
import socket
import time
import urllib.request
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
from unittest.mock import patch

import httpx
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webapp.chat_models import CompanyRef, Scope
from webapp.chat_runtime_eval_cases import RuntimeCase

# Frozen evaluation clock: the baseline must not change with the wall clock.
FROZEN_NOW = dt.datetime(2026, 10, 8, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
CORPUS_PATH = Path(__file__).resolve().parents[1] / "tests/fixtures/chat_runtime_eval_corpus.json"
_COMPANY_NAMES = {"601288": "农业银行", "600519": "贵州茅台", "600900": "长江电力"}

# Plan step kinds must match the server's frozen intent contract in webapp/execution_plan.py.
_INTENT_PLANS: Mapping[str, Mapping[str, Any]] = {
    "report_fact": {"source_mode": "local_evidence", "steps": [("retrieve", "retrieve", []), ("answer", "answer", ["retrieve"])]},
    "financial_trend": {"source_mode": "local_evidence", "steps": [("retrieve", "retrieve", []), ("answer", "answer", ["retrieve"])]},
    "company_trend": {"source_mode": "local_evidence", "steps": [("retrieve", "retrieve", []), ("answer", "answer", ["retrieve"])]},
    "market_quote": {"source_mode": "external_market", "steps": [("quote", "market_quote", []), ("answer", "answer", ["quote"])]},
    "market_trend": {"source_mode": "external_market", "steps": [("kline", "market_kline", []), ("answer", "answer", ["kline"])]},
    "market_recap": {"source_mode": "market_recap", "steps": [("overview", "market_overview", []), ("answer", "answer", ["overview"])]},
    "general_knowledge": {"source_mode": "general_knowledge", "steps": [("answer", "answer", [])]},
    "research_task": {"source_mode": "local_evidence", "steps": [("retrieve", "retrieve", []), ("answer", "answer", ["retrieve"])]},
}

_PLANS: Mapping[str, Mapping[str, Any]] = {
    "knowledge": {"source_mode": "general_knowledge", "steps": [("answer", "answer", [])]},
    "report": {"source_mode": "local_evidence", "steps": [("retrieve", "retrieve", []), ("answer", "answer", ["retrieve"])]},
    "followup": {"source_mode": "local_evidence", "steps": [("retrieve", "retrieve", []), ("answer", "answer", ["retrieve"])]},
    "failure": {"source_mode": "local_evidence", "steps": [("retrieve", "retrieve", []), ("answer", "answer", ["retrieve"])]},
    "recovery": {"source_mode": "local_evidence", "steps": [("retrieve", "retrieve", []), ("answer", "answer", ["retrieve"])]},
    "research": {"source_mode": "local_evidence", "steps": [("retrieve", "retrieve", []), ("answer", "answer", ["retrieve"])]},
    "market": {"source_mode": "external_market", "steps": [("quote", "market_quote", []), ("answer", "answer", ["quote"])]},
    "recap": {"source_mode": "market_recap", "steps": [("overview", "market_overview", []), ("answer", "answer", ["overview"])]},
}


@dataclass
class ProviderFixture:
    """Per-case physical-attempt counter; adapters call ``record_attempt`` at source entry."""

    version: str
    case_id: str = ""
    calls: Counter[str] = field(default_factory=Counter)
    limits: Mapping[str, int] = field(default_factory=dict)

    def record_attempt(self, source: str) -> None:
        self.calls[source] += 1
        limit = self.limits.get(source)
        if limit is not None and self.calls[source] > limit:
            raise AssertionError(f"offline source call limit exceeded: {source}")

    def assert_all_calls_frozen(self) -> None:
        unknown = set(self.calls) - set(self.limits)
        exceeded = {name: count for name, count in self.calls.items()
                    if name in self.limits and count > self.limits[name]}
        if unknown or exceeded:
            raise AssertionError(f"unexpected provider attempts: unknown={sorted(unknown)} exceeded={exceeded}")


@dataclass(frozen=True)
class RuntimeObservation:
    events: tuple[str, ...]
    persisted_status: str
    answer: str
    session_id: str
    run_id: str
    call_attempts: Mapping[str, int]
    first_frame_seconds: float | None
    first_content_seconds: float | None
    total_seconds: float | None
    usage: Mapping[str, Any] | None
    terminal: Mapping[str, Any]


def _deny_network(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("offline chat evaluation attempted outbound network access")


def _network_guard(stack: ExitStack) -> None:
    """Block real TCP/HTTP transports; the ASGI TestClient transport stays in-process."""
    stack.enter_context(patch.object(socket, "create_connection", _deny_network))
    stack.enter_context(patch.object(socket.socket, "connect", _deny_network))
    stack.enter_context(patch.object(urllib.request, "urlopen", _deny_network))
    stack.enter_context(patch.object(requests.Session, "request", _deny_network))
    stack.enter_context(patch.object(httpx.HTTPTransport, "handle_request", _deny_network))
    stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _deny_network))


def _parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in text.replace("\r\n", "\n").split("\n\n"):
        if not frame.strip():
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event_name = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                data_lines.append(line.partition(":")[2].lstrip())
        if not data_lines:
            continue
        try:
            data = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid SSE payload for {event_name}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"SSE payload for {event_name} must be an object")
        events.append((event_name, data))
    return events


def load_corpus(path: Path | None = None) -> tuple[dict[str, Any], ...]:
    """Load the versioned, synthetic offline evidence corpus."""
    raw = json.loads((path or CORPUS_PATH).read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1 or not isinstance(raw.get("documents"), list):
        raise ValueError("corpus fixture must be schema_version 1 with a documents array")
    documents = []
    for document in raw["documents"]:
        if not isinstance(document, dict) or not document.get("report_id") or not document.get("text"):
            raise ValueError("corpus document requires report_id and text")
        entry = dict(document)
        entry.setdefault("id", str(entry.get("evidence_id") or entry["report_id"]))
        entry.setdefault("source", "pdf")
        entry.setdefault("section", "经营情况讨论")
        documents.append(entry)
    return tuple(documents)


class _FixtureStore:
    """Deterministic in-memory vector-store stand-in that still honours Scope where-filters."""

    def __init__(self, documents: Iterable[Mapping[str, Any]], fixture: ProviderFixture, *, unavailable: bool) -> None:
        self._documents = tuple(dict(document) for document in documents)
        self._fixture = fixture
        self._unavailable = unavailable

    def query(self, _question: str, top_k: int = 8, where: Any = None) -> list[dict[str, Any]]:
        self._fixture.record_attempt("retrieval")
        if self._unavailable:
            raise RuntimeError("fixture retrieval unavailable")
        allowed: set[str] | None = None
        if isinstance(where, Mapping) and isinstance(where.get("report_id"), Mapping):
            allowed = {str(item) for item in where["report_id"].get("$in", ())}
        hits = [dict(document) for document in self._documents
                if allowed is None or document["report_id"] in allowed]
        return hits[:max(1, top_k)]

    def list_report_ids(self) -> list[str]:
        return sorted({str(document["report_id"]) for document in self._documents})


class _FixtureAI:
    """Fixed model stand-in returning a case plan and a fixed answer; counts model attempts."""

    api_key = "offline-evaluation-key"

    def __init__(self, case: RuntimeCase, fixture: ProviderFixture) -> None:
        self._case = case
        self._fixture = fixture
        answers = case.provider_fixture.get("model") or ()
        first = answers[0] if answers else {}
        self._answer = str(first.get("answer") or "固定离线替身回答。") if isinstance(first, Mapping) else "固定离线替身回答。"
        self._model = str(first.get("model") or "fixed-offline") if isinstance(first, Mapping) else "fixed-offline"

    def _plan_json(self) -> str:
        spec = _INTENT_PLANS.get(self._case.expected_intent) or _PLANS[self._case.category]
        return json.dumps({
            "objective": f"离线评测固定计划：{self._case.id}",
            "source_mode": spec["source_mode"],
            "steps": [{"id": step_id, "kind": kind, "required": True, "depends_on": list(depends)}
                      for step_id, kind, depends in spec["steps"]],
            "acceptance": ["固定离线用例按预期完成"],
        }, ensure_ascii=False)

    def chat(self, *, messages: Any = None, system: Any = None, response_format: Any = None, **_kwargs: Any) -> dict[str, Any]:
        self._fixture.record_attempt("model")
        if isinstance(response_format, Mapping) and response_format.get("type") == "json_object":
            return {"content": self._plan_json()}
        return {"content": self._answer}

    def chat_stream(self, *, messages: Any = None, system: Any = None, tools: Any = None, **_kwargs: Any):
        self._fixture.record_attempt("model")
        yield {"type": "delta", "text": self._answer, "reasoning": ""}
        yield {"type": "done", "answer": self._answer, "reasoning": "",
               "model": self._model, "usage": {}}


class _FixtureRagConfig:
    """Controlled offline RAG/MCP configuration: no web search, no real models."""

    enabled = True
    store_path = ""
    chunk_size = 800
    chunk_overlap = 100
    top_k = 8
    embedding_model = "fixture-offline"
    auto_ingest = False
    enhanced_analysis = False
    analysis_dimensions: tuple[str, ...] = ()
    mcp_tools = True
    mcp_tool_timeout = 5
    mcp_max_tool_rounds = 3
    mcp_max_tool_calls = 4
    mcp_tool_whitelist: tuple[str, ...] = ()
    web_search = False
    web_search_timeout = 5
    rerank = False
    rerank_candidates = 30
    rerank_score_threshold = 0.5
    rerank_margin_threshold = 0.05

    @classmethod
    def load(cls) -> "_FixtureRagConfig":
        return cls()


class _FixtureEvidenceNormalizer:
    """Evidence normalizer that maps fixture citations to PDF artifacts without a real file.

    The file-existence lookup is local infrastructure, not the security invariant; Scope
    membership and fact derivation still run through the real FactNormalizer path.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        from webapp.chat_evidence import EvidenceNormalizer
        self._real = EvidenceNormalizer()

    def normalize_rag_citations(self, citations: Any, *, analysis_dir: str, reports_dir: str,
                                jump_version: int) -> tuple[Any, ...]:
        from webapp.chat_evidence import pdf_page_url
        from webapp.chat_models import EvidenceArtifact

        artifacts = []
        for citation in citations or ():
            if not isinstance(citation, Mapping):
                continue
            if str(citation.get("source") or "") != "pdf":
                continue
            page = citation.get("page")
            snippet = str(citation.get("snippet") or "")
            report_id = str(citation.get("report_id") or "")
            if not report_id or not snippet or not isinstance(page, int) or isinstance(page, bool) or page <= 0:
                continue
            filename = f"{report_id.replace(':', '-')}.pdf"
            artifacts.append(EvidenceArtifact.pdf(
                report_id, filename, page, snippet,
                pdf_url=pdf_page_url(filename, page, jump_version),
            ))
        return tuple(artifacts)

    def normalize_web_sources(self, rows: Any, *, fetched_at: str) -> tuple[Any, ...]:
        return self._real.normalize_web_sources(rows, fetched_at=fetched_at)

    def normalize_tool_event(self, *args: Any, **kwargs: Any) -> Any:
        return self._real.normalize_tool_event(*args, **kwargs)


class _FixtureMarketMcp:
    """Local MCP stand-in for the recap aggregation; counts one attempt per tool call."""

    def __init__(self, fixture: ProviderFixture) -> None:
        self._fixture = fixture

    def call_tool(self, name: str, _arguments: Any = None, **_kwargs: Any) -> dict[str, Any]:
        self._fixture.record_attempt("mcp")
        return {"tool": name, "as_of": "2026-10-08",
                "data": [{"name": name, "date": "2026-10-08", "pool_count": 50}],
                "note": "固定离线替身聚合结果，仅代表样本池"}


class _FixtureQuote:
    """Local Tencent quote stand-in; never reaches the network."""

    def __init__(self, fixture: ProviderFixture, rows: Iterable[Mapping[str, Any]]) -> None:
        self._fixture = fixture
        self._rows = tuple(dict(row) for row in rows)

    def realtime(self, symbols: Iterable[str] = (), **_kwargs: Any) -> list[dict[str, Any]]:
        self._fixture.record_attempt("market")
        wanted = {str(symbol) for symbol in symbols}
        return [dict(row) for row in self._rows if not wanted or str(row.get("symbol")) in wanted]

    def kline(self, symbol: str, **_kwargs: Any) -> list[dict[str, Any]]:
        self._fixture.record_attempt("market")
        return [dict(row) for row in self._rows if str(row.get("symbol")) == str(symbol)]

    def indices(self, **_kwargs: Any) -> list[dict[str, Any]]:
        self._fixture.record_attempt("market")
        return [dict(row) for row in self._rows]


class OfflineChatHarness:
    """Apply the case's frozen providers to the real server module for one case."""

    def __init__(self, case: RuntimeCase, *, workspace: Path, corpus: Iterable[Mapping[str, Any]] | None = None) -> None:
        self.case = case
        self.workspace = workspace
        self.fixture = ProviderFixture(version="fixed-offline-v1", case_id=case.id, limits=case.max_calls)
        self._corpus = tuple(corpus if corpus is not None else load_corpus())

    def _frozen_scope(self) -> Scope:
        raw = self.case.scope
        mode = str(raw.get("mode") or "")
        codes = tuple(str(code) for code in raw.get("companies") or ())
        report_ids = tuple(str(item) for item in raw.get("report_ids") or ())
        if mode in {"general_knowledge", "whole_corpus"} or not codes:
            return Scope("whole_corpus", (), (), fallback_reason="离线评测冻结范围")
        company = CompanyRef(codes[0], _COMPANY_NAMES.get(codes[0], codes[0]))
        return Scope("company_only", (company,), report_ids)

    def _retrieval_unavailable(self) -> bool:
        entries = self.case.provider_fixture.get("retrieval") or ()
        return any(isinstance(entry, Mapping) and entry.get("error") for entry in entries)

    def _install(self, stack: ExitStack, server: Any) -> None:
        from financial_report_fetcher.rag.qa import RagQA

        store = _FixtureStore(self._corpus, self.fixture, unavailable=self._retrieval_unavailable())
        ai = _FixtureAI(self.case, self.fixture)
        quote_rows = self.case.provider_fixture.get("market") or ()
        scope = self._frozen_scope()
        market_rows = tuple(dict(row) for row in quote_rows if isinstance(row, Mapping))
        use_mcp = self.case.category == "recap"

        def frozen_scope(_body: Any, _run_id: str = "", *, requires_company: bool = False) -> Any:
            from webapp.chat_scope import ScopeResolution
            if scope.mode == "whole_corpus" and requires_company:
                return ScopeResolution(None, "离线评测未提供公司范围。")
            return ScopeResolution(scope)

        def frozen_window(question: str) -> Any:
            from webapp.chat_time import resolve_market_window
            return resolve_market_window(question, now=FROZEN_NOW)

        patches = {
            "DATA_DIR": self.workspace,
            "chat_store": _isolated_store(self.workspace),
            "ai_client": ai,
            "rag_qa": RagQA(store, ai, top_k=8, company_code_resolver=lambda _text: None),
            "tencent_quote": _FixtureQuote(self.fixture, market_rows),
            "_resolve_scope": frozen_scope,
            "resolve_market_window": frozen_window,
            "EvidenceNormalizer": _FixtureEvidenceNormalizer,
            "RagConfig": _FixtureRagConfig,
            "market_data_mcp": _FixtureMarketMcp(self.fixture),
            "_mcp_tool_defs_cache": [
                {"type": "function", "function": {"name": "stock_zt_pool"}},
                {"type": "function", "function": {"name": "stock_sector_fund_flow_rank"}},
                {"type": "function", "function": {"name": "get_realtime_quote"}},
            ],
        }
        for name, value in patches.items():
            stack.enter_context(patch.object(server, name, value))
        if use_mcp:
            stack.enter_context(patch.object(server, "_build_chat_tool_defs", lambda _cfg: [
                {"type": "function", "function": {"name": "market_overview", "parameters": {"type": "object", "properties": {}}}},
            ]))
            stack.enter_context(patch.object(server, "_build_chat_tool_executor", _fixture_recap_executor(self.fixture)))
        else:
            stack.enter_context(patch.object(server, "_build_chat_tool_defs", lambda _cfg: None))

    def run(self, app: FastAPI) -> RuntimeObservation:
        from webapp import server

        self.workspace.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            _network_guard(stack)
            self._install(stack, server)
            return _drive(self.case, app, self.fixture)


def _fixture_recap_executor(fixture: ProviderFixture):
    """MCP executor stand-in for the recap case; counts one physical attempt per call."""
    def build(_cfg: Any, **_kwargs: Any):
        def execute(name: str, _arguments: Mapping[str, Any]) -> str:
            fixture.record_attempt("mcp")
            return json.dumps({"tool": name, "pool_count": 50, "note": "固定离线替身聚合结果"}, ensure_ascii=False)
        return execute
    return build


def _isolated_store(workspace: Path):
    from webapp.chat_store import ChatStore
    return ChatStore(str(workspace / "sessions.json"))


def _drive(case: RuntimeCase, app: FastAPI, fixture: ProviderFixture) -> RuntimeObservation:
    """POST each turn to /api/chat/stream and inspect its durable final AnswerRun.

    Starlette's TestClient buffers StreamingResponse bodies, so timing fields are
    deliberately unavailable instead of being misreported as server latency.
    """
    session_id = ""
    all_events: list[tuple[str, dict[str, Any]]] = []
    started = time.perf_counter()
    with TestClient(app) as client:
        for turn in case.turns:
            response = client.post("/api/chat/stream", json={
                "question": turn,
                "session_id": session_id or None,
                "use_mcp": case.category == "recap",
                "scope_mode": "auto",
            })
            if response.status_code != 200:
                raise RuntimeError(f"offline chat endpoint returned HTTP {response.status_code}")
            events = _parse_sse(response.text)
            all_events.extend(events)
            session_event = next((payload for name, payload in events if name == "session"), None)
            if session_event and isinstance(session_event.get("session_id"), str):
                session_id = session_event["session_id"]
            if not session_id:
                raise AssertionError("chat stream did not provide a session identity")
            terminal_events = [(name, payload) for name, payload in events
                               if name in {"done", "error", "stopped", "supplement_needed"}]
            if len(terminal_events) != 1:
                raise AssertionError(f"expected exactly one terminal SSE event, got {len(terminal_events)}")
    elapsed = time.perf_counter() - started
    fixture.assert_all_calls_frozen()
    names = [name for name, _ in all_events]
    run_ids = [payload.get("run_id") for name, payload in all_events if name == "run_started"]
    if len(run_ids) != len(case.turns) or any(not value for value in run_ids):
        raise AssertionError("each chat turn must emit exactly one run_started identity")
    final_name, terminal = next((name, payload) for name, payload in reversed(all_events)
                                if name in {"done", "error", "stopped", "supplement_needed"})
    run = terminal.get("run") if isinstance(terminal.get("run"), dict) else {}
    status = str(run.get("status") or ("failed" if final_name == "error" else final_name))
    answer = str(run.get("content") or terminal.get("answer") or terminal.get("error") or "")
    # Re-read durable state from the server ChatStore rather than trusting SSE alone.
    from webapp import server
    saved = server.chat_store.get_session(session_id) or {}
    durable_run = next((message.get("run") for message in reversed(saved.get("messages", []))
                        if isinstance(message, dict) and isinstance(message.get("run"), dict)
                        and message["run"].get("id") == run_ids[-1]), None)
    if durable_run is None:
        raise AssertionError("terminal AnswerRun was not persisted in ChatStore")
    if durable_run.get("status") != status:
        raise AssertionError("terminal SSE status does not match the persisted AnswerRun")
    return RuntimeObservation(
        events=tuple(names), persisted_status=status, answer=answer,
        session_id=session_id, run_id=str(run_ids[-1]),
        call_attempts=dict(fixture.calls), first_frame_seconds=None,
        first_content_seconds=None, total_seconds=elapsed,
        usage=run.get("usage") if isinstance(run.get("usage"), dict) else None,
        terminal=terminal,
    )


def run_case(case: RuntimeCase, app: FastAPI, *, fixture: ProviderFixture | None = None,
             workspace: Path | None = None) -> RuntimeObservation:
    """Evaluate one case end-to-end against its own frozen providers."""
    if workspace is None:
        import tempfile
        workspace = Path(tempfile.mkdtemp(prefix=f"chat-eval-{case.id}-"))
    harness = OfflineChatHarness(case, workspace=workspace)
    if fixture is not None:
        harness.fixture = fixture
    return harness.run(app)
