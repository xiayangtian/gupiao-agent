"""webapp.server API 测试（TestClient + monkeypatch 替换模块级组件）"""

import itertools
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient

import webapp.server as server
from webapp.chat_intent import ChatIntentAnalysis
from financial_report_fetcher.analysis_pipeline import analysis_output_stem
from financial_report_fetcher.models import DownloadStatus, ReportMeta, ReportType
from financial_report_fetcher.rag.ingest import IngestResult
from financial_report_fetcher.report_identity import build_report_filename, build_report_id


# 已验证 PDF 事实的确定性证据标识：``report_id#p{page}``（与 artifact 身份一致）。
PDF_EVIDENCE_ID = "601288:2026-06-30:semi_annual#p40"


def test_startup_removes_only_legacy_research_memory_sidecar(tmp_path, monkeypatch):
    memory = tmp_path / "research_memory.json"
    workspace = tmp_path / "research_workspace.json"
    memory.write_text('{"entries": []}', encoding="utf-8")
    workspace.write_text('{"items": []}', encoding="utf-8")
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    server._remove_legacy_research_memory_sidecar()

    assert not memory.exists()
    assert workspace.exists()


def test_startup_fails_loudly_when_legacy_research_memory_sidecar_cannot_be_removed(tmp_path, monkeypatch):
    memory = tmp_path / "research_memory.json"
    memory.write_text('{"entries": []}', encoding="utf-8")
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    def fail_unlink(self, *, missing_ok=False):
        if self == memory:
            raise OSError("permission denied")
        return None

    monkeypatch.setattr(type(memory), "unlink", fail_unlink)

    with pytest.raises(RuntimeError, match="无法清理已移除的研究记忆数据"):
        server._remove_legacy_research_memory_sidecar()


def test_startup_lifecycle_removes_legacy_research_memory_sidecar(tmp_path, monkeypatch):
    memory = tmp_path / "research_memory.json"
    memory.write_text('{"entries": []}', encoding="utf-8")
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    with TestClient(server.app):
        assert not memory.exists()


def test_startup_lifecycle_fails_when_legacy_research_memory_sidecar_cannot_be_removed(tmp_path, monkeypatch):
    memory = tmp_path / "research_memory.json"
    memory.write_text('{"entries": []}', encoding="utf-8")
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    def fail_unlink(self, *, missing_ok=False):
        if self == memory:
            raise OSError("permission denied")
        return None

    monkeypatch.setattr(type(memory), "unlink", fail_unlink)

    with pytest.raises(RuntimeError, match="无法清理已移除的研究记忆数据"):
        with TestClient(server.app):
            pass


@pytest.mark.parametrize(("method", "path"), [
    ("get", "/api/research/memory"),
    ("get", "/api/research/memory/facts?session_id=test"),
    ("get", "/api/research/memory/artifacts?session_id=test"),
    ("get", "/api/research/memory/decisions?session_id=test"),
    ("get", "/api/research/memory/entry-1"),
    ("post", "/api/research/memory/facts?session_id=test"),
    ("post", "/api/research/memory/artifacts?session_id=test"),
    ("post", "/api/research/memory/decisions?session_id=test"),
    ("delete", "/api/research/memory/entry-1"),
])
def test_removed_research_memory_routes_return_404(client, method, path):
    response = getattr(client, method)(path)

    assert response.status_code == 404


def _read_sse(response):
    """解析 SSE 响应体为 [(event_name, data), ...] 列表。"""
    events = []
    for block in response.text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if event_name is not None:
            events.append((event_name, data))
    return events


def _event(events, name):
    """从解析后的 SSE 事件中取出指定事件的数据；找不到则报错。"""
    for event_name, data in events:
        if event_name == name:
            return data
    raise AssertionError(f"事件 {name!r} 不存在，实际：{[e for e, _ in events]}")


def _disconnect_after_first_delta(client, monkeypatch, payload, deltas):
    """模拟客户端在首个 delta 后停止读取：用只产出 delta、不产出 done 的
    生成器驱动流式端点（等同于「没有 done」的停止语义），返回解析出的 SSE 事件。

    TestClient 的 ASGI 传输会缓冲整个响应体，无法真正中途断开连接；这里以
    生产线程只产出部分 delta 后结束来复现同一条 stopped 持久化路径。
    """
    class StalledRagQA:
        def answer_stream(self, question, history=None, filters=None, tools=None,
                          priority_report_id=None, scope=None, run_id=None):
            for text in deltas:
                yield {"type": "delta", "text": text, "reasoning": ""}
            # 不再产出 done：模拟停止/断开

    monkeypatch.setattr(server, "rag_qa", StalledRagQA())
    return _read_sse(client.post("/api/chat/stream", json=payload))


def _configure_scoped_rag_answer(env, citations=None, *, report_ids=None):
    """配置 fake rag_store + rag_qa，让 /api/chat/stream 解析公司范围并产出引用。

    把 PDF 写到隔离的 REPORTS_DIR，使 EvidenceNormalizer 能把 pdf 引用映射为
    可跳页证据 artifact；默认报告为 601288 半年报。
    """
    monkeypatch = env["monkeypatch"]
    citations = list(citations or [])
    report_ids = list(report_ids or sorted(
        {c["report_id"] for c in citations} or ["601288:2026-06-30:semi_annual"]
    ))

    reports_dir = server.REPORTS_DIR
    os.makedirs(reports_dir, exist_ok=True)
    pdf_path = os.path.join(reports_dir, "农业银行_601288_半年报_2026.pdf")
    if not os.path.exists(pdf_path):
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 fake")

    class FakeRagStore:
        def list_report_ids(self):
            return list(report_ids)

    class ScopedRagQA:
        def answer_stream(self, question, history=None, filters=None, tools=None,
                          priority_report_id=None, scope=None, run_id=None):
            yield {"type": "delta", "text": "经营现金流为", "reasoning": ""}
            yield {
                "type": "done",
                "answer": "经营现金流为",
                "reasoning": "",
                "citations": list(citations),
                "model": "m",
                "usage": {"total_tokens": 7},
                "tools_used": [],
                "web_sources": [],
                "retrieval_report_ids": list(report_ids),
                "retrieval_degraded": False,
            }

    monkeypatch.setattr(server, "rag_store", FakeRagStore())
    monkeypatch.setattr(server, "rag_qa", ScopedRagQA())


def _meta(code, rt, period, name="测试公司"):
    return ReportMeta(
        company_id=code,
        company_name=name,
        report_type=rt,
        period=period,
        download_url="http://cninfo.example/x.pdf",
        title=f"{name}{period.year}年度报告",
    )


class _FakeReport:
    """模拟 AnalysisReport，只实现 Web 侧用到的接口"""

    def to_json(self):
        return {
            "meta": {"company": "长江电力（600900）", "report_year": 2025},
            "dimensions": [
                {"id": "financial_summary", "name": "财务摘要", "content": "营收 500 亿"}
            ],
        }

    def save(self, output_dir):
        return os.path.join(output_dir, "长江电力_600900_2025_分析报告.md")


class _FakeV3Document:
    def __init__(self, stage="completed"):
        self.stage = stage

    def to_dict(self):
        return {
            "schema_version": 3,
            "analysis_id": "长江电力_600900_2025-12-31_分析报告",
            "report_id": "600900:2025-12-31:annual",
            "stage": self.stage,
            "quick": {"conclusions": [{"claim": "营收保持稳定"}]},
            "sections": [{"section_id": "financial-overview", "title": "财务概览"}],
        }


class FakeProgressivePipeline:
    def __init__(self):
        self.requests = []
        self.side_effect = None

    def run(self, request, emit, stop_event):
        self.requests.append(request)
        if self.side_effect is not None:
            return self.side_effect(request, emit, stop_event)
        document = _FakeV3Document()
        emit("job.stage_changed", {"stage": "fast_ready"})
        emit("quick.ready", {"quick": document.to_dict()["quick"]})
        emit("section.ready", {"section": document.to_dict()["sections"][0]})
        emit("job.completed", {"analysis": document.to_dict()})
        return document


class FakeIndex:
    """模拟 StockIndex（方法面足够即可，不继承）"""

    def start(self):
        pass

    def wait_ready(self, timeout=5.0):
        return True

    def search(self, q, limit=10):
        return [{"code": "600900", "name": "长江电力"}] if q == "长江" else []

    def company_name(self, code):
        return {"600900": "长江电力", "601288": "农业银行"}.get(code)

    def is_valid_code(self, code):
        return code in {"600900", "601288"}

    def match_company_name(self, text):
        if "长江电力" in text:
            return {"code": "600900", "name": "长江电力"}
        if "农业银行" in text:
            return {"code": "601288", "name": "农业银行"}
        return None

    @property
    def is_ready(self):
        return True


def _legacy_test_intent_analyzer(question, context=""):
    """Use the old deterministic router only as a test double for legacy API fixtures."""
    import re

    from webapp.chat_intent import ChatIntentAnalysis
    from webapp.chat_policy import IntentRouter, QuestionRouter
    from webapp.chat_scope import Scope
    from webapp.chat_time import is_orphan_market_horizon

    pending = None
    for line in reversed((context or "").splitlines()):
        if '"pending_question"' in line:
            try:
                pending = json.loads(line)
            except json.JSONDecodeError:
                pending = None
            break
    if isinstance(pending, dict) and pending.get("missing_field") == "window":
        if re.fullmatch(r"\s*\d{6}\s*", question) or any(term in question for term in ("日", "周", "月", "年", "半年")):
            intent = pending.get("prior_intent")
            task = "report_trend" if intent == "financial_trend" else "trend"
            category = "financial_report" if intent == "financial_trend" else "market_analysis"
            subject_kind = "equity" if intent != "market_recap" else "broad_market"
            return ChatIntentAnalysis(category, subject_kind, str(pending.get("pending_question") or ""),
                                      "", task, question.strip(), False, "")
    if any(term in question for term in ("原因", "新闻", "公告")):
        return ChatIntentAnalysis("market_analysis", "other", "长江电力", "600900",
                                  "attribution", question, False, "")
    if any(name in question for name in ("长江电力", "农业银行", "福耀玻璃")) and any(
        term in question for term in ("营收", "收入", "利润", "现金流", "财报")
    ):
        return ChatIntentAnalysis("financial_report", "equity", question, "", "report_fact",
                                  question, False, "")
    route = QuestionRouter().classify(question)
    code_match = re.search(r"(?<!\d)(\d{6})(?!\d)", question)
    code = code_match.group(1) if code_match else ""
    if IntentRouter().classify(question, Scope.whole_corpus()).intent == "research_task":
        subject_kind = "equity" if code or any(
            name in question for name in ("长江电力", "农业银行", "福耀玻璃")
        ) else "other"
        time_expression = question if any(term in question for term in ("当前", "今日", "今天", "最新", "近期")) else ""
        return ChatIntentAnalysis("financial_knowledge", subject_kind, question, code, "research",
                                  time_expression, False, "")
    if route.intent == "market_recap":
        return ChatIntentAnalysis("market_analysis", "broad_market", "A股", code, "recap", question,
                                  False, "")
    if route.intent in {"market_quote", "market_trend"}:
        return ChatIntentAnalysis("market_analysis", "equity", question, code,
                                  "quote" if route.intent == "market_quote" else "trend",
                                  question, False, "")
    if route.intent == "financial_trend":
        task = "report_fact" if "经营现金流变化" in question else "report_trend"
        return ChatIntentAnalysis("financial_report", "equity", question, code, task,
                                  question, False, "")
    if route.intent == "report_fact":
        category = "financial_report"
        task = "report_fact"
        subject_kind = "industry" if IntentRouter().classify(question, Scope.whole_corpus()).intent == "industry_benchmark" else "equity"
        return ChatIntentAnalysis(category, subject_kind, question, code, task, question, False, "")
    if route.intent == "clarification":
        if is_orphan_market_horizon(question):
            return ChatIntentAnalysis("market_analysis", "none", "", "", "trend", question,
                                      True, "请说明希望分析的市场或证券对象。")
        return ChatIntentAnalysis("unclear", "none", "", "", "none", "", True,
                                  "请具体说明你的金融问题。")
    if route.intent == "general_web":
        return ChatIntentAnalysis("financial_knowledge", "other", question, code, "research",
                                  question, False, "")
    return ChatIntentAnalysis("financial_knowledge", "none", "", "", "explain", "", False, "")


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """替换 server 模块级组件为假实现；yield 组件引用供断言"""
    task_managers = []

    def make_task_manager(**kwargs):
        db_path = tmp_path / f"tasks-{len(task_managers)}.sqlite3"
        manager = server.TaskManager(db_path=str(db_path), **kwargs)
        task_managers.append(manager)
        return manager

    fake_ds = MagicMock()
    fake_ds.fetch_reports.return_value = [
        _meta("600900", ReportType.ANNUAL, date(2025, 12, 31), "长江电力"),
        _meta("600900", ReportType.QUARTERLY, date(2025, 3, 31), "长江电力"),
    ]

    fake_ai = MagicMock()
    fake_ai.api_key = "sk-test"

    fake_analyzer = MagicMock()
    fake_analyzer.analyze.return_value = _FakeReport()
    fake_analyzer.qa.return_value = "测试 AI 回答"

    fake_dl = MagicMock()
    fake_dl.download_one.return_value = DownloadStatus.SUCCESS
    fake_pipeline = FakeProgressivePipeline()

    monkeypatch.setattr(server, "datasource", fake_ds)
    monkeypatch.setattr(server, "stock_index", FakeIndex())
    monkeypatch.setattr(server, "ai_client", fake_ai)
    monkeypatch.setattr(server, "_analyze_chat_intent", _legacy_test_intent_analyzer)
    monkeypatch.setattr(server, "analyzer", fake_analyzer)
    monkeypatch.setattr(server, "downloader", fake_dl)
    monkeypatch.setattr(server, "progressive_pipeline", fake_pipeline)
    monkeypatch.setattr(server, "task_manager", make_task_manager())
    monkeypatch.setattr(server, "chat_sessions", {})
    # 隔离真实 reports/ 目录，downloaded 断言与本地磁盘状态无关
    monkeypatch.setattr(server, "REPORTS_DIR", str(tmp_path))
    # 隔离真实 MCP：问答端点默认不注入工具（TestMcpChat 等按需覆盖）
    monkeypatch.setattr(server, "_mcp_tool_defs", lambda: None)

    yield {
        "fake_ds": fake_ds,
        "fake_ai": fake_ai,
        "fake_analyzer": fake_analyzer,
        "fake_dl": fake_dl,
        "fake_pipeline": fake_pipeline,
        "make_task_manager": make_task_manager,
        "monkeypatch": monkeypatch,
        "tmp_path": tmp_path,
    }

    for manager in task_managers:
        manager.shutdown()


@pytest.fixture()
def client(env):
    with TestClient(server.app) as c:
        yield c


def test_chat_stream_clarifies_unresolved_company_before_planning_or_sources(client, env, monkeypatch):
    planner_calls = []
    source_calls = []

    class ForbiddenPlanner:
        def __init__(self, *args, **kwargs):
            pass

        def plan(self, *args, **kwargs):
            planner_calls.append((args, kwargs))
            raise AssertionError("planner must not run before company clarification")

    monkeypatch.setattr(server, "ExecutionPlanner", ForbiddenPlanner)
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda *args, **kwargs: source_calls.append("tools"))
    monkeypatch.setattr(server, "_chat_qa_or_degraded", lambda: source_calls.append("qa") or object())

    response = client.post("/api/chat/stream", json={
        "question": "未知公司近五交易日股价走势", "use_mcp": True,
    })
    events = _read_sse(response)

    assert response.status_code == 200
    assert "clarification" in [name for name, _ in events]
    assert "公司" in _event(events, "clarification")["message"]
    assert planner_calls == []
    assert source_calls == []


def test_chat_stream_uses_model_classification_for_lowercase_a_share(client, env, monkeypatch, tmp_path):
    from financial_report_fetcher.rag.config import RagConfig
    from financial_report_fetcher.rag.qa import RagQA
    from webapp.chat_store import ChatStore
    from webapp.execution_planner import PlanningResult

    analysis = ChatIntentAnalysis(
        "market_analysis", "broad_market", "A股", "", "recap", "今日", False, "",
    )
    monkeypatch.setattr(server, "_analyze_chat_intent", lambda question, context="": analysis)
    monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / "model-intent-market.json")))
    monkeypatch.setattr(server.RagConfig, "load", classmethod(
        lambda _cls: RagConfig(mcp_tools=False, web_search=False),
    ))
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: None)
    monkeypatch.setattr(server.tencent_quote, "index", lambda codes: [
        {"code": code, "name": "指数", "price": 1000.0, "change": 1.0} for code in codes
    ])

    class FailedPlanner:
        def __init__(self, *_args):
            pass

        def plan(self, *_args, **_kwargs):
            return PlanningResult("fallback", None)

    class AnswerAI:
        def chat_stream(self, **_kwargs):
            yield {"type": "done", "answer": "已取得指数范围数据。", "model": "fixture", "usage": {}}

    class NoStore:
        def query(self, *_args, **_kwargs):
            raise AssertionError("market recap must not retrieve company reports")

    monkeypatch.setattr(server, "ExecutionPlanner", FailedPlanner)
    monkeypatch.setattr(server, "rag_qa", RagQA(NoStore(), AnswerAI()))
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "分析今日a股行情", "use_mcp": False,
    }))

    assert "clarification" not in [name for name, _ in events]
    assert _event(events, "scope_resolved")["scope"]["companies"] == []
    assert _event(events, "done")["run"] is not None


def test_chat_stream_rejects_non_financial_before_answer_or_tools(client, env, monkeypatch):
    analysis = ChatIntentAnalysis(
        "non_financial", "none", "", "", "none", "", False, "",
    )
    monkeypatch.setattr(server, "_analyze_chat_intent", lambda question, context="": analysis)
    source_calls = []
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda *_args: source_calls.append("tools"))

    events = _read_sse(client.post("/api/chat/stream", json={"question": "写一首诗"}))

    assert "clarification" in [name for name, _ in events]
    assert "金融" in _event(events, "clarification")["message"]
    assert source_calls == []
    env["fake_ai"].chat.assert_not_called()


def test_chat_stream_persists_and_consumes_orphan_window_clarification(client, env, monkeypatch, tmp_path):
    from webapp.chat_store import ChatStore

    store = ChatStore(str(tmp_path / "pending-market.json"))
    monkeypatch.setattr(server, "chat_store", store)
    captured = []

    def require_scope(body, run_id="", *, requires_company=False, question=None):
        captured.append((question or body.question, requires_company))
        return server.ScopeResolution(None, "请提供公司名称或股票代码。")

    monkeypatch.setattr(server, "_resolve_scope", require_scope)
    first = _read_sse(client.post("/api/chat/stream", json={
        "question": "近五交易日", "use_mcp": False,
    }))
    sid = _event(first, "session")["session_id"]
    session = store.get_session(sid)
    assert "pending_clarification" in session
    assert session["pending_clarification"]["missing_field"] == "subject"
    assert _event(first, "done")["run"] is None

    second = _read_sse(client.post("/api/chat/stream", json={
        "question": "600660", "session_id": sid, "use_mcp": False,
    }))

    assert captured == [("近五交易日 600660", True)]
    next_pending = store.get_session(sid)["pending_clarification"]
    assert next_pending["version"] != session["pending_clarification"]["version"]
    assert next_pending["missing_field"] == "subject"
    assert next_pending["market_window"]["trading_days"] == 5
    assert "clarification" in [name for name, _ in second]


def test_chat_stream_clarifies_missing_window_before_planning_or_sources(client, env, monkeypatch):
    planner_calls = []
    source_calls = []

    class ForbiddenPlanner:
        def __init__(self, *args, **kwargs):
            pass

        def plan(self, *args, **kwargs):
            planner_calls.append((args, kwargs))
            raise AssertionError("planner must not run before window clarification")

    monkeypatch.setattr(server, "ExecutionPlanner", ForbiddenPlanner)
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda *args, **kwargs: source_calls.append("tools"))
    monkeypatch.setattr(server, "_chat_qa_or_degraded", lambda: source_calls.append("qa") or object())

    response = client.post("/api/chat/stream", json={
        "question": "长江电力股价走势", "use_mcp": True,
    })
    events = _read_sse(response)

    assert response.status_code == 200
    assert "clarification" in [name for name, _ in events]
    assert "时间" in _event(events, "clarification")["message"]
    assert planner_calls == []
    assert source_calls == []


def test_chat_scope_preserves_known_company_when_no_reports_are_indexed(env, monkeypatch):
    class EmptyRagStore:
        def list_report_ids(self):
            return []

    monkeypatch.setattr(server, "rag_store", EmptyRagStore())
    resolution = server._resolve_scope(
        server.StreamChatRequest(question="600900最新股价"),
        requires_company=True,
    )

    assert resolution.scope is not None
    assert resolution.scope.companies[0].code == "600900"
    assert resolution.scope.report_ids == ()


def test_market_planner_failure_uses_deterministic_index_only_plan_without_pdf(client, env, monkeypatch, tmp_path):
    from financial_report_fetcher.rag.config import RagConfig
    from financial_report_fetcher.rag.qa import RagQA
    from webapp.chat_store import ChatStore
    from webapp.execution_planner import PlanningResult

    monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / "safe-market.json")))
    monkeypatch.setattr(server.RagConfig, "load", classmethod(lambda _cls: RagConfig(mcp_tools=False, web_search=False)))
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: None)
    calls = []
    monkeypatch.setattr(server.tencent_quote, "kline", lambda symbol, **kwargs: (
        calls.append((symbol, kwargs)) or [{"date": "2026-09-24", "close": 10.0}]
    ))

    class FailedPlanner:
        def __init__(self, *_args):
            pass
        def plan(self, *_args, **_kwargs):
            return PlanningResult("fallback", None)

    class AnswerAI:
        def chat_stream(self, **_kwargs):
            yield {"type": "done", "answer": "仅依据已取得的指数行情。", "model": "fixture", "usage": {}}

    class NoStore:
        def query(self, *_args, **_kwargs):
            raise AssertionError("market plan must not retrieve PDF evidence")

    monkeypatch.setattr(server, "ExecutionPlanner", FailedPlanner)
    monkeypatch.setattr(server, "rag_qa", RagQA(NoStore(), AnswerAI()))
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "2026-09-24 A股复盘", "scope_mode": "whole_corpus", "use_mcp": False,
    }))

    assert [step["kind"] for step in _event(events, "execution_plan")["steps"]] == [
        "market_indices", "answer",
    ]
    assert len(calls) == 4
    assert all(item[1]["count"] >= 1 for item in calls)
    assert [step["kind"] for step in _event(events, "done")["run"]["execution_steps"]] == [
        "market_indices", "answer",
    ]
    assert _event(events, "done")["run"]["retrieval_report_ids"] == []


@pytest.mark.parametrize(("whitelist", "missing_tool", "breaker_open"), [
    (["stock_zt_pool", "stock_sector_fund_flow_rank"], "stock_sector_fund_flow_rank", False),
    (["stock_zt_pool"], None, False),
    (["stock_zt_pool", "stock_sector_fund_flow_rank"], None, True),
])
def test_market_overview_requires_complete_live_allowlist(
    client, env, monkeypatch, tmp_path, whitelist, missing_tool, breaker_open,
):
    from financial_report_fetcher.rag.config import RagConfig
    from financial_report_fetcher.rag.qa import RagQA
    from webapp.chat_store import ChatStore
    from webapp.execution_planner import PlanningResult

    names = ["stock_zt_pool", "stock_sector_fund_flow_rank"]
    if missing_tool:
        names.remove(missing_tool)
    definitions = [{"type": "function", "function": {"name": name}} for name in names]
    monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / f"mcp-gate-{len(whitelist)}-{breaker_open}.json")))
    monkeypatch.setattr(server.RagConfig, "load", classmethod(lambda _cls: RagConfig(
        mcp_tools=True, web_search=False, mcp_max_tool_calls=9, mcp_tool_whitelist=whitelist,
    )))
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: definitions)
    monkeypatch.setattr(server, "_mcp_tool_defs_cache", definitions)
    monkeypatch.setattr(server, "mcp_breaker", type("Breaker", (), {"allow": lambda self: not breaker_open})())
    monkeypatch.setattr(server, "_build_chat_tool_executor", lambda *_args, **_kwargs: lambda *_args: "")
    mcp_calls, index_calls = [], []
    monkeypatch.setattr(server.market_data_mcp, "call_tool", lambda *args, **kwargs: mcp_calls.append(args))
    monkeypatch.setattr(server.tencent_quote, "kline", lambda symbol, **kwargs: (
        index_calls.append(symbol) or [{"date": "2026-09-24", "close": 1.0}]
    ))

    class FailedPlanner:
        def __init__(self, *_args): pass
        def plan(self, *_args, **_kwargs): return PlanningResult("fallback", None)
    class AnswerAI:
        def chat_stream(self, **_kwargs):
            yield {"type": "done", "answer": "仅说明实际指数覆盖。", "model": "fixture", "usage": {}}
    class NoStore:
        def query(self, *_args, **_kwargs): raise AssertionError("market recap must not retrieve")

    monkeypatch.setattr(server, "ExecutionPlanner", FailedPlanner)
    monkeypatch.setattr(server, "rag_qa", RagQA(NoStore(), AnswerAI()))
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "2026-09-24 A股复盘", "scope_mode": "whole_corpus", "use_mcp": True,
    }))

    assert [step["kind"] for step in _event(events, "execution_plan")["steps"]] == [
        "market_indices", "answer",
    ]
    recap_run = _event(events, "done")["run"]
    assert recap_run["status"] == "partial"
    assert "行情数据覆盖有限" in recap_run["content"]
    assert len(index_calls) == 4
    assert mcp_calls == []


def test_market_planner_failure_without_minimum_budget_stops_before_any_source(client, env, monkeypatch, tmp_path):
    from financial_report_fetcher.rag.config import RagConfig
    from webapp.chat_store import ChatStore
    from webapp.execution_planner import PlanningResult

    monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / "under-budget.json")))
    monkeypatch.setattr(server.RagConfig, "load", classmethod(lambda _cls: RagConfig(
        mcp_tools=False, web_search=False, mcp_max_tool_calls=3,
        market_recap_max_tool_calls=3,
    )))
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: None)
    source_calls = []
    monkeypatch.setattr(server.tencent_quote, "kline", lambda *_args, **_kwargs: source_calls.append("kline"))

    class FailedPlanner:
        def __init__(self, *_args):
            pass
        def plan(self, *_args, **_kwargs):
            return PlanningResult("fallback", None)

    class KnowledgeFallbackQA:
        def answer_from_context(self, question, **kwargs):
            assert kwargs["knowledge_mode"] is True
            yield {"type": "done", "answer": "未取得可核验行情数据，不能确认指数点位；可从指数方向和市场广度分析。",
                   "citations": [], "model": "fixture", "usage": {}}

    monkeypatch.setattr(server, "ExecutionPlanner", FailedPlanner)
    monkeypatch.setattr(server, "rag_qa", KnowledgeFallbackQA())
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "2026-09-24 A股复盘", "scope_mode": "whole_corpus", "use_mcp": False,
    }))

    done = _event(events, "done")
    assert "未取得可核验行情数据" in done["answer"]
    assert done["run"]["status"] == "partial"
    assert source_calls == []


def test_market_repair_replays_live_sse_routes_and_one_shot_clarification_offline(
    client, env, monkeypatch, tmp_path, caplog,
):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from financial_report_fetcher.rag.config import RagConfig
    from financial_report_fetcher.rag.qa import RagQA
    from webapp.chat_store import ChatStore
    from webapp.execution_planner import PlanningResult, safe_market_plan
    from webapp import chat_request_resolution
    from webapp import chat_time

    store = ChatStore(str(tmp_path / "market-sse-replay.json"))
    monkeypatch.setattr(server, "chat_store", store)
    monkeypatch.setattr(server.RagConfig, "load", classmethod(lambda _cls: RagConfig(
        mcp_tools=False, web_search=False, mcp_max_tool_calls=9,
    )))
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: None)
    monkeypatch.setattr(server, "_build_chat_tool_executor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_mcp_tool_defs_cache", [])
    monkeypatch.setattr(server, "mcp_breaker", type("Breaker", (), {"allow": lambda self: False})())
    fixed_now = datetime(2026, 10, 16, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(chat_request_resolution, "resolve_market_window",
                        lambda question, _now=None: chat_time.resolve_market_window(question, fixed_now))
    monkeypatch.setattr(server, "_resolve_scope", lambda body, run_id="", *, requires_company=False, question=None:
        server.ScopeResolution(server.Scope.company_only("600660", "福耀玻璃")
                               if requires_company else server.Scope.whole_corpus()))
    market_calls, pdf_calls, mcp_calls = [], [], []
    dates = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]
    def fake_kline(symbol, *, period, count, adjust):
        market_calls.append((symbol, period, count, adjust))
        return [{"date": day, "close": 10.0 + index} for index, day in enumerate(dates)]
    monkeypatch.setattr(server.tencent_quote, "kline", fake_kline)
    monkeypatch.setattr(server.market_data_mcp, "call_tool", lambda *args, **kwargs: mcp_calls.append(args))

    class FixedPlanner:
        def __init__(self, *_args): pass
        def plan(self, question, scope, capabilities, *, authoritative_intent=None, **_kwargs):
            plan = safe_market_plan(authoritative_intent, capabilities.available_kinds,
                                    capabilities.max_external_calls)
            return PlanningResult("validated" if plan else "unavailable", plan)

    class NoStore:
        def query(self, *_args, **_kwargs):
            pdf_calls.append("query")
            raise AssertionError("market request must never retrieve financial PDFs")
    class AnswerAI:
        def chat_stream(self, *, messages, **_kwargs):
            question = str(messages[-1].get("content", ""))
            is_company_market = "福耀玻璃" in question
            answer = ("福耀玻璃近期行情" if is_company_market else "仅依据取得的行情来源作答。")
            yield {"type": "done", "answer": answer, "finish_reason": "length" if is_company_market else "stop",
                   "model": "fixture", "usage": {}}

    monkeypatch.setattr(server, "ExecutionPlanner", FixedPlanner)
    monkeypatch.setattr(server, "rag_qa", RagQA(NoStore(), AnswerAI()))

    cases = [
        ("分析今日股票行情", "market_recap", "market_recap"),
        ("分析福耀玻璃近期行情", "market_trend", "external_market"),
        ("分析福耀玻璃近5日行情", "market_trend", "external_market"),
    ]
    session_id = None
    replay_runs = []
    for question, intent, source_mode in cases:
        events = _read_sse(client.post("/api/chat/stream", json={
            "question": question, "session_id": session_id, "use_mcp": False,
        }))
        session_id = session_id or _event(events, "session")["session_id"]
        assert _event(events, "execution_plan")["source_mode"] == source_mode
        replay_run = _event(events, "done")["run"]
        assert replay_run["tool_artifacts"]
        assert all(item["tool_name"] == "kline" for item in replay_run["tool_artifacts"])
        if intent == "market_trend":
            assert replay_run["verification_report"]["status"] == "passed"
            assert "999" not in replay_run["content"]
            assert "10元/股" in replay_run["content"]
            assert "行情数据覆盖有限" not in replay_run["content"]
        replay_runs.append(replay_run)

    orphan = _read_sse(client.post("/api/chat/stream", json={
        "question": "近五交易日", "session_id": session_id, "use_mcp": False,
    }))
    assert "clarification" in [name for name, _ in orphan]
    assert _event(orphan, "done")["run"] is None
    assert "execution_plan" not in [name for name, _ in orphan]

    first_half = _read_sse(client.post("/api/chat/stream", json={
        "question": "福耀玻璃股价走势", "use_mcp": False,
    }))
    clarification_sid = _event(first_half, "session")["session_id"]
    assert _event(first_half, "done")["run"] is None
    assert "clarification" in [name for name, _ in first_half]
    completed = _read_sse(client.post("/api/chat/stream", json={
        "question": "近五交易日", "session_id": clarification_sid, "use_mcp": False,
    }))
    completed_run = _event(completed, "done")["run"]

    assert completed_run["status"] == "completed"
    assert completed_run["scope"]["companies"][0]["code"] == "600660"
    assert completed_run["execution_plan"]["source_mode"] == "external_market"
    assert completed_run["retrieval_report_ids"] == []
    assert len(market_calls) == 7
    assert pdf_calls == [] and mcp_calls == []
    assert caplog.text.count("verification=passed") >= 3
    assert all(run["execution_plan"]["source_mode"] in {"market_recap", "external_market"}
               for run in replay_runs)


def test_market_source_failure_persists_failed_run_without_invoking_financial_rag(client, env, monkeypatch, tmp_path):
    from financial_report_fetcher.rag.config import RagConfig
    from webapp.chat_store import ChatStore
    from webapp.execution_plan import ExecutionPlan
    from webapp.execution_planner import PlanningResult

    monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / "failed-market.json")))
    monkeypatch.setattr(server.RagConfig, "load", classmethod(lambda _cls: RagConfig(
        mcp_tools=False, web_search=False, mcp_max_tool_calls=3,
    )))
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: None)
    monkeypatch.setattr(server, "_resolve_scope", lambda body, run_id="", *, requires_company=False, question=None:
        server.ScopeResolution(server.Scope.company_only("600660", "福耀玻璃")))
    monkeypatch.setattr(server.tencent_quote, "kline", lambda *_args, **_kwargs: [])

    class FixedPlanner:
        def __init__(self, *_args): pass
        def plan(self, *_args, **_kwargs):
            return PlanningResult("validated", ExecutionPlan.from_dict({
                "objective": "个股近五日行情", "source_mode": "external_market",
                "steps": [{"id": "kline", "kind": "market_kline", "required": True},
                          {"id": "answer", "kind": "answer", "depends_on": ["kline"]}],
                "acceptance": ["匹配主体和窗口的带日期行情"],
            }))

    class KnowledgeFallbackQA:
        def answer_from_context(self, question, **kwargs):
            assert kwargs["knowledge_mode"] is True
            yield {"type": "done", "answer": "未取得行情数据；可说明一般走势分析方法，不代表当前结论。",
                   "citations": [], "model": "fixture", "usage": {}}
        def answer_stream(self, *_args, **_kwargs):
            raise AssertionError("market source failure must not fall back to report retrieval")

    monkeypatch.setattr(server, "ExecutionPlanner", FixedPlanner)
    monkeypatch.setattr(server, "rag_qa", KnowledgeFallbackQA())
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "600660近五交易日行情", "use_mcp": False,
    }))

    run = _event(events, "done")["run"]
    assert run["status"] == "partial"
    assert run["retrieval_report_ids"] == []
    assert "未取得行情数据" in run["content"]


def test_recap_coverage_marks_fixed_provider_window_mismatch_and_single_day_pool():
    from datetime import date

    from webapp.chat_time import MarketWindow
    from webapp.source_runtime import SourceCoverage, SourceResult

    window = MarketWindow("上周", "calendar_week", date(2026, 9, 28), date(2026, 10, 4), None)
    fund = SourceResult("f", "mcp", "stock_sector_fund_flow_rank", "market", "partial",
                        coverage=SourceCoverage(query_window="5日", data_window="2026-10-05"))
    pool = SourceResult("p", "mcp", "stock_zt_pool", "market", "partial",
                        coverage=SourceCoverage(returned_rows=50, limit=50, data_window="2026-10-02"))
    outside = SourceResult("p2", "mcp", "stock_zt_pool", "market", "partial",
                           coverage=SourceCoverage(returned_rows=5, limit=50, data_window="2026-10-05"))

    fund_coverage = server._recap_coverage_for_window(fund, window).coverage
    pool_coverage = server._recap_coverage_for_window(pool, window).coverage
    outside_coverage = server._recap_coverage_for_window(outside, window).coverage

    assert "与请求窗口上周不匹配" in fund_coverage.query_window
    assert "仅2026-10-02单日补充" in pool_coverage.query_window
    assert "与请求窗口上周不匹配" in outside_coverage.query_window

    today = MarketWindow("今日", "explicit", date(2026, 10, 5), date(2026, 10, 5), None)
    stale_fund = SourceResult("f2", "mcp", "stock_sector_fund_flow_rank", "market", "partial",
                              coverage=SourceCoverage(query_window="今日", data_window="2026-10-04"))
    stale_coverage = server._recap_coverage_for_window(stale_fund, today).coverage
    assert "与请求窗口今日不匹配" in stale_coverage.query_window


def test_task_manager_startup_waits_for_inflight_shutdown(monkeypatch):
    shutdown_entered = threading.Event()
    release_shutdown = threading.Event()
    startup_done = threading.Event()

    class BlockingManager:
        def shutdown(self):
            shutdown_entered.set()
            release_shutdown.wait(timeout=3.0)

    replacement = object()
    monkeypatch.setattr(server, "task_manager", BlockingManager())
    monkeypatch.setattr(server, "TaskManager", lambda: replacement)

    shutting_down = threading.Thread(target=server._shutdown_task_manager)
    shutting_down.start()
    assert shutdown_entered.wait(timeout=1.0)

    def startup():
        server._startup_task_manager()
        startup_done.set()

    starting_up = threading.Thread(target=startup)
    starting_up.start()
    try:
        assert not startup_done.wait(timeout=0.15)
    finally:
        release_shutdown.set()

    shutting_down.join(timeout=2.0)
    starting_up.join(timeout=2.0)
    assert not shutting_down.is_alive()
    assert not starting_up.is_alive()
    assert server.task_manager is replacement


class TestAutocomplete:
    def test_search(self, client):
        r = client.get("/api/companies", params={"q": "长江"})
        assert r.status_code == 200
        assert r.json()["results"] == [{"code": "600900", "name": "长江电力"}]


class TestHealth:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["ai_key_configured"] is True
        assert data["index_ready"] is True


class TestReportsList:
    def test_list_reports_desc_order_and_downloaded_flag(self, client):
        r = client.get(
            "/api/companies/600900/reports",
            params={"start": "2025-01-01", "end": "2025-12-31"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "长江电力"
        # 按报告期降序
        assert [x["period"] for x in body["reports"]] == ["2025-12-31", "2025-03-31"]
        assert body["reports"][0]["type"] == "annual"
        # 本地 reports/ 无此文件 → downloaded=False
        assert all(x["downloaded"] is False for x in body["reports"])

    def test_unknown_code_404(self, client):
        r = client.get(
            "/api/companies/999999/reports",
            params={"start": "2025-01-01", "end": "2025-12-31"},
        )
        assert r.status_code == 404

    def test_bad_date_range_400(self, client):
        r = client.get(
            "/api/companies/600900/reports",
            params={"start": "2026-01-01", "end": "2025-12-31"},
        )
        assert r.status_code == 400


class TestServePdf:
    def test_serve_pdf(self, client, tmp_path, monkeypatch):
        pdf = tmp_path / "fake.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake pdf")
        # 跳过真实下载/文件检查，直接返回构造的 PDF
        monkeypatch.setattr(server, "_ensure_pdf", lambda meta: str(pdf))
        r = client.get("/api/reports/600900/2025-12-31.pdf")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content == b"%PDF-1.4 fake pdf"


class TestAnalyze:
    def _poll_until_done(self, client, task_id):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            task = client.get(f"/api/tasks/{task_id}").json()
            if task["status"] in ("done", "failed"):
                return task
            time.sleep(0.03)
        return {"status": "timeout"}

    def test_analyze_submits_task_and_polls(self, client, env):
        r = client.post(
            "/api/reports/600900/2025-12-31/analyze",
            json={"dimensions": ["financial_summary"]},
        )
        assert r.status_code == 200
        task_id = r.json()["task_id"]
        assert r.json()["dimensions"] == ["financial_summary"]

        task = self._poll_until_done(client, task_id)
        assert task["status"] == "done"
        result = task["result"]
        assert result["schema_version"] == 3
        assert result["quick"]["conclusions"]
        assert result["sections"][0]["section_id"] == "financial-overview"

    def test_reanalyze_retires_legacy_year_form_analysis_products(
        self, client, env, monkeypatch, tmp_path
    ):
        """重新分析同一报告期后，旧年份命名的分析产物必须被替换，避免历史出现两份。"""
        analysis_dir = tmp_path / "analysis"
        analysis_dir.mkdir()
        monkeypatch.setattr(server, "ANALYSIS_DIR", str(analysis_dir))
        legacy_json = analysis_dir / "长江电力_600900_2025_分析报告.json"
        legacy_md = analysis_dir / "长江电力_600900_2025_分析报告.md"
        legacy_json.write_text(
            json.dumps(
                {"meta": {"company": "长江电力", "period": "2025-12-31"}, "dimensions": []},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        legacy_md.write_text("# 旧分析", encoding="utf-8")

        # 替身必须像真实流水线一样先落盘新产物，再返回文档，否则清理顺序错误也会假绿
        def analyze_and_save(request, emit, stop_event):
            stem = analysis_output_stem(request.analysis_id)
            (analysis_dir / f"{stem}.json").write_text(
                json.dumps(
                    {"meta": {"company": "长江电力", "period": "2025-12-31"}, "dimensions": []},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (analysis_dir / f"{stem}.md").write_text("# 新分析", encoding="utf-8")
            return _FakeV3Document()

        env["fake_pipeline"].side_effect = analyze_and_save

        # 清理必须发生在新产物落盘之后：调用清理时新 JSON/MD 应已存在，旧产物尚未被删
        real_retire = server.retire_superseded_analysis_files
        observed = {}

        def spy_retire(target_dir, *, code, period, keep_filename):
            stem = keep_filename[: -len(".json")]
            observed["new_json"] = (analysis_dir / f"{stem}.json").is_file()
            observed["new_md"] = (analysis_dir / f"{stem}.md").is_file()
            observed["legacy_alive"] = legacy_json.is_file()
            return real_retire(
                target_dir, code=code, period=period, keep_filename=keep_filename
            )

        monkeypatch.setattr(server, "retire_superseded_analysis_files", spy_retire)

        r = client.post(
            "/api/reports/600900/2025-12-31/analyze",
            json={"dimensions": ["financial_summary"]},
        )
        task = self._poll_until_done(client, r.json()["task_id"])

        assert task["status"] == "done"
        assert observed == {"new_json": True, "new_md": True, "legacy_alive": True}
        assert (analysis_dir / "长江电力_600900_2025-12-31_分析报告.json").exists()
        assert not legacy_json.exists()
        assert not legacy_md.exists()

    def test_cancelled_analysis_keeps_legacy_year_form_products(
        self, client, env, monkeypatch, tmp_path
    ):
        """分析被取消时不得清理同报告期旧产物，否则用户会丢失仍可读的旧报告。"""
        analysis_dir = tmp_path / "analysis"
        analysis_dir.mkdir()
        monkeypatch.setattr(server, "ANALYSIS_DIR", str(analysis_dir))
        legacy_json = analysis_dir / "长江电力_600900_2025_分析报告.json"
        legacy_md = analysis_dir / "长江电力_600900_2025_分析报告.md"
        legacy_json.write_text(
            json.dumps(
                {"meta": {"company": "长江电力", "period": "2025-12-31"}, "dimensions": []},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        legacy_md.write_text("# 旧分析", encoding="utf-8")

        def analyse_then_cancel(request, emit, stop_event):
            document = _FakeV3Document(stage="cancelled")
            emit("job.cancelled", {"analysis": document.to_dict()})
            return document

        env["fake_pipeline"].side_effect = analyse_then_cancel

        r = client.post(
            "/api/reports/600900/2025-12-31/analyze",
            json={"dimensions": ["financial_summary"]},
        )
        task = self._poll_until_done(client, r.json()["task_id"])

        assert task["status"] in ("cancelled", "done")
        assert legacy_json.exists()
        assert legacy_md.exists()

    def test_legacy_cleanup_failure_does_not_fail_completed_analysis(
        self, client, env, monkeypatch, tmp_path
    ):
        """旧产物清理是尽力而为：失败不得把已成功保存的分析任务变成 failed。"""
        analysis_dir = tmp_path / "analysis"
        analysis_dir.mkdir()
        monkeypatch.setattr(server, "ANALYSIS_DIR", str(analysis_dir))

        def boom(*args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(server, "retire_superseded_analysis_files", boom)

        r = client.post(
            "/api/reports/600900/2025-12-31/analyze",
            json={"dimensions": ["financial_summary"]},
        )
        task = self._poll_until_done(client, r.json()["task_id"])

        assert task["status"] == "done"

    def test_analyze_returns_json_503_when_report_source_times_out(self, client, env):
        """上游财报源超时应转为可供前端消费的 HTTP 异常。"""
        env["fake_ds"].fetch_reports.side_effect = requests.exceptions.ReadTimeout(
            "cninfo timeout"
        )

        with pytest.raises(HTTPException) as exc_info:
            server._find_report_meta("600900", date(2025, 12, 31))

        assert exc_info.value.status_code == 503
        assert "暂时不可用" in exc_info.value.detail

    def test_analyze_task_exposes_dimension_progress_while_running(self, client, env):
        """分析尚未完成时，任务接口应返回当前维度对应的持久进度。"""
        reported = threading.Event()
        release = threading.Event()
        def progressive_analyze(request, emit, stop_event):
            emit("job.stage_changed", {"stage": "deep_processing"})
            reported.set()
            release.wait(timeout=5.0)
            return _FakeV3Document()

        env["fake_pipeline"].side_effect = progressive_analyze
        try:
            response = client.post(
                "/api/reports/600900/2025-12-31/analyze",
                json={"dimensions": ["financial_summary", "risk_warning"]},
            )
            task_id = response.json()["task_id"]
            assert reported.wait(timeout=3.0)

            task = client.get(f"/api/tasks/{task_id}").json()
            assert task["status"] == "running"
            assert task["result"]["stage"] == "deep_processing"
        finally:
            release.set()

    def test_analyze_quarters_pass_exact_period_to_analysis(self, client, env):
        """Web 分析 Q1/Q3 时必须传递精确期次，供保存与 RAG 关联使用。"""
        env["fake_ds"].fetch_reports.return_value = [
            _meta("600900", ReportType.QUARTERLY, date(2025, 3, 31), "长江电力"),
            _meta("600900", ReportType.QUARTERLY, date(2025, 9, 30), "长江电力"),
        ]
        for period in ("2025-03-31", "2025-09-30"):
            response = client.post(
                f"/api/reports/600900/{period}/analyze",
                json={"dimensions": ["financial_summary"]},
            )
            assert response.status_code == 200
            assert self._poll_until_done(client, response.json()["task_id"])["status"] == "done"

        periods = [request.period for request in env["fake_pipeline"].requests]
        assert periods == ["2025-03-31", "2025-09-30"]

    def test_analyze_queued_when_workers_full(self, client, env, monkeypatch):
        """并发化：worker 满时新分析任务排队（200）而非拒绝（409）"""
        gate = threading.Event()

        def blocking():
            gate.wait(timeout=10)
            return "released"

        # 单 worker 池：先占住唯一 worker
        tm = env["make_task_manager"](max_workers=1)
        monkeypatch.setattr(server, "task_manager", tm)
        tid = tm.submit(blocking)
        assert tid is not None
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if tm.get(tid)["status"] == "running":
                break
            time.sleep(0.01)
        else:
            pytest.fail("阻塞任务未能进入 running 状态")

        r = client.post(
            "/api/reports/600900/2025-12-31/analyze",
            json={"dimensions": []},
        )
        assert r.status_code == 200
        task_id = r.json()["task_id"]
        assert tm.get(task_id)["status"] == "pending"  # 排队中

        gate.set()  # 放行阻塞任务
        task = self._poll_until_done(client, task_id)
        assert task["status"] == "done"

    def test_analyze_requires_ai_key(self, client, env):
        env["fake_ai"].api_key = ""
        r = client.post(
            "/api/reports/600900/2025-12-31/analyze",
            json={"dimensions": []},
        )
        assert r.status_code == 400
        assert "AI_API_KEY" in r.json()["detail"]


class TestProgressiveAnalyzeApi:
    class FakeDocument:
        def to_dict(self):
            return {"schema_version": 3, "stage": "completed", "quick": {"conclusions": []}}

    class FakePipeline:
        def __init__(self):
            self.requests = []

        def run(self, request, emit, stop_event):
            self.requests.append(request)
            emit("quick.ready", {"quick": {"conclusions": []}})
            emit("job.completed", {"analysis": self.FakeDocument().to_dict()})
            return self.FakeDocument()

    def test_interests_endpoint_exposes_supported_priorities(self, client):
        response = client.get("/api/analysis/interests")

        assert response.status_code == 200
        body = response.json()
        assert {item["id"] for item in body["interests"]} >= {"cash_flow", "risks"}
        assert all({"id", "name", "description", "default"} <= set(item) for item in body["interests"])

    def test_analyze_accepts_interests_and_returns_recovery_urls(
        self, client, monkeypatch
    ):
        pipeline = self.FakePipeline()
        pipeline.FakeDocument = self.FakeDocument
        monkeypatch.setattr(server, "progressive_pipeline", pipeline, raising=False)

        response = client.post(
            "/api/reports/600900/2025-12-31/analyze",
            json={"interests": ["cash_flow", "risks", "cash_flow", "unknown"]},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["interests"] == ["cash_flow", "risks"]
        assert body["status_url"] == f"/api/tasks/{body['task_id']}"
        assert body["event_url"] == f"/api/analysis/tasks/{body['task_id']}/events"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not pipeline.requests:
            time.sleep(0.01)
        assert pipeline.requests[0].interests == ("cash_flow", "risks")

    def test_legacy_dimensions_are_mapped_to_interests(self, client, monkeypatch):
        pipeline = self.FakePipeline()
        pipeline.FakeDocument = self.FakeDocument
        monkeypatch.setattr(server, "progressive_pipeline", pipeline, raising=False)

        response = client.post(
            "/api/reports/600900/2025-12-31/analyze",
            json={"dimensions": ["cashflow", "risk_warning"]},
        )

        assert response.status_code == 200
        assert response.json()["interests"] == ["cash_flow", "risks"]

    def test_sse_replays_only_events_after_last_event_id(self, client):
        store = server.task_manager._store
        store.create("replay-task")
        first = store.append_event("replay-task", "quick.ready", {"quick": {}})
        second = store.append_event(
            "replay-task", "section.ready", {"section": {"section_id": "cash"}}
        )
        store.update("replay-task", status="done")

        response = client.get(
            "/api/analysis/tasks/replay-task/events?after=0",
            headers={"Last-Event-ID": str(first.id)},
        )

        assert response.status_code == 200
        assert f"id: {second.id}" in response.text
        assert f"id: {first.id}" not in response.text
        assert "event: section.ready" in response.text


class TestChat:
    def test_chat_answer_and_history(self, client, env, monkeypatch):
        # 隔离真实 RAG 库：本测试验证传统问答路径的 history 传递，
        # 本地 config.yaml 启用 RAG 且库已索引时会优先走 RAG，故置空 rag_qa
        monkeypatch.setattr(server, "rag_qa", None)
        r1 = client.post(
            "/api/reports/600900/2025-12-31/chat",
            json={"question": "现金流如何？"},
        )
        assert r1.status_code == 200
        assert r1.json()["answer"] == "测试 AI 回答"
        assert r1.json()["elapsed_seconds"] >= 0
        # 第二次提问时 qa() 收到的 history 应含第一轮两条消息
        client.post(
            "/api/reports/600900/2025-12-31/chat",
            json={"question": "再细说下"},
        )
        history_arg = env["fake_analyzer"].qa.call_args.kwargs["history"]
        assert len(history_arg) == 2

    def test_report_chat_rag_fallback_log_is_redacted_and_report_correlated(self, client, env, monkeypatch, caplog):
        """旧单报告问答的 RAG 回退日志可按报告定位，且不泄露用户问题。"""
        secret_question = "仅用于旧问答日志泄露测试的用户问题"

        class BrokenRagQA:
            def try_answer_report(self, *args, **kwargs):
                raise RuntimeError(secret_question)

        monkeypatch.setattr(server, "rag_qa", BrokenRagQA())
        caplog.set_level(logging.INFO, logger=server.__name__)
        response = client.post(
            "/api/reports/600900/2025-12-31/chat",
            json={"question": secret_question},
        )

        assert response.status_code == 200
        assert response.json()["answer"] == "测试 AI 回答"
        assert secret_question not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "chat_rag_fallback" in message
            and "report_code=600900" in message
            and "report_period=2025-12-31" in message
            and "RuntimeError" in message
            for message in messages
        )

    def test_chat_requires_ai_key(self, client, env):
        env["fake_ai"].api_key = ""
        r = client.post(
            "/api/reports/600900/2025-12-31/chat",
            json={"question": "x"},
        )
        assert r.status_code == 400
        assert "AI_API_KEY" in r.json()["detail"]

    def test_chat_empty_question_400(self, client):
        r = client.post(
            "/api/reports/600900/2025-12-31/chat",
            json={"question": "   "},
        )
        assert r.status_code == 400


class TestHistoryApi:
    def test_history_empty(self, client, tmp_path, monkeypatch):
        """reports/ 与 reports/analysis/ 不存在 → 200 空列表"""
        monkeypatch.setattr(server, "REPORTS_DIR", str(tmp_path / "nope"))
        monkeypatch.setattr(server, "ANALYSIS_DIR", str(tmp_path / "nope" / "analysis"))
        r = client.get("/api/history")
        assert r.status_code == 200
        assert r.json() == {"items": []}

    def test_history_flat_list(self, client, tmp_path, monkeypatch):
        """构造 Web 格式分析 JSON + PDF → 扁平列表正确聚合"""
        a_dir = tmp_path / "analysis"
        a_dir.mkdir(parents=True)
        (a_dir / "长江电力_600900_2025_分析报告.json").write_text(json.dumps({
            "meta": {"company": "长江电力（600900）", "period": "2025-12-31"},
            "dimensions": [{"id": "financial_summary", "name": "财务摘要",
                            "content": "营收增长", "error": None}],
            "metrics": [{"year": 2025, "revenue": 853.6}],
        }, ensure_ascii=False), encoding="utf-8")
        r_dir = tmp_path / "reports"
        r_dir.mkdir(parents=True)
        (r_dir / "长江电力_600900_年报_2025.pdf").write_bytes(b"%PDF")
        monkeypatch.setattr(server, "REPORTS_DIR", str(r_dir))
        monkeypatch.setattr(server, "ANALYSIS_DIR", str(a_dir))
        r = client.get("/api/history")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) >= 1
        cj = next(i for i in items if i["code"] == "600900")
        assert cj["has_analysis"] is True
        assert cj["period"] == "2025-12-31"
        assert cj["pdf_filename"] == "长江电力_600900_年报_2025.pdf"

    def test_history_api_lists_q1_and_q3_separately(self, client, tmp_path, monkeypatch):
        """历史 API 不得忽略完整期次的季度 PDF，也不得将 Q3 合并到 Q1。"""
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        for period in ("2025-03-31", "2025-09-30"):
            (reports_dir / f"长江电力_600900_季报_{period}.pdf").write_bytes(b"%PDF")
        monkeypatch.setattr(server, "REPORTS_DIR", str(reports_dir))
        monkeypatch.setattr(server, "ANALYSIS_DIR", str(tmp_path / "analysis"))

        response = client.get("/api/history")
        assert response.status_code == 200
        quarterly = [item for item in response.json()["items"] if item["type"] == "季报"]
        assert [item["period"] for item in quarterly] == ["2025-03-31", "2025-09-30"]

    def test_history_detail(self, client, tmp_path, monkeypatch):
        """GET /api/history/{filename} 返回分析报告完整内容"""
        a_dir = tmp_path / "analysis"
        a_dir.mkdir(parents=True)
        fname = "长江电力_600900_2024_分析报告.json"
        (a_dir / fname).write_text(json.dumps({
            "meta": {"company": "长江电力（600900）", "period": "2024-12-31"},
            "dimensions": [{"id": "financial_summary", "name": "财务摘要",
                            "content": "测试内容", "error": None}],
        }, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(server, "ANALYSIS_DIR", str(a_dir))
        r = client.get(f"/api/history/{fname}")
        assert r.status_code == 200
        assert r.json()["dimensions"][0]["content"] == "测试内容"

    def test_history_detail_cleans_missing_rows_and_hides_empty_dimensions(
        self, client, tmp_path, monkeypatch
    ):
        """旧分析文件读取时也应精简无披露内容，无需迁移磁盘文件。"""
        a_dir = tmp_path / "analysis"
        a_dir.mkdir(parents=True)
        fname = "长江电力_600900_2025_分析报告.json"
        (a_dir / fname).write_text(json.dumps({
            "meta": {"company": "长江电力（600900）", "period": "2025-12-31"},
            "dimensions": [
                {"id": "empty", "name": "空维度", "content": "数据未披露", "error": None},
                {"id": "summary", "name": "财务摘要", "error": None,
                 "content": "| 指标 | 数值 | 同比 |\n|---|---|---|\n"
                            "| 营业收入 | 未披露 | 暂无数据 |\n"
                            "| 总资产 | 100亿元 | 未披露 |"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(server, "ANALYSIS_DIR", str(a_dir))

        response = client.get(f"/api/history/{fname}")

        assert response.status_code == 200
        dimensions = response.json()["dimensions"]
        assert [item["id"] for item in dimensions] == ["summary"]
        assert "营业收入" not in dimensions[0]["content"]
        assert "总资产" in dimensions[0]["content"]

    def test_history_detail_404(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "ANALYSIS_DIR", str(tmp_path))
        r = client.get("/api/history/不存在的文件.json")
        assert r.status_code == 404

    def test_delete_history_analysis_removes_only_analysis_artifacts(
        self, client, tmp_path, monkeypatch
    ):
        """删除分析只移除同名 JSON/Markdown，不能触及原始 PDF。"""
        analysis_dir = tmp_path / "analysis"
        reports_dir = tmp_path / "reports"
        analysis_dir.mkdir()
        reports_dir.mkdir()
        filename = "长江电力_600900_2025_分析报告.json"
        (analysis_dir / filename).write_text("{}", encoding="utf-8")
        (analysis_dir / filename.replace(".json", ".md")).write_text("# 分析", encoding="utf-8")
        pdf = reports_dir / "长江电力_600900_年报_2025.pdf"
        pdf.write_bytes(b"%PDF")
        monkeypatch.setattr(server, "ANALYSIS_DIR", str(analysis_dir))
        monkeypatch.setattr(server, "REPORTS_DIR", str(reports_dir))

        response = client.delete(f"/api/history/{filename}")

        assert response.status_code == 200
        assert response.json() == {"deleted": filename}
        assert not (analysis_dir / filename).exists()
        assert not (analysis_dir / filename.replace(".json", ".md")).exists()
        assert pdf.exists()

    def test_history_pdf_serves_local_file_inline(self, client, tmp_path, monkeypatch):
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        filename = "长江电力_600900_年报_2025.pdf"
        content = b"%PDF-1.4 local history pdf"
        (reports_dir / filename).write_bytes(content)
        monkeypatch.setattr(server, "REPORTS_DIR", str(reports_dir))

        response = client.get(f"/api/history-pdf/{filename}")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.content == content

    def test_history_pdf_rejects_non_pdf(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "REPORTS_DIR", str(tmp_path))
        response = client.get("/api/history-pdf/not-a-pdf.txt")
        assert response.status_code == 404

    def test_list_reports_analyzed_flag(self, client, tmp_path, monkeypatch):
        """精确 period（年报）→ analyzed=True；无匹配（季报）→ False"""
        a_dir = tmp_path / "analysis"
        a_dir.mkdir(parents=True)
        (a_dir / "长江电力_600900_2025_分析报告.json").write_text(json.dumps({
            "meta": {"company": "长江电力（600900）", "period": "2025-12-31"},
            "dimensions": [], "metrics": None,
        }, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(server, "ANALYSIS_DIR", str(a_dir))
        r = client.get("/api/companies/600900/reports",
                       params={"start": "2025-01-01", "end": "2025-12-31"})
        reports = r.json()["reports"]
        annual = next(x for x in reports if x["type"] == "annual")
        quarterly = next(x for x in reports if x["type"] == "quarterly")
        assert annual["analyzed"] is True
        assert quarterly["analyzed"] is False

class FakeRagQA:
    """测试用 RAG QA：可配置命中/未命中"""

    def __init__(self, result=None):
        self._result = result
        self.calls = []

    def try_answer_report(self, code, period_iso, question, history=None):
        self.calls.append((code, period_iso, question))
        if self._result is None:
            return None
        return {"answer": self._result, "citations": [{"report_id": "600900:2025-12-31:annual"}]}

    def answer(self, question, history=None, filters=None):
        if self._result is None:
            return None
        return {"answer": self._result, "citations": []}


class TestRagApi:
    def test_global_chat_uses_rag(self, client, env, monkeypatch):
        fake = FakeRagQA(result="RAG 回答")
        monkeypatch.setattr(server, "rag_qa", fake)
        r = client.post("/api/chat", json={"question": "长江电力营收？"})
        assert r.status_code == 200
        assert r.json()["answer"] == "RAG 回答"
        assert r.json()["elapsed_seconds"] >= 0

    def test_global_chat_requires_ai_key(self, client, env):
        env["fake_ai"].api_key = ""
        r = client.post("/api/chat", json={"question": "x"})
        assert r.status_code == 400
        assert "AI_API_KEY" in r.json()["detail"]

    def test_global_chat_rag_not_ready_503(self, client, env, monkeypatch):
        monkeypatch.setattr(server, "rag_qa", None)
        r = client.post("/api/chat", json={"question": "x"})
        assert r.status_code == 503

    def test_rag_status_disabled(self, client, monkeypatch):
        # /api/rag/status 以 rag_service 是否就绪为准；本地 config.yaml 启用 RAG 时
        # 模块加载会初始化全局组件，这里统一置空模拟「未启用」状态
        monkeypatch.setattr(server, "rag_store", None)
        monkeypatch.setattr(server, "rag_service", None)
        monkeypatch.setattr(server, "rag_qa", None)
        r = client.get("/api/rag/status")
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    def test_analyze_auto_ingests_after_save(self, client, env, monkeypatch):
        """分析任务成功后自动摄取（auto_ingest_report 钩子被调用）"""
        calls = []
        monkeypatch.setattr(server, "rag_service",
                            type("Fake", (), {"auto_ingest_report": lambda self, p: calls.append(p)})())
        r = client.post("/api/reports/600900/2025-12-31/analyze", json={"dimensions": []})
        tid = r.json()["task_id"]
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if server.task_manager.get(tid)["status"] == "done":
                break
            time.sleep(0.01)
        assert calls, "分析完成后应触发自动摄取"

    def test_rag_ingest_submits_task(self, client, env, monkeypatch):
        class FakeSvc:
            def ingest_all(self, force=False):
                return None

        monkeypatch.setattr(server, "rag_service", FakeSvc())
        r = client.post("/api/rag/ingest")
        assert r.status_code == 200
        assert "task_id" in r.json()

    def test_rag_ingest_result_is_json_and_survives_task_manager_restart(
        self, client, monkeypatch
    ):
        class FakeSvc:
            def ingest_all(self, force=False):
                return IngestResult(
                    ingested=2,
                    skipped=1,
                    total_chunks=17,
                    errors=["旧季报身份不明确"],
                )

        monkeypatch.setattr(server, "rag_service", FakeSvc())
        response = client.post("/api/rag/ingest")
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            task_response = client.get(f"/api/tasks/{task_id}")
            task = task_response.json()
            if task["status"] in ("done", "failed"):
                break
            time.sleep(0.01)

        assert task_response.status_code == 200
        assert task == {
            "status": "done",
            "progress": None,
            "result": {
                "ingested": 2,
                "skipped": 1,
                "total_chunks": 17,
                "errors": ["旧季报身份不明确"],
            },
            "error": None,
        }
        json.dumps(task, allow_nan=False)

        db_path = server.task_manager._db_path
        server.task_manager.shutdown()
        restarted = server.TaskManager(db_path=db_path)
        monkeypatch.setattr(server, "task_manager", restarted)

        restarted_response = client.get(f"/api/tasks/{task_id}")
        assert restarted_response.status_code == 200
        assert restarted_response.json()["result"] == task["result"]


class TestRagFilesApi:
    def test_rag_files_lists_entries(self, client, monkeypatch):
        class FakeSvc:
            def list_files(self):
                return [
                    {"report_id": "600900:2025-12-31:annual", "source": "pdf",
                     "type": "annual", "type_label": "年报", "company": "长江电力",
                     "code": "600900", "year": 2025,
                     "filename": "长江电力_600900_年报_2025.pdf",
                     "added": True, "chunk_count": 12},
                    {"report_id": "600900:2025-12-31:annual", "source": "analysis",
                     "type": "annual", "type_label": "年报", "company": "长江电力",
                     "code": "600900", "year": 2025,
                     "filename": "长江电力_600900_2025_分析报告.json",
                     "added": False, "chunk_count": 0},
                ]

        monkeypatch.setattr(server, "rag_service", FakeSvc())
        r = client.get("/api/rag/files")
        assert r.status_code == 200
        data = r.json()
        assert data["enabled"] is True
        assert len(data["items"]) == 2
        assert data["stats"]["added"] == 1
        assert data["stats"]["not_added"] == 1

    def test_rag_files_disabled(self, client, monkeypatch):
        monkeypatch.setattr(server, "rag_service", None)
        r = client.get("/api/rag/files")
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert r.json()["items"] == []

    def test_rag_ingest_one_submits_task(self, client, env, monkeypatch):
        calls = []

        class FakeSvc:
            def ingest_file(self, report_id, source, file_path=None):
                calls.append((report_id, source))

        monkeypatch.setattr(server, "rag_service", FakeSvc())
        r = client.post("/api/rag/ingest/one",
                        json={"report_id": "600900:2025-12-31:annual", "source": "pdf"})
        assert r.status_code == 200
        assert "task_id" in r.json()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if calls:
                break
            time.sleep(0.01)
        assert calls == [("600900:2025-12-31:annual", "pdf")]

    def test_rag_delete_index(self, client, monkeypatch):
        deleted = []

        class FakeSvc:
            def delete_file_index(self, report_id, source):
                deleted.append((report_id, source))

        monkeypatch.setattr(server, "rag_service", FakeSvc())
        r = client.delete("/api/rag/index/600900:2025-12-31:annual/pdf")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert deleted == [("600900:2025-12-31:annual", "pdf")]


class TestChatRagBoost:
    def test_chat_prefers_rag_when_indexed(self, client, env, monkeypatch):
        """库已索引：走 RAG 且不调用 analyzer.qa（不下载 PDF）"""
        fake = FakeRagQA(result="RAG 答案")
        monkeypatch.setattr(server, "rag_qa", fake)
        r = client.post(
            "/api/reports/600900/2025-12-31/chat",
            json={"question": "现金流如何？"},
        )
        assert r.status_code == 200
        assert r.json()["answer"] == "RAG 答案"
        assert env["fake_analyzer"].qa.call_count == 0
        assert fake.calls == [("600900", "2025-12-31", "现金流如何？")]

    def test_chat_falls_back_when_not_indexed(self, client, env, monkeypatch):
        """库未命中：回退 analyzer.qa，响应保持兼容"""
        monkeypatch.setattr(server, "rag_qa", FakeRagQA(result=None))
        r = client.post(
            "/api/reports/600900/2025-12-31/chat",
            json={"question": "现金流如何？"},
        )
        assert r.status_code == 200
        assert r.json()["answer"] == "测试 AI 回答"
        assert env["fake_analyzer"].qa.call_count == 1


class TestAnalysisDimensionsApi:
    def test_analysis_dimensions_endpoint(self, client):
        """GET /api/analysis/dimensions 返回全部可勾选维度及默认标记"""
        r = client.get("/api/analysis/dimensions")
        assert r.status_code == 200
        body = r.json()
        dims = {d["id"]: d for d in body["dimensions"]}
        # 新维度已纳入
        assert "profit_quality" in dims
        assert "cashflow" in dims
        assert "governance" in dims
        # 默认 5 个维度标记
        defaults = set(body["defaults"])
        assert defaults == {
            "financial_summary", "risk_warning", "business_highlights",
            "profit_quality", "cashflow",
        }
        for dim_id in defaults:
            assert dims[dim_id]["default"] is True
        # custom 无 prompt，不应出现在可勾选清单
        assert "custom" not in dims

    def test_analyze_default_dimensions_from_config(self, client, env, monkeypatch):
        """旧配置不再决定固定 Tab；空请求使用新的默认关注方向。"""
        fake_cfg = MagicMock()
        fake_cfg.analysis_dimensions = ["financial_summary", "governance"]
        monkeypatch.setattr(server, "RagConfig", type("C", (), {"load": staticmethod(lambda: fake_cfg)}))
        r = client.post("/api/reports/600900/2025-12-31/analyze", json={"dimensions": []})
        assert r.status_code == 200
        tid = r.json()["task_id"]
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if server.task_manager.get(tid)["status"] == "done":
                break
            time.sleep(0.01)
        assert env["fake_pipeline"].requests[0].interests == (
            "financial_overview", "cash_flow", "risks"
        )

    def test_analyze_pre_ingests_before_analysis(self, client, env, monkeypatch):
        """新管线不让 RAG 前置摄取阻塞 quick，完成后再摄取分析产物。"""
        events = []
        fake_svc = type("Fake", (), {
            "auto_ingest_report": lambda self, p: events.append("ingest"),
        })()
        monkeypatch.setattr(server, "rag_service", fake_svc)

        def _analyze(request, emit, stop_event):
            events.append("analyze")
            return _FakeV3Document()

        env["fake_pipeline"].side_effect = _analyze
        r = client.post("/api/reports/600900/2025-12-31/analyze", json={"dimensions": ["financial_summary"]})
        tid = r.json()["task_id"]
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if server.task_manager.get(tid)["status"] == "done":
                break
            time.sleep(0.01)
        assert events == ["analyze", "ingest"]


class TestInitRagInjection:
    def test_init_rag_injects_rag_analysis_when_enabled(self, monkeypatch, tmp_path):
        """RAG 启用时 analyzer 重新构造并注入 RagAnalysis（按维度检索上下文）"""
        from types import SimpleNamespace

        fake_cfg = SimpleNamespace(
            enabled=True,
            store_path=str(tmp_path),
            chunk_size=800,
            chunk_overlap=100,
            top_k=8,
            embedding_model="fake-model",
            auto_ingest=True,
            enhanced_analysis=True,
        )
        monkeypatch.setattr(server, "RagConfig", type("C", (), {"load": staticmethod(lambda: fake_cfg)}))

        class FakeEmbedder:
            def __init__(self, *a, **kw):
                pass

        class FakeRagStore:
            def __init__(self, *a, **kw):
                pass

        class FakeSvc:
            def __init__(self, *a, **kw):
                pass

        class FakeQA:
            def __init__(self, *a, **kw):
                pass

        class FakeRagAnalysis:
            def __init__(self, *a, **kw):
                self.args = (a, kw)

        monkeypatch.setattr(server, "LocalEmbedder", FakeEmbedder)
        monkeypatch.setattr(server, "RagStore", FakeRagStore)
        monkeypatch.setattr(server, "IngestionService", FakeSvc)
        monkeypatch.setattr(server, "RagQA", FakeQA)
        monkeypatch.setattr(server, "RagAnalysis", FakeRagAnalysis)

        orig = (server.analyzer, server.rag_store, server.rag_service, server.rag_qa)
        try:
            server._init_rag()
            assert isinstance(server.analyzer.rag_analysis, FakeRagAnalysis)
        finally:
            server.analyzer, server.rag_store, server.rag_service, server.rag_qa = orig

    def test_init_rag_skips_injection_when_enhanced_analysis_disabled(self, monkeypatch, tmp_path):
        """enhanced_analysis=false 时即使 RAG 启用也不注入（保持现状行为）"""
        from types import SimpleNamespace

        fake_cfg = SimpleNamespace(
            enabled=True,
            store_path=str(tmp_path),
            chunk_size=800,
            chunk_overlap=100,
            top_k=8,
            embedding_model="fake-model",
            auto_ingest=True,
            enhanced_analysis=False,
        )
        monkeypatch.setattr(server, "RagConfig", type("C", (), {"load": staticmethod(lambda: fake_cfg)}))

        class FakeEmbedder:
            def __init__(self, *a, **kw):
                pass

        class FakeRagStore:
            def __init__(self, *a, **kw):
                pass

        class FakeSvc:
            def __init__(self, *a, **kw):
                pass

        class FakeQA:
            def __init__(self, *a, **kw):
                pass

        monkeypatch.setattr(server, "LocalEmbedder", FakeEmbedder)
        monkeypatch.setattr(server, "RagStore", FakeRagStore)
        monkeypatch.setattr(server, "IngestionService", FakeSvc)
        monkeypatch.setattr(server, "RagQA", FakeQA)

        orig = (server.analyzer, server.rag_store, server.rag_service, server.rag_qa)
        try:
            # 模拟模块加载时未注入 RAG 的初始状态
            server.analyzer = server.ReportAnalyzer(server.ai_client)
            server._init_rag()
            assert server.analyzer.rag_analysis is None
        finally:
            server.analyzer, server.rag_store, server.rag_service, server.rag_qa = orig


class TestChatSessionsApi:
    def test_list_and_get_sessions(self, client, env, monkeypatch, tmp_path):
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)
        s = store.create_session()
        store.append_messages(s["id"], [
            {"role": "user", "content": "长江电力怎么样？"},
            {"role": "assistant", "content": "还不错"},
        ])
        r = client.get("/api/chat/sessions")
        assert r.status_code == 200
        items = r.json()["sessions"]
        assert items[0]["id"] == s["id"]
        assert items[0]["message_count"] == 2

        r2 = client.get(f"/api/chat/sessions/{s['id']}")
        assert r2.status_code == 200
        assert r2.json()["messages"][0]["content"] == "长江电力怎么样？"

        r3 = client.get("/api/chat/sessions/nope")
        assert r3.status_code == 404

    def test_create_session_endpoint(self, client, env, monkeypatch, tmp_path):
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)
        r = client.post("/api/chat/sessions")
        assert r.status_code == 200
        assert r.json()["session_id"]


    def test_create_session_reuses_existing_empty(self, client, env, monkeypatch, tmp_path):
        """「新会话」接口：已有未对话的空会话时直接复用，避免堆积"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)
        r1 = client.post("/api/chat/sessions")
        sid1 = r1.json()["session_id"]
        r2 = client.post("/api/chat/sessions")
        assert r2.json()["session_id"] == sid1
        assert len(store.list_sessions()) == 1

    def test_create_session_after_conversation_makes_new(self, client, env, monkeypatch, tmp_path):
        """空会话已产生对话后，再「新会话」应新建（原会话不再是空会话）"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)
        s = store.create_session()
        store.append_messages(s["id"], [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        r = client.post("/api/chat/sessions")
        assert r.json()["session_id"] != s["id"]
        assert len(store.list_sessions()) == 2

    def test_rename_session_endpoint(self, client, env, monkeypatch, tmp_path):
        """重命名会话标题；空标题报 400；未知会话报 404"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)
        s = store.create_session()

        r = client.patch(f"/api/chat/sessions/{s['id']}", json={"title": "  新的标题  "})
        assert r.status_code == 200
        assert r.json()["title"] == "新的标题"
        assert store.get_session(s["id"])["title"] == "新的标题"

        r2 = client.patch(f"/api/chat/sessions/{s['id']}", json={"title": "   "})
        assert r2.status_code == 400

        r3 = client.patch("/api/chat/sessions/nope", json={"title": "x"})
        assert r3.status_code == 404

    def test_delete_session_endpoint(self, client, env, monkeypatch, tmp_path):
        """删除会话；未知会话报 404"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)
        s = store.create_session()

        r = client.delete(f"/api/chat/sessions/{s['id']}")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert store.get_session(s["id"]) is None

        r2 = client.delete(f"/api/chat/sessions/{s['id']}")
        assert r2.status_code == 404

    def test_chat_stream_without_session_starts_new_session(self, client, env, monkeypatch, tmp_path):
        """流式提问未传 session_id：创建独立会话，不消费预建空会话"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)
        empty = store.create_session()

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None, scope=None, run_id=None):
                yield {"type": "delta", "text": "答", "reasoning": ""}
                yield {"type": "done", "answer": "答",
                       "reasoning": "", "citations": [], "model": "m",
                       "usage": {"total_tokens": 1}}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        r = client.post("/api/chat/stream", json={"question": "问"})
        assert r.status_code == 200
        assert empty["id"] not in r.text
        sessions = store.list_sessions()
        assert len(sessions) == 2
        by_id = {s["id"]: s for s in sessions}
        assert by_id[empty["id"]]["message_count"] == 0
        assert sorted(s["message_count"] for s in sessions) == [0, 2]

    def test_chat_stream_scope_failure_log_is_run_correlated_and_redacted(self, client, env, monkeypatch, tmp_path, caplog):
        """Scope 解析失败也必须使用本次 run_id，且日志不包含用户问题或 traceback。"""
        from webapp.chat_store import ChatStore

        secret_question = "仅用于 Scope 日志泄露测试的用户问题"
        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class BrokenResolver:
            def resolve_for_chat(self, *args, **kwargs):
                raise RuntimeError(secret_question)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None, scope=None, run_id=None):
                yield {"type": "done", "answer": "降级回答", "citations": [], "model": "m", "usage": {}}

        monkeypatch.setattr(server, "_build_scope_resolver", lambda: BrokenResolver())
        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        caplog.set_level(logging.INFO, logger=server.__name__)
        response = client.post("/api/chat/stream", json={"question": secret_question})
        events = _read_sse(response)
        done = next(data for event, data in events if event == "done")
        run_id = done["run"]["id"]

        assert done["run"]["scope"]["fallback_reason"] == "范围解析失败，已回退全库范围"
        assert secret_question not in caplog.text
        assert all(record.exc_info is None for record in caplog.records), "Scope 失败日志不得附带 traceback"
        messages = [record.getMessage() for record in caplog.records]
        assert any("chat_scope_resolution_failed" in message and run_id in message and "RuntimeError" in message for message in messages)

    def test_source_runtime_plan_persists_the_same_four_market_results_without_refetch(
        self, client, env, monkeypatch, tmp_path,
    ):
        from webapp.chat_store import ChatStore
        from webapp.execution_plan import ExecutionPlan
        from webapp.execution_planner import PlanningResult
        from financial_report_fetcher.rag.qa import RagQA

        store = ChatStore(str(tmp_path / "runtime-sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)
        calls = []
        def fake_kline(symbol, *, period, count, adjust):
            calls.append((symbol, period, count, adjust))
            return [{"date": "2026-09-24", "close": 10.0}]
        monkeypatch.setattr(server.tencent_quote, "kline", fake_kline)

        class FixedPlanner:
            def __init__(self, _planner):
                pass
            def plan(self, *_args, **_kwargs):
                plan = ExecutionPlan.from_dict({"objective": "复盘", "source_mode": "market_recap",
                    "steps": [{"id": "indices", "kind": "market_indices", "required": True},
                              {"id": "answer", "kind": "answer", "depends_on": ["indices"]}],
                    "acceptance": ["覆盖四个指数"]})
                return PlanningResult("validated", plan)

        class AnswerAI:
            def chat_stream(self, **_kwargs):
                yield {"type": "done", "answer": "本次行情来源已取得。", "model": "fixture", "usage": {}}

        class NoStore:
            def query(self, *_args, **_kwargs):
                raise AssertionError("plan path must not retrieve again")
        monkeypatch.setattr(server, "ExecutionPlanner", FixedPlanner)
        monkeypatch.setattr(server, "rag_qa", RagQA(NoStore(), AnswerAI()))

        response = client.post("/api/chat/stream", json={"question": "2026-09-24复盘", "use_mcp": False,
                                                         "scope_mode": "whole_corpus"})
        assert response.status_code == 200
        events = _read_sse(response)
        run = _event(events, "done")["run"]
        assert len(calls) == 4
        assert all(period == "day" and count >= 5 for _, period, count, _ in calls)
        assert len(run["tool_artifacts"]) == 4
        assert all(item["as_of"] == "2026-09-24" for item in run["tool_artifacts"])
        assert all("实际数据期 2026-09-24" in item["result_summary"] for item in run["tool_artifacts"])
        assert run["source_summary"]["market_data"] == "已使用"

    @pytest.mark.parametrize(("akshare_available", "baostock_available", "expected_status", "tencent_partial", "recap_limit", "tencent_bad_previous"), [
        (True, True, "completed", False, 15, False), (False, True, "completed", False, 15, False),
        (False, False, "partial", False, 15, False), (True, True, "completed", True, 15, False),
        (False, True, "partial", False, 4, False), (True, True, "completed", False, 15, True),
    ])
    def test_recap_asgi_falls_back_through_authorized_daily_index_sources(
        self, client, env, monkeypatch, tmp_path, akshare_available, baostock_available,
        expected_status, tencent_partial, recap_limit, tencent_bad_previous,
    ):
        from webapp.chat_store import ChatStore
        from webapp.execution_plan import ExecutionPlan
        from webapp.execution_planner import PlanningResult
        from financial_report_fetcher.rag.qa import RagQA

        monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / "recap-sessions.json")))
        if recap_limit != 15:
            from financial_report_fetcher.rag.config import RagConfig
            monkeypatch.setattr(server.RagConfig, "load", classmethod(lambda _cls: RagConfig(
                mcp_max_tool_calls=10, market_recap_max_tool_calls=recap_limit,
            )))
        tencent_calls = []
        akshare_index_calls = []
        baostock_index_calls = []
        provider_call_order = []
        mcp_calls = []
        snapshot_calls = []
        trade_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        def previous_trade_date():
            previous_day = date.fromisoformat(trade_date) - timedelta(days=1)
            while previous_day.weekday() >= 5:
                previous_day -= timedelta(days=1)
            return previous_day.isoformat()
        def fake_kline(symbol, *, period, count, adjust):
            provider_call_order.append(("tencent", symbol))
            tencent_calls.append((symbol, period, count, adjust))
            if symbol == "sh000001":
                if tencent_partial:
                    return [{"date": trade_date, "close": 10.0, "symbol": symbol}]
                if tencent_bad_previous:
                    return [
                        {"date": previous_trade_date(), "close": 0.0, "symbol": symbol},
                        {"date": trade_date, "close": 10.0, "symbol": symbol},
                    ]
                return []
            return [
                {"date": previous_trade_date(), "close": 9.5, "symbol": symbol},
                {"date": trade_date, "close": 10.0, "symbol": symbol},
            ]
        def fake_index_kline(symbol, *, start_date, end_date):
            provider_call_order.append(("akshare", symbol))
            akshare_index_calls.append((symbol, start_date, end_date))
            if not akshare_available:
                return {"as_of": trade_date, "bars": []}
            return {"as_of": trade_date, "bars": [
                {"date": previous_trade_date(), "close": 9.5, "symbol": symbol, "kline_period": "day"},
                {"date": trade_date, "close": 10.0, "symbol": symbol, "kline_period": "day"},
            ]}
        def fake_baostock_index_kline(symbol, *, start_date, end_date):
            provider_call_order.append(("baostock", symbol))
            baostock_index_calls.append((symbol, start_date, end_date))
            if not baostock_available:
                return {"as_of": trade_date, "bars": []}
            return {"as_of": trade_date, "bars": [
                {"date": previous_trade_date(), "close": 9.5, "symbol": symbol, "kline_period": "day"},
                {"date": trade_date, "close": 10.0, "symbol": symbol, "kline_period": "day"},
            ]}
        def fake_mcp(name, arguments, *, timeout, retry):
            mcp_calls.append((name, dict(arguments), timeout, retry))
            return {"as_of": trade_date, "data": [{"date": trade_date, "symbol": "600001"}], "total_rows": 1}
        def fake_snapshot(day):
            snapshot_calls.append(day)
            return {
                "status": "success", "trade_date": day, "as_of": day + "T15:05:00+08:00",
                "universe_rows": 5000, "unique_security_rows": 5000, "classified_rows": 5000,
                "advancing_count": 2500, "declining_count": 2300, "unchanged_count": 200,
                "total_volume": 900000, "total_turnover": 850000000000,
                "volume_sample_count": 5000, "turnover_sample_count": 5000,
                "volume_ratio": 1.1, "volume_ratio_sample_count": 4900,
                "volume_ratio_active_rows": 5000, "volume_ratio_coverage": 0.98,
                "limit_up_count": 35, "limit_down_count": 20, "limitations": "",
            }
        monkeypatch.setattr(server, "get_market_snapshot", fake_snapshot)
        monkeypatch.setattr(server.tencent_quote, "kline", fake_kline)
        monkeypatch.setattr(server, "load_a_share_index_kline", fake_index_kline, raising=False)
        monkeypatch.setattr(server, "load_baostock_index_kline", fake_baostock_index_kline, raising=False)
        monkeypatch.setattr(server.market_data_mcp, "call_tool", fake_mcp)
        monkeypatch.setattr(server, "mcp_breaker", type("Breaker", (), {"allow": lambda self: True})())
        monkeypatch.setattr(server, "_mcp_tool_defs_cache", [
            {"function": {"name": "stock_zt_pool"}},
            {"function": {"name": "stock_sector_fund_flow_rank"}},
        ])
        monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: [
            {"type": "function", "function": {"name": "stock_zt_pool"}},
            {"type": "function", "function": {"name": "stock_sector_fund_flow_rank"}},
        ])
        monkeypatch.setattr(server, "_build_chat_tool_executor", lambda *_args, **_kwargs: lambda *_a: "")
        runtimes = []
        planning_capabilities = []
        original_runtime = server._source_runtime_for_run
        def capture_runtime(*args, **kwargs):
            runtime = original_runtime(*args, **kwargs)
            runtimes.append(runtime)
            return runtime
        monkeypatch.setattr(server, "_source_runtime_for_run", capture_runtime)

        class FixedPlanner:
            def __init__(self, _planner):
                pass
            def plan(self, _question, _scope, capabilities, **_kwargs):
                planning_capabilities.append(capabilities)
                plan = ExecutionPlan.from_dict({"objective": "今日 A股复盘", "source_mode": "market_recap",
                    "steps": [{"id": "indices", "kind": "market_indices"},
                              {"id": "overview", "kind": "market_overview"},
                              {"id": "answer", "kind": "answer", "depends_on": ["indices", "overview"]}],
                    "acceptance": ["披露实际数据覆盖"]})
                return PlanningResult("validated", plan)

        class AnswerAI:
            def __init__(self):
                self.messages = []
            def chat_stream(self, **kwargs):
                self.messages = kwargs.get("messages", [])
                yield {"type": "done", "answer": (
                    "上证指数收于10.00点；上证指数较上一交易日上涨0.50点；上证指数涨跌幅+5.26%。"
                    "市场快照证券样本5000只，已分类5000只；全A量比1.1倍，量比样本覆盖率98%。"
                    "涨停35家，跌停20家。外部参考。"
                ), "model": "fixture", "usage": {}}

        class NoStore:
            def query(self, *_args, **_kwargs):
                raise AssertionError("recap plan must not retrieve")
        answer_ai = AnswerAI()
        monkeypatch.setattr(server, "ExecutionPlanner", FixedPlanner)
        monkeypatch.setattr(server, "rag_qa", RagQA(NoStore(), answer_ai))

        response = client.post("/api/chat/stream", json={
            "question": "今日 A股复盘", "use_mcp": True, "scope_mode": "whole_corpus",
        })
        events = _read_sse(response)
        run = _event(events, "done")["run"]
        assert len(akshare_index_calls) == (0 if recap_limit == 4 else 1)
        if recap_limit != 4:
            assert {call[0] for call in akshare_index_calls} == {"sh000001"}
            assert all(call[2] == trade_date for call in akshare_index_calls)
        assert [call[0] for call in tencent_calls] == ["sh000001", "sz399001", "sz399006", "sh000688"]
        tencent_index_artifacts = [item for item in run["tool_artifacts"]
                                   if item["provider"] == "tencent" and item["tool_name"] == "kline"]
        assert any(item["status"] == ("partial" if tencent_partial or tencent_bad_previous else "unavailable")
                   for item in tencent_index_artifacts)
        if tencent_partial or tencent_bad_previous:
            assert any(item["provider"] == "akshare" and item["tool_name"] == "a_share_index_kline"
                       and item["status"] == "success" for item in run["tool_artifacts"])
        if recap_limit == 4:
            assert baostock_index_calls == []
            assert provider_call_order == [("tencent", symbol) for symbol in
                                           ("sh000001", "sz399001", "sz399006", "sh000688")]
            assert mcp_calls == []
            assert snapshot_calls == []
            assert any(item["status"] == "unavailable" and item["tool_name"] == "a_share_index_kline"
                       for item in run["tool_artifacts"])
        elif akshare_available:
            assert baostock_index_calls == []
            assert provider_call_order == [
                *(('tencent', symbol) for symbol in ("sh000001", "sz399001", "sz399006", "sh000688")),
                ("akshare", "sh000001"),
            ]
            assert [call[0] for call in mcp_calls] == ["stock_sector_fund_flow_rank"]
            assert snapshot_calls == [trade_date]
        else:
            assert [call[0] for call in baostock_index_calls] == ["sh000001"]
            assert provider_call_order[-2:] == [("akshare", "sh000001"), ("baostock", "sh000001")]
            assert [call[0] for call in mcp_calls] == ["stock_sector_fund_flow_rank"]
            assert snapshot_calls == [trade_date]
        assert run["status"] == expected_status, run.get("verification_report")
        assert run["verification_report"]["status"] == "passed"
        if recap_limit == 4:
            assert not any(fact["metric"] == "index_close" and fact["company_code"] == "sh000001"
                           for fact in run["facts"])
        elif tencent_bad_previous:
            assert not any(fact["metric"] == "index_close" and fact["company_code"] == "sh000001"
                           and fact["value"] == 0.0 for fact in run["facts"])
        elif akshare_available:
            assert any(f"上证指数 {trade_date} 较上一交易日涨跌幅=5.26%" in str(message)
                       for message in answer_ai.messages)
        elif baostock_available:
            assert any(item["provider"] == "baostock" and item["tool_name"] == "a_share_index_kline"
                       and item["status"] == "success" for item in run["tool_artifacts"])
            assert any(fact["metric"] == "index_close" and fact["company_code"] == "sh000001"
                       and fact["value"] == 10.0 for fact in run["facts"])
        else:
            assert any(item["provider"] == "baostock" and item["tool_name"] == "a_share_index_kline"
                       and item["status"] == "unavailable" for item in run["tool_artifacts"])
            assert not any(fact["metric"] == "index_close" and fact["company_code"] == "sh000001"
                           for fact in run["facts"])
        if recap_limit == 15:
            assert len(run["tool_artifacts"]) == (7 if akshare_available else 8)
            akshare_artifact = next(item for item in run["tool_artifacts"]
                                    if item["provider"] == "akshare" and item["tool_name"] == "a_share_market_snapshot")
            assert akshare_artifact["as_of"] == trade_date + "T15:05:00+08:00"
        fact_metrics = {fact["metric"] for fact in run["facts"]}
        if recap_limit == 15:
            assert {"market_advancing_count", "market_limit_up_count", "market_volume_ratio"} <= fact_metrics
        expected_calls = 4 if recap_limit == 4 else (7 if akshare_available else 8)
        assert runtimes[0].budget.snapshot() == {"total": expected_calls, "market": expected_calls, "web": 0}
        assert planning_capabilities[0].max_external_calls == recap_limit
        assert planning_capabilities[0].step_costs["market_indices"] == (4 if recap_limit == 4 else 12)
        assert planning_capabilities[0].step_costs["market_overview"] == 3

    def test_chat_stream_sse_and_session_persist(self, client, env, monkeypatch, tmp_path, caplog):
        """流式端点：SSE 事件含 session/delta/done；会话消息持久化"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None, scope=None, run_id=None):
                yield {"type": "delta", "text": "营收", "reasoning": ""}
                yield {"type": "delta", "text": "增长", "reasoning": ""}
                yield {"type": "done", "answer": "营收增长",
                       "reasoning": "", "citations": [], "model": "m",
                       "usage": {"total_tokens": 5}}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        caplog.set_level(logging.INFO, logger=server.__name__)
        r = client.post("/api/chat/stream", json={"question": "长江电力营收如何？"})
        assert r.status_code == 200
        body = r.text
        assert "event: session" in body
        assert "event: delta" in body
        assert "营收" in body
        assert "event: done" in body
        done_data = json.loads(body.split("event: done\ndata: ", 1)[1].split("\n\n", 1)[0])
        assert done_data["elapsed_seconds"] >= 0
        run_id = done_data["run"]["id"]
        messages = [record.getMessage() for record in caplog.records]
        assert any("chat_run_started" in message and run_id in message for message in messages)
        assert any("chat_run_finished" in message and run_id in message and "completed" in message for message in messages)
        # 会话已持久化（user + assistant）
        sessions = store.list_sessions()
        assert len(sessions) == 1
        detail = store.get_session(sessions[0]["id"])
        assert detail["messages"][0]["role"] == "user"
        assert detail["messages"][0]["content"] == "长江电力营收如何？"
        assert datetime.fromisoformat(detail["messages"][0]["created_at"]).utcoffset() == timedelta(hours=8)
        assert detail["messages"][1]["role"] == "assistant"
        assert datetime.fromisoformat(detail["messages"][1]["created_at"]).utcoffset() == timedelta(hours=8)
        assert detail["messages"][1]["content"] == "营收增长"
        # 新 run 必须真实证据化，不再是旧消息迁移的 legacy_evidence_unavailable
        assert detail["messages"][1]["run"]["legacy_evidence_unavailable"] is False
        assert detail["messages"][1]["run"]["status"] == "completed"
        assert detail["messages"][1]["run"]["scope"]["mode"] == "company_only"
        assert detail["messages"][1]["run"]["scope"]["companies"][0]["code"] == "600900"
        assert detail["messages"][1]["run"]["artifacts"] == []

    def test_chat_stream_failure_logs_run_id_without_question_or_traceback(self, client, env, monkeypatch, tmp_path, caplog):
        """诊断日志只保留 run_id/异常类型，绝不写入问题正文或 traceback。"""
        from webapp.chat_store import ChatStore

        secret_question = "仅用于日志泄露测试的用户问题"
        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None, scope=None, run_id=None):
                raise RuntimeError(question)
                yield  # pragma: no cover - 保持为生成器

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        caplog.set_level(logging.INFO, logger=server.__name__)
        response = client.post("/api/chat/stream", json={"question": secret_question})
        events = _read_sse(response)
        error = next(data for event, data in events if event == "error")
        run_id = error["run"]["id"]

        assert error["run"]["status"] == "failed"
        assert run_id
        assert error["error"] == f"回答时遇到问题，请稍后重试。（反馈编号：{run_id}）"
        assert secret_question not in error["error"]
        assert secret_question not in caplog.text
        assert all(record.exc_info is None for record in caplog.records), "诊断日志不得附带 traceback"
        messages = [record.getMessage() for record in caplog.records]
        assert any("chat_run_producer_failed" in message and run_id in message and "RuntimeError" in message for message in messages)
        assert any("chat_run_finished" in message and run_id in message and "failed" in message for message in messages)

    def test_chat_stream_error_event_redacts_sensitive_payload(self, client, env, monkeypatch, tmp_path, caplog):
        """RAG 主动 error 事件的敏感正文也不得进入 SSE 或日志。"""
        from webapp.chat_store import ChatStore

        private_marker = "仅用于 SSE error 事件泄露测试的敏感文本"
        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None, scope=None, run_id=None):
                yield {"type": "error", "error": private_marker}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        caplog.set_level(logging.INFO, logger=server.__name__)
        response = client.post("/api/chat/stream", json={"question": "普通问题"})
        events = _read_sse(response)
        error = next(data for event, data in events if event == "error")
        run_id = error["run"]["id"]

        assert error["error"] == f"回答时遇到问题，请稍后重试。（反馈编号：{run_id}）"
        assert private_marker not in response.text
        assert private_marker not in caplog.text
        assert error["run"]["status"] == "failed"

    def test_chat_stream_never_exposes_model_reasoning(self, client, env, monkeypatch, tmp_path):
        """SSE 只发送回答内容与可解释工具阶段，不发送模型私有推理。"""
        from webapp.chat_store import ChatStore

        monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / "sessions.json")))

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None, scope=None, run_id=None):
                yield {"type": "delta", "text": "", "reasoning": "这是不应暴露的私有推理"}
                yield {"type": "delta", "text": "公开回答", "reasoning": "另一个私有片段"}
                yield {"type": "done", "answer": "公开回答",
                       "reasoning": "完整私有推理", "citations": [], "model": "m",
                       "usage": {"total_tokens": 3}}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        response = client.post("/api/chat/stream", json={"question": "问题"})

        assert response.status_code == 200
        assert "公开回答" in response.text
        assert "这是不应暴露的私有推理" not in response.text
        assert "另一个私有片段" not in response.text
        assert "完整私有推理" not in response.text
        assert "私有推理" not in response.text

    def test_chat_stream_empty_retrieval_default_answer(self, client, env, monkeypatch, tmp_path):
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None, scope=None, run_id=None):
                yield {"type": "empty"}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        r = client.post("/api/chat/stream", json={"question": "x"})
        body = r.text
        assert "本地财报库中没有找到相关内容" in body
        assert "event: done" in body
        assert store.list_sessions()[0]["message_count"] == 2

    def test_chat_stream_without_rag_uses_degraded_tool_orchestration(self, client, env, monkeypatch):
        """索引未初始化时，流式问答仍可进入受控工具编排而不是返回 503。"""
        constructed = []

        class FallbackRagQA:
            def __init__(self, store, *args, **kwargs):
                constructed.append((store, kwargs))

            def answer_stream(self, question, **kwargs):
                yield {"type": "done", "answer": "工具降级回答", "citations": [],
                       "model": "m", "usage": {}, "tools_used": [],
                       "retrieval_degraded": True}

        monkeypatch.setattr(server, "rag_qa", None)
        monkeypatch.setattr(server, "RagQA", FallbackRagQA)
        monkeypatch.setattr(server, "_build_chat_tool_executor", lambda cfg: lambda name, args: "{}")
        response = client.post("/api/chat/stream", json={"question": "x"})

        assert response.status_code == 200
        assert "工具降级回答" in response.text
        assert constructed[0][0] is None


class TestTaskResultApi:
    @pytest.mark.parametrize(
        "value",
        [float("nan"), float("inf"), float("-inf")],
        ids=["nan", "positive-infinity", "negative-infinity"],
    )
    def test_non_finite_task_result_returns_json_failed_state(self, client, value):
        """任务 API 对非有限结果仍应返回可编码的 failed 快照。"""
        task_id = server.task_manager.submit(lambda: {"value": value})
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            snapshot = server.task_manager.get(task_id)
            if snapshot["status"] in ("done", "failed"):
                break
            time.sleep(0.01)

        response = client.get(f"/api/tasks/{task_id}")

        assert response.status_code == 200
        task = response.json()
        assert task["status"] == "failed"
        assert task["result"] is None
        assert "JSON" in task["error"]
        json.dumps(task, allow_nan=False)


class TestCancelAnalysisTask:
    def test_cancel_task_endpoint(self, client, env, monkeypatch):
        """POST /api/tasks/{id}/cancel 调用 task_manager.cancel"""
        class FakeTM:
            def __init__(self):
                self.cancelled = []

            def cancel(self, tid):
                self.cancelled.append(tid)
                return True

        monkeypatch.setattr(server, "task_manager", FakeTM())
        r = client.post("/api/tasks/abc123/cancel")
        assert r.status_code == 200
        assert r.json()["cancelled"] is True

    def test_cancel_unknown_task_404(self, client, env, monkeypatch):
        class FakeTM:
            def cancel(self, tid):
                return False

            def get(self, tid):
                return None

        monkeypatch.setattr(server, "task_manager", FakeTM())
        r = client.post("/api/tasks/nope/cancel")
        assert r.status_code == 404

    def test_analyze_task_can_be_cancelled(self, client, env, monkeypatch):
        """真实 TaskManager：取消信号传入渐进管线，已完成内容可正常收尾。"""
        import threading as _threading

        tm = env["make_task_manager"]()
        monkeypatch.setattr(server, "task_manager", tm)
        entered = _threading.Event()

        def _analyze(request, emit, stop):
            entered.set()
            stop.wait(timeout=5.0)
            return _FakeV3Document(stage="cancelled")

        env["fake_pipeline"].side_effect = _analyze
        r = client.post("/api/reports/600900/2025-12-31/analyze",
                        json={"dimensions": ["financial_summary"]})
        tid = r.json()["task_id"]
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not entered.is_set():
            time.sleep(0.01)
        assert entered.is_set(), "分析任务应进入 analyze"
        r2 = client.post(f"/api/tasks/{tid}/cancel")
        assert r2.status_code == 200
        assert r2.json()["cancelled"] is True
        deadline = time.monotonic() + 5.0
        t = None
        while time.monotonic() < deadline:
            t = tm.get(tid)
            if t["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.01)
        assert t["status"] == "cancelled"


class TestChatStreamPartial:
    def test_chat_stream_saves_partial_when_stream_ends_without_done(
        self, client, env, monkeypatch, tmp_path
    ):
        """流未收到 done（用户停止/断开）：已生成部分写入会话历史"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None, scope=None, run_id=None):
                yield {"type": "delta", "text": "部分回答", "reasoning": ""}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        r = client.post("/api/chat/stream", json={"question": "长江电力营收如何？"})
        assert r.status_code == 200
        assert "部分回答" in r.text
        sid = store.list_sessions()[0]["id"]
        detail = store.get_session(sid)
        assert [m["content"] for m in detail["messages"]] == ["长江电力营收如何？", "部分回答"]
        assert detail["messages"][1]["run"]["status"] == "stopped"


class TestChatStreamRunPersistence:
    def test_chat_stream_persists_scope_and_pdf_artifact(self, client, env, monkeypatch, tmp_path):
        """scope_resolved 冻结公司范围；done 持久化可跳页 PDF 证据 artifact。"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)
        _configure_scoped_rag_answer(env, citations=[{
            "report_id": "601288:2026-06-30:semi_annual",
            "source": "pdf",
            "page": 40,
            "snippet": "现金流",
        }])

        events = _read_sse(client.post("/api/chat/stream", json={
            "question": "经营现金流多少？",
            "focus_report": {"code": "601288", "period": "2026-06-30"},
        }))

        assert _event(events, "scope_resolved")["scope"]["mode"] == "company_only"
        assert _event(events, "run_started")["run_id"]
        assert _event(events, "done")["run"]["artifacts"][0]["page"] == 40
        sid = _event(events, "session")["session_id"]
        persisted = client.get(f"/api/chat/sessions/{sid}").json()["messages"][1]["run"]
        assert persisted["artifacts"][0]["page"] == 40
        assert persisted["status"] == "completed"

    def test_chat_stream_stop_persists_stopped_not_completed(self, client, env, monkeypatch, tmp_path):
        """停止/断开（没有 done）后持久化 stopped run，而不是伪装为 completed。"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)
        events = _disconnect_after_first_delta(
            client, monkeypatch, {"question": "长江电力营收如何？"}, ["部分回答"],
        )
        sid = _event(events, "session")["session_id"]
        run = client.get(f"/api/chat/sessions/{sid}").json()["messages"][1]["run"]
        assert run["status"] == "stopped"
        assert run["content"] == "部分回答"


class TestChatStreamConcurrency:
    def test_chat_stream_concurrent_sessions(self, client, env, monkeypatch, tmp_path):
        """多个会话可同时发起流式请求：互不阻塞，各自完成并落盘会话"""
        import time as _time

        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None, scope=None, run_id=None):
                _time.sleep(0.3)  # 模拟模型耗时；并发应并行而非串行
                yield {"type": "delta", "text": question, "reasoning": ""}
                yield {"type": "done", "answer": question + "答案", "reasoning": "",
                       "citations": [], "model": "m", "usage": {}}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        results = []

        def _ask(q):
            r = client.post("/api/chat/stream", json={"question": q})
            results.append((q, r.status_code, "event: done" in r.text))

        threads = [threading.Thread(target=_ask, args=(f"并发问题{i}",)) for i in range(2)]
        started = _time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=8)
        elapsed = _time.monotonic() - started

        assert len(results) == 2
        assert all(code == 200 and ok for _, code, ok in results), results
        assert len(store.list_sessions()) == 2
        # 并发（~0.3s）而非串行（~0.6s）；放宽到 0.55s 容差
        assert elapsed < 0.55, f"两次流式应并发执行，实际耗时 {elapsed:.2f}s"


class TestMcpChat:
    def test_chat_stream_passthrough_tool_events(self, client, env, monkeypatch, tmp_path):
        """/api/chat/stream 透传 tool_call/tool_result，done 带 tools_used"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None, scope=None, run_id=None):
                yield {"type": "tool_call", "name": "get_financial_metrics",
                       "arguments": {"symbol": "600519"}}
                yield {"type": "tool_result", "name": "get_financial_metrics",
                       "summary": '{"net_profit": 345}'}
                yield {"type": "done", "answer": "结合MCP数据：净利345亿",
                       "reasoning": "", "citations": [], "model": "m",
                       "usage": {}, "tools_used": ["get_financial_metrics"]}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        r = client.post("/api/chat/stream", json={"question": "600900净利如何？"})
        body = r.text
        assert "event: tool_call" in body
        assert "get_financial_metrics" in body
        assert "event: tool_result" in body
        assert "tools_used" in body
        assert "event: done" in body
        assert store.list_sessions()[0]["message_count"] == 2

    def test_non_financial_request_cannot_passthrough_untrusted_web_sources(self, client, env, monkeypatch, tmp_path):
        from webapp.chat_intent import ChatIntentAnalysis
        from webapp.chat_store import ChatStore

        monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / "sessions.json")))
        monkeypatch.setattr(server, "_analyze_chat_intent", lambda *_args: ChatIntentAnalysis(
            "non_financial", "none", "", "", "none", "", False, "",
        ))

        class ForbiddenRagQA:
            def answer_stream(self, *_args, **_kwargs):
                raise AssertionError("non-financial requests must not reach answer generation")

        monkeypatch.setattr(server, "rag_qa", ForbiddenRagQA())
        response = client.post("/api/chat/stream", json={"question": "问什么？"})
        assert "当前只回答金融相关问题" in response.text
        assert "web_sources" not in response.text
        assert "event: tool_call" not in response.text

    def test_chat_stream_use_mcp_false_still_streams(self, client, env, monkeypatch, tmp_path):
        """use_mcp=false 时仍正常流式（工具开关不改变响应结构）"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None, scope=None, run_id=None):
                assert tools is None or question != "无工具问题"
                yield {"type": "delta", "text": "答案", "reasoning": ""}
                yield {"type": "done", "answer": "答案", "reasoning": "", "citations": [],
                       "model": "m", "usage": {}, "tools_used": []}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        r = client.post("/api/chat/stream",
                        json={"question": "无工具问题", "use_mcp": False})
        assert r.status_code == 200
        assert "event: done" in r.text

    def test_chat_stream_exposes_retrieval_degraded(self, client, env, monkeypatch, tmp_path):
        from webapp.chat_store import ChatStore

        monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / "sessions.json")))

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None,
                              priority_report_id=None, scope=None, run_id=None):
                yield {"type": "done", "answer": "降级回答", "citations": [],
                       "tools_used": [], "retrieval_degraded": True}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        response = client.post("/api/chat/stream", json={"question": "测试降级"})

        assert response.status_code == 200
        assert '"retrieval_degraded": true' in response.text


class TestMcpToolExecutor:
    def test_executor_filters_arguments_by_live_schema(self, monkeypatch):
        calls = []

        class FakeMCP:
            def call_tool(self, name, arguments, timeout=None):
                calls.append((name, arguments))
                return '{"time":"now"}'

        monkeypatch.setattr(server, "market_data_mcp", FakeMCP())
        monkeypatch.setattr(server, "_mcp_tool_input_schemas", {
            "get_time_info": {"type": "object", "properties": {}},
        })
        cfg = type("C", (), {"mcp_tools": True, "mcp_tool_timeout": 15})()

        result = server._build_mcp_tool_executor(cfg)(
            "get_time_info", {"symbol": "600519", "output_format": "markdown"}
        )

        assert calls == [("get_time_info", {})]
        assert result == '{"time":"now"}'

    def test_executor_resolves_symbol_name_to_code(self, monkeypatch):
        """执行器把股票名称解析为 6 位代码后再调 MCP"""
        calls = []

        class FakeIndex:
            def search(self, q, limit=10):
                return [{"code": "600900", "name": "长江电力"}] if "长江" in q else []

            def is_valid_code(self, code):
                return len(code) == 6 and code.isdigit()

        class FakeMCP:
            def call_tool(self, name, arguments, timeout=None):
                calls.append((name, arguments, timeout))
                return "{}"

        monkeypatch.setattr(server, "stock_index", FakeIndex())
        monkeypatch.setattr(server, "market_data_mcp", FakeMCP())
        cfg = type("C", (), {"mcp_tools": True, "mcp_tool_timeout": 15, "mcp_max_tool_rounds": 3})()
        executor = server._build_mcp_tool_executor(cfg)
        assert executor is not None
        result = executor("get_financial_metrics", {"symbol": "长江电力"})
        assert calls == [("get_financial_metrics", {"symbol": "600900", "output_format": "json"}, 15)]
        assert result == "{}"

    def test_executor_unresolvable_symbol_returns_hint(self, monkeypatch):
        """无法解析的股票名返回提示文本，不调用 MCP"""
        class FakeIndex:
            def search(self, q, limit=10):
                return []

            def is_valid_code(self, code):
                return len(code) == 6 and code.isdigit()

        monkeypatch.setattr(server, "stock_index", FakeIndex())
        cfg = type("C", (), {"mcp_tools": True, "mcp_tool_timeout": 15, "mcp_max_tool_rounds": 3})()
        executor = server._build_mcp_tool_executor(cfg)
        result = executor("get_realtime_quote", {"symbol": "不存在的公司"})
        assert "无法解析" in result

    def test_executor_none_when_disabled(self, monkeypatch):
        """mcp_tools=false 时返回 None（不启用工具）"""
        cfg = type("C", (), {"mcp_tools": False, "mcp_tool_timeout": 15, "mcp_max_tool_rounds": 3})()
        assert server._build_mcp_tool_executor(cfg) is None


class TestMcpToolDefs:
    def test_mcp_tool_defs_does_not_inject_stale_tools_when_list_fails(self, monkeypatch):
        """问答 MCP 清单不可用时不注入旧客户端的兜底工具。"""
        class FakeMCP:
            def list_tools(self, timeout=None):
                raise RuntimeError("MCP 不可用")

        monkeypatch.setattr(server, "market_data_mcp", FakeMCP())
        monkeypatch.setattr(server, "_mcp_tool_defs_cache", None)
        monkeypatch.setattr(server, "_mcp_tool_defs_ready", False)
        defs = server._mcp_tool_defs()
        assert defs is None

    def test_chat_tool_defs_adds_web_search_when_key_configured(self, monkeypatch):
        class FakeSearch:
            available = True

            def __init__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(server, "TavilyWebSearch", FakeSearch)
        monkeypatch.setattr(server, "_mcp_tool_defs", lambda: [])
        cfg = type("C", (), {"mcp_tools": False, "web_search": True, "web_search_timeout": 15})()
        defs = server._build_chat_tool_defs(cfg)
        # 补报工具只申请授权、由服务端解析候选，因此始终随问答工具一起提供。
        assert [item["function"]["name"] for item in defs] == ["web_search", "request_missing_reports"]

    def test_chat_tool_defs_exposes_web_search_when_only_deepseek_is_available(self, monkeypatch):
        class AvailableDeepSeek:
            available = True
            def __init__(self, *args, **kwargs):
                pass

        class UnavailableTavily:
            available = False
            def __init__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(server, "DeepSeekWebSearch", AvailableDeepSeek)
        monkeypatch.setattr(server, "TavilyWebSearch", UnavailableTavily)
        monkeypatch.setattr(server, "_mcp_tool_defs", lambda: None)
        cfg = type("C", (), {"mcp_tools": False, "web_search": True, "web_search_timeout": 15})()

        definitions = server._build_chat_tool_defs(cfg)

        assert [item["function"]["name"] for item in definitions] == [
            "web_search", "request_missing_reports",
        ]

    def test_chat_tool_defs_does_not_expose_removed_news_tool(self, monkeypatch):
        """新闻 MCP 被下线后，问答模型仍可获得网页搜索作为时效性信息来源。"""
        class FakeSearch:
            available = True

            def __init__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(server, "TavilyWebSearch", FakeSearch)
        monkeypatch.setattr(server, "_mcp_tool_defs", lambda: [
            {"type": "function", "function": {"name": "get_news_data", "parameters": {}}},
            {"type": "function", "function": {"name": "get_realtime_quote", "parameters": {}}},
        ])
        cfg = type("C", (), {"mcp_tools": True, "web_search": True, "web_search_timeout": 15})()

        defs = server._build_chat_tool_defs(cfg)

        assert [item["function"]["name"] for item in defs] == [
            "get_realtime_quote", "web_search", "request_missing_reports",
        ]

    def test_mcp_tool_defs_built_when_available(self, monkeypatch):
        """MCP 可用时构建工具定义并缓存"""
        class FakeMCP:
            def list_tools(self, timeout=None):
                return [
                    {"name": "get_realtime_quote", "description": "实时行情",
                     "input_schema": {"type": "object", "properties": {}}},
                ]

        monkeypatch.setattr(server, "market_data_mcp", FakeMCP())
        monkeypatch.setattr(server, "_mcp_tool_defs_cache", None)
        monkeypatch.setattr(server, "_mcp_tool_defs_ready", False)
        defs = server._mcp_tool_defs()
        assert defs and defs[0]["function"]["name"] == "get_realtime_quote"
        assert server._mcp_tool_defs() is defs  # 已缓存


class TestRealtimeRouting:
    def test_executor_realtime_routes_to_tencent(self, monkeypatch):
        """get_realtime_data 走腾讯行情（不调用 stock_mcp），返回实时 JSON"""
        calls = {"mcp": [], "tq": []}

        class FakeIndex:
            def is_valid_code(self, code):
                return len(code) == 6 and code.isdigit()

            def search(self, q, limit=10):
                return []

        class FakeTencent:
            def realtime(self, symbols):
                calls["tq"].append(symbols)
                return [{
                    "code": "600900", "name": "长江电力", "price": 28.43,
                    "prev_close": 28.21, "open": 28.2, "high": 28.49,
                    "low": 28.12, "change": 0.22, "change_pct": 0.78,
                    "volume": 359884, "amount_wan": 102049, "turnover_rate": 0.15,
                    "pe": 19.28, "total_mv_yi": 6956.31, "time": "2026-08-26 10:00:00",
                }]

        class FakeMCP:
            def call_tool(self, name, arguments, timeout=None):
                calls["mcp"].append((name, arguments))

        monkeypatch.setattr(server, "stock_index", FakeIndex())
        monkeypatch.setattr(server, "tencent_quote", FakeTencent())
        monkeypatch.setattr(server, "market_data_mcp", FakeMCP())
        cfg = type("C", (), {"mcp_tools": True, "mcp_tool_timeout": 15, "mcp_max_tool_rounds": 3})()
        executor = server._build_mcp_tool_executor(cfg)
        result = executor("get_realtime_data", {"symbol": "600900"})
        import json as _json
        payload = _json.loads(result)
        assert payload["price"] == 28.43
        assert payload["name"] == "长江电力"
        assert calls["tq"] == [["600900"]]
        assert calls["mcp"] == []

    def test_executor_other_tools_still_use_mcp(self, monkeypatch):
        """非实时行情工具走问答专用市场 MCP。"""
        calls = []

        class FakeIndex:
            def is_valid_code(self, code):
                return len(code) == 6 and code.isdigit()

            def search(self, q, limit=10):
                return []

        class FakeMCP:
            def call_tool(self, name, arguments, timeout=None):
                calls.append((name, arguments))
                return "{}"

        monkeypatch.setattr(server, "stock_index", FakeIndex())
        monkeypatch.setattr(server, "market_data_mcp", FakeMCP())
        cfg = type("C", (), {"mcp_tools": True, "mcp_tool_timeout": 15, "mcp_max_tool_rounds": 3})()
        executor = server._build_mcp_tool_executor(cfg)
        executor("get_financial_metrics", {"symbol": "600900"})
        assert calls == [("get_financial_metrics", {"symbol": "600900", "output_format": "json"})]


class TestSymbolResolution:
    def test_code_parsed_without_index(self, monkeypatch):
        """6 位数字代码直接可用，不依赖股票索引"""
        class FakeIndex:
            def is_valid_code(self, code):
                return False  # 索引不可用

            def search(self, q, limit=10):
                return []

        monkeypatch.setattr(server, "stock_index", FakeIndex())
        assert server._resolve_symbol_code("600900") == "600900"

    def test_name_resolved_via_mcp_dict(self, monkeypatch):
        """索引不可用时用 MCP 全市场名称词典解析名称（带缓存，只加载一次）"""
        calls = []

        class FakeIndex:
            def is_valid_code(self, code):
                return False

            def search(self, q, limit=10):
                return []

        class FakeMCP:
            def call_tool(self, name, arguments, timeout=None):
                calls.append(name)
                return '[{"code":"600900","name":"长江电力"},{"code":"600519","name":"贵州茅台"}]'

        monkeypatch.setattr(server, "stock_index", FakeIndex())
        monkeypatch.setattr(server, "stock_mcp", FakeMCP())
        monkeypatch.setattr(server, "_stock_name_cache", None)
        assert server._resolve_symbol_code("长江电力") == "600900"
        assert server._resolve_symbol_code("贵州茅台") == "600519"
        assert calls == ["get_stock_a_code_name"]  # 缓存：只加载一次


class TestResolveCompanyIndustry:
    def _fake_stock_mcp(self, monkeypatch, raw=None, error=None):
        class FakeMCP:
            def call_tool(self, name, arguments, timeout=None):
                if error is not None:
                    raise error
                return raw

        monkeypatch.setattr(server, "stock_mcp", FakeMCP())
        return FakeMCP

    def test_reads_explicit_industry_field(self, monkeypatch):
        self._fake_stock_mcp(
            monkeypatch,
            raw='{"industry": "银行", "industry_name": "银行", "A股简称": "农业银行"}',
        )

        ref = server._resolve_company_industry("601288")

        assert ref is not None
        assert ref.name == "银行"
        assert ref.provider == "china-stock-mcp"
        assert ref.resolved_at

    def test_reads_industry_name_when_industry_missing(self, monkeypatch):
        self._fake_stock_mcp(monkeypatch, raw='{"industry_name": "电力"}')

        ref = server._resolve_company_industry("600900")

        assert ref.name == "电力"

    def test_non_json_returns_none(self, monkeypatch):
        self._fake_stock_mcp(monkeypatch, raw="not-json")

        assert server._resolve_company_industry("601288") is None

    def test_missing_industry_field_returns_none(self, monkeypatch):
        self._fake_stock_mcp(monkeypatch, raw='{"A股简称": "农业银行"}')

        assert server._resolve_company_industry("601288") is None

    def test_empty_industry_field_returns_none(self, monkeypatch):
        self._fake_stock_mcp(monkeypatch, raw='{"industry": "  "}')

        assert server._resolve_company_industry("601288") is None

    def test_tool_failure_returns_none(self, monkeypatch):
        self._fake_stock_mcp(monkeypatch, error=RuntimeError("mcp down"))

        assert server._resolve_company_industry("601288") is None


class TestMcpCircuit:
    def _cfg(self):
        return type("C", (), {"mcp_tools": True, "mcp_tool_timeout": 15, "mcp_max_tool_rounds": 3})()

    def _fake_index(self):
        class FakeIndex:
            def is_valid_code(self, code):
                return len(code) == 6 and code.isdigit()

            def search(self, q, limit=10):
                return []

        return FakeIndex()

    def test_executor_blocks_when_circuit_open(self, monkeypatch):
        """熔断打开且冷却期内：executor 直接返回提示，不调用 MCP"""
        from webapp.mcp_guard import McpCircuitBreaker

        breaker = McpCircuitBreaker(failure_threshold=2, cooldown_seconds=300)
        breaker.record_failure("e")
        breaker.record_failure("e")
        monkeypatch.setattr(server, "mcp_breaker", breaker)
        monkeypatch.setattr(server, "stock_index", self._fake_index())
        calls = []
        monkeypatch.setattr(server, "stock_mcp", type("M", (), {"call_tool": lambda *a, **k: calls.append(a)})())
        executor = server._build_mcp_tool_executor(self._cfg())
        result = executor("get_financial_metrics", {"symbol": "600900"})
        assert "暂不可用" in result
        assert calls == []

    def test_executor_records_failure(self, monkeypatch):
        """工具调用异常记录到熔断器"""
        from webapp.mcp_guard import McpCircuitBreaker

        breaker = McpCircuitBreaker(failure_threshold=5, cooldown_seconds=300)
        monkeypatch.setattr(server, "mcp_breaker", breaker)
        monkeypatch.setattr(server, "stock_index", self._fake_index())

        def _fail(name, arguments, timeout=None):
            raise RuntimeError("MCP 连接断开")

        monkeypatch.setattr(server, "stock_mcp", type("M", (), {"call_tool": _fail})())
        executor = server._build_mcp_tool_executor(self._cfg())
        result = executor("get_financial_metrics", {"symbol": "600900"})
        assert "工具调用失败" in result
        assert breaker.status()["consecutive_failures"] == 1

    def test_executor_records_success(self, monkeypatch):
        """工具调用成功清零连续失败并累计成功数"""
        from webapp.mcp_guard import McpCircuitBreaker

        breaker = McpCircuitBreaker(failure_threshold=5, cooldown_seconds=300)
        breaker.record_failure("e")
        monkeypatch.setattr(server, "mcp_breaker", breaker)
        monkeypatch.setattr(server, "stock_index", self._fake_index())
        monkeypatch.setattr(server, "market_data_mcp", type("M", (), {"call_tool": lambda *a, **k: "{}"})())
        executor = server._build_mcp_tool_executor(self._cfg())
        executor("get_financial_metrics", {"symbol": "600900"})
        st = breaker.status()
        assert st["consecutive_failures"] == 0
        assert st["success_calls"] == 1

    def test_mcp_status_endpoint(self, client, env, monkeypatch):
        from webapp.mcp_guard import McpCircuitBreaker

        monkeypatch.setattr(server, "mcp_breaker", McpCircuitBreaker(failure_threshold=3, cooldown_seconds=300))
        r = client.get("/api/mcp/status")
        assert r.status_code == 200
        body = r.json()
        assert body["circuit"] == "closed"
        assert "consecutive_failures" in body
        assert "total_calls" in body

    def test_mcp_diagnose_endpoint(self, client, env, monkeypatch):
        """诊断检查问答实际使用的 MCP，并返回数据源检查结果。"""
        class MarketMCP:
            def list_tools(self, timeout=None):
                return [
                    {"name": "index_prices"},
                    {"name": "stock_sector_fund_flow_rank"},
                    {"name": "data_source_status"},
                ]

            def call_tool(self, name, arguments, timeout=None):
                assert name == "data_source_status"
                assert arguments == {}
                return '{"tushare":"ok","eastmoney":"degraded"}'

        class LegacyMCP:
            def list_tools(self, timeout=None):
                raise AssertionError("诊断不应再访问旧 MCP")

        monkeypatch.setattr(server, "market_data_mcp", MarketMCP())
        monkeypatch.setattr(server, "stock_mcp", LegacyMCP())
        r = client.post("/api/mcp/diagnose")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["provider"] == "stock-data-mcp"
        assert body["tools"] == ["index_prices", "stock_sector_fund_flow_rank", "data_source_status"]
        assert body["data_source_status"]["ok"] is True

    def test_mcp_diagnose_reports_data_source_failure(self, client, env, monkeypatch):
        """数据源状态调用失败时，不能把 MCP 显示为正常。"""
        class MarketMCP:
            def list_tools(self, timeout=None):
                return [{"name": "data_source_status"}]

            def call_tool(self, name, arguments, timeout=None):
                raise RuntimeError("Eastmoney 连接超时")

        monkeypatch.setattr(server, "market_data_mcp", MarketMCP())
        r = client.post("/api/mcp/diagnose")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["data_source_status"]["ok"] is False
        assert "Eastmoney" in body["data_source_status"]["message"]


class TestDownloadReport:
    def test_download_report_triggers_download(self, client, env, monkeypatch, tmp_path):
        """未下载时 POST download 触发下载并返回 downloaded=True"""
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        monkeypatch.setattr(server, "_pdf_file_exists", lambda meta: False)
        monkeypatch.setattr(server, "_local_pdf_path", lambda meta: str(pdf))
        r = client.post("/api/reports/600900/2025-12-31/download")
        assert r.status_code == 200
        assert r.json()["downloaded"] is True
        assert env["fake_dl"].download_one.call_count == 1

    def test_download_report_idempotent_when_exists(self, client, env, monkeypatch, tmp_path):
        """已下载时不重复触发下载（幂等）"""
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        monkeypatch.setattr(server, "_pdf_file_exists", lambda meta: True)
        monkeypatch.setattr(server, "_local_pdf_path", lambda meta: str(pdf))
        r = client.post("/api/reports/600900/2025-12-31/download")
        assert r.status_code == 200
        assert r.json()["downloaded"] is True
        assert env["fake_dl"].download_one.call_count == 0


class TestIndexCache:
    def test_index_no_cache_and_versioned_assets(self, client):
        """首页禁用缓存，且前端资源带版本号并按依赖顺序加载。"""
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache"
        body = r.text
        workflow_src = "/static/analysis_workflow.js?v="
        chat_rendering_src = "/static/chat_rendering.js?v="
        app_src = "/static/app.js?v="
        assert workflow_src in body
        assert chat_rendering_src in body
        assert "/static/app.js?v=" in body
        assert "/static/style.css?v=" in body
        assert body.index(workflow_src) < body.index(app_src)
        assert body.index(chat_rendering_src) < body.index(app_src)

    def test_asset_version_tracks_the_structure_visualization_script(self, client, monkeypatch):
        """结构图脚本必须计入版本号，否则浏览器会继续用旧缓存。"""
        import webapp.server as server

        newest = 2_000_000_000
        real_getmtime = os.path.getmtime
        visualization_asset = os.path.join(server.STATIC_DIR, "analysis_visualizations.js")

        def fake_getmtime(path):
            if path == visualization_asset:
                return newest
            return real_getmtime(path)

        monkeypatch.setattr(os.path, "getmtime", fake_getmtime)
        body = client.get("/").text

        assert "/static/analysis_visualizations.js?v=%d" % newest in body


class TestHealthStartedAt:
    def test_health_includes_started_at(self, client):
        """健康检查返回服务启动时间（供状态脚本判断是否最新代码）"""
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["started_at"]
        assert isinstance(body["started_ts"], float)
        assert body["started_ts"] > 0


class TestFocusReport:
    def test_chat_stream_focus_report_sets_priority(self, client, env, monkeypatch, tmp_path):
        """focus_report 解析为 report_id 并提升检索权重（传给 answer_stream）"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "s.json"))
        monkeypatch.setattr(server, "chat_store", store)
        captured = {}

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None,
                              priority_report_id=None, scope=None, run_id=None):
                captured["priority"] = priority_report_id
                yield {"type": "delta", "text": "x", "reasoning": ""}
                yield {"type": "done", "answer": "x", "reasoning": "", "citations": [],
                       "model": "m", "usage": {}, "tools_used": []}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        r = client.post("/api/chat/stream", json={
            "question": "营收如何？",
            "focus_report": {"code": "600900", "period": "2025-12-31"},
        })
        assert r.status_code == 200
        assert captured["priority"] == "600900:2025-12-31:annual"

    def test_chat_stream_without_focus_report(self, client, env, monkeypatch, tmp_path):
        """未传 focus_report 时不指定优先报告"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "s.json"))
        monkeypatch.setattr(server, "chat_store", store)
        captured = {}

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None,
                              priority_report_id=None, scope=None, run_id=None):
                captured["priority"] = priority_report_id
                yield {"type": "done", "answer": "x", "reasoning": "", "citations": [],
                       "model": "m", "usage": {}, "tools_used": []}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        r = client.post("/api/chat/stream", json={"question": "长江电力营收如何？"})
        assert r.status_code == 200
        assert captured["priority"] is None


# ── 智能问答财报补充下载（授权 / 恢复回答）─────────────────────

_SUPPLEMENT_CODE = "601288"
_SUPPLEMENT_NAME = "农业银行"
_SUPPLEMENT_INDEXED = "601288:2024-12-31:annual"
_SUPPLEMENT_REQUESTED = "601288:2025-06-30:semi_annual"


def _supplement_question():
    """问题显式包含本地已索引的公司代码 → company_only 范围。"""
    return {"question": "601288 的 2025 半年报经营现金流变化？"}


def _supplement_meta(period, report_type):
    return ReportMeta(
        company_id=_SUPPLEMENT_CODE,
        company_name=_SUPPLEMENT_NAME,
        report_type=report_type,
        period=period,
        download_url="http://cninfo.example/secret-report.pdf",
        title=f"{_SUPPLEMENT_NAME}{period.year}年报告",
        disclosure_date=period,
    )


class _SupplementRagQA:
    """首轮提交受控补报需求；恢复轮产出普通回答，并记录每次调用的范围。"""

    def __init__(self, needs=None, answer="补充后回答", handler=None, resume_error="",
                 resume_empty=False):
        self.answer = answer
        self.handler = handler
        self.resume_error = resume_error
        self.resume_empty = resume_empty
        self.handler_accepted = None
        self.needs = list(needs) if needs is not None else [
            {"period": "2025-06-30", "report_type": "semi_annual"},
        ]
        self.calls = []

    def answer_stream(self, question, history=None, filters=None, tools=None,
                      priority_report_id=None, scope=None, run_id=None):
        self.calls.append({"question": question, "scope": scope, "tools": tools,
                           "history": history, "run_id": run_id})
        if len(self.calls) == 1:
            # 模拟 RagQA：补报处理器在生产线程内被回调，只有它接受需求才上交模型请求。
            if self.handler is not None:
                self.handler_accepted = bool(self.handler({
                    "reason": "本地缺少 2025 年半年报原文", "needs": list(self.needs),
                }))
            if self.handler is None or self.handler_accepted:
                yield {"type": "supplement_request", "reason": "本地缺少 2025 年半年报原文",
                       "needs": list(self.needs)}
            else:
                yield {"type": "delta", "text": "本地证据不足。", "reasoning": ""}
                yield {"type": "done", "answer": "本地证据不足。", "reasoning": "",
                       "citations": [], "model": "m", "usage": {},
                       "tools_used": [], "web_sources": [],
                       "retrieval_report_ids": [], "retrieval_degraded": False}
            return
        if self.resume_error:
            yield {"type": "error", "error": self.resume_error}
            return
        if self.resume_empty:
            yield {"type": "empty"}
            return
        yield {"type": "delta", "text": self.answer, "reasoning": ""}
        yield {"type": "done", "answer": self.answer, "reasoning": "",
               "citations": [], "model": "m", "usage": {},
               "tools_used": [], "web_sources": [],
               "retrieval_report_ids": [], "retrieval_degraded": False}


class _SupplementDownloader:
    """受控下载器替身：按报告身份返回状态，成功时写出最小合法 PDF。"""

    def __init__(self, statuses=None):
        self.statuses = dict(statuses or {})
        self.calls = []

    def download_one(self, report, storage_dir):
        report_id = build_report_id(report.company_id, report.period, report.report_type)
        self.calls.append(report_id)
        status = self.statuses.get(report_id, DownloadStatus.SUCCESS)
        if status in (DownloadStatus.SUCCESS, DownloadStatus.SKIPPED):
            os.makedirs(storage_dir, exist_ok=True)
            with open(os.path.join(storage_dir, build_report_filename(report)), "wb") as handle:
                handle.write(b"%PDF-1.4 fake")
        return status


class _SupplementIngestionService:
    """摄取服务替身：默认产出 PDF 索引证据；可指定某些文件摄取失败。"""

    def __init__(self, failing_filenames=()):
        self.failing = set(failing_filenames)
        self.calls = []

    def auto_ingest_pdf(self, pdf_path):
        self.calls.append(pdf_path)
        return os.path.basename(pdf_path) not in self.failing


@pytest.fixture()
def supplement_env(env, monkeypatch, tmp_path):
    """隔离补报协作方：会话存储、报告目录、报告元数据、下载器与摄取服务。"""
    from webapp.chat_store import ChatStore
    from webapp.chat_supplement import SupplementRegistry

    reports_dir = tmp_path / "supplement-reports"
    reports_dir.mkdir()
    store = ChatStore(str(tmp_path / "sessions.json"))
    # handler 传入真实处理器：验证生产线程内补报上下文确实可达。
    rag = _SupplementRagQA(handler=server._handle_supplement_request)
    downloader = _SupplementDownloader()
    ingestion = _SupplementIngestionService()
    metas = [_supplement_meta(date(2025, 6, 30), ReportType.SEMI_ANNUAL)]
    env["fake_ds"].fetch_reports.side_effect = (
        lambda stock_code, report_types, start_date, end_date: [
            meta for meta in metas
            if meta.company_id == stock_code and meta.report_type in list(report_types)
        ]
    )

    class FakeRagStore:
        def list_report_ids(self):
            return [_SUPPLEMENT_INDEXED]

    monkeypatch.setattr(server, "chat_store", store)
    monkeypatch.setattr(server, "supplement_registry", SupplementRegistry())
    monkeypatch.setattr(server, "_supplement_reports", {})
    monkeypatch.setattr(server, "REPORTS_DIR", str(reports_dir))
    monkeypatch.setattr(server, "rag_qa", rag)
    monkeypatch.setattr(server, "rag_store", FakeRagStore())
    monkeypatch.setattr(server, "downloader", downloader)
    monkeypatch.setattr(server, "rag_service", ingestion)

    return {
        "store": store,
        "rag": rag,
        "downloader": downloader,
        "ingestion": ingestion,
        "metas": metas,
        "reports_dir": reports_dir,
    }


def _propose_supplement(client, question=None):
    """经真实流式端点提出补报请求，返回 (事件列表, supplement_needed 数据)。"""
    events = _read_sse(client.post("/api/chat/stream", json=question or _supplement_question()))
    return events, _event(events, "supplement_needed")


def _propose_session_id(proposal_events):
    """从提出补报的 SSE 事件里取会话 id（授权必须绑定到同一会话）。"""
    return _event(proposal_events, "session")["session_id"]


def _resolve_supplement(client, supplement_id, body):
    return _read_sse(client.post(
        f"/api/chat/supplements/{supplement_id}/resolve", json=body,
    ))


def _ask_in_same_session(client, session_id, question):
    """在同一会话继续提问；授权卡绑定的是提出问题的那次问答。"""
    return _read_sse(client.post("/api/chat/stream", json={
        "session_id": session_id, "question": question,
    }))


def _candidate_ids(payload):
    return [candidate["id"] for candidate in payload["candidates"]]


class TestChatSupplementApi:
    def test_stream_supplement_needs_consent_and_does_not_download(self, client, supplement_env):
        """缺证据时只提出候选清单并暂停；未经授权不调用下载器。"""
        events, needed = _propose_supplement(client)

        assert needed["limit"] == 5
        assert needed["reason"] == "本地缺少 2025 年半年报原文"
        assert [candidate["code"] for candidate in needed["candidates"]] == [_SUPPLEMENT_CODE]
        assert [candidate["period"] for candidate in needed["candidates"]] == ["2025-06-30"]
        assert needed["candidates"][0]["label"] == "2025 半年报"
        assert supplement_env["downloader"].calls == []
        assert supplement_env["ingestion"].calls == []
        names = [name for name, _ in events]
        assert names[:3] == ["session", "run_started", "reasoning_stage"]
        assert events[2][1]["message"] == "正在准备分析步骤…"
        assert names[3:] == ["scope_resolved", "plan_fallback", "policy_resolved", "policy_fallback",
                             "supplement_needed"]

    def test_supplement_handler_is_reachable_from_the_streaming_producer_thread(
        self, client, supplement_env,
    ):
        """补报处理器必须在模型调用线程内可用，否则模型永远申请不到授权。"""
        _propose_supplement(client)

        assert supplement_env["rag"].handler_accepted is True

    def test_proposed_supplement_persists_waiting_consent_and_keeps_only_safe_fields(
        self, client, supplement_env,
    ):
        """授权前保存 waiting_consent 运行；摘要不含 URL、文件路径或异常文本。"""
        proposal_events, needed = _propose_supplement(client)

        sessions = supplement_env["store"].list_sessions()
        detail = supplement_env["store"].get_session(sessions[0]["id"])
        run = detail["messages"][-1]["run"]
        assert run["status"] == "waiting_consent"
        assert run["supplement"]["status"] == "proposed"
        assert run["supplement"]["limit"] == 5
        assert len(run["supplement"]["candidates"]) == 1

        raw = json.dumps(detail, ensure_ascii=False)
        assert "cninfo.example" not in raw
        assert "secret-report.pdf" not in raw
        assert str(supplement_env["reports_dir"]) not in raw
        assert needed["candidates"][0]["id"] in raw
        # 候选载荷同样不含下载地址
        assert "cninfo.example" not in json.dumps(needed, ensure_ascii=False)

    def test_approved_supplement_downloads_ingests_and_resumes_answer(
        self, client, supplement_env,
    ):
        """授权后下载并摄取，随后用同一问题恢复回答；范围只追加已摄取报告。"""
        proposal_events, needed = _propose_supplement(client)
        supplement_id = needed["supplement_id"]

        events = _resolve_supplement(client, supplement_id, {
            "session_id": _propose_session_id(proposal_events),
            "action": "approve",
            "candidate_ids": _candidate_ids(needed),
        })

        names = [name for name, _ in events]
        assert names == [
            "session", "supplement_download_started", "supplement_downloaded",
            "supplement_ingested", "run_started", "delta", "done",
        ]
        assert supplement_env["downloader"].calls == [_SUPPLEMENT_REQUESTED]
        assert [os.path.basename(path) for path in supplement_env["ingestion"].calls] == [
            "农业银行_601288_半年报_2025.pdf"
        ]

        done = _event(events, "done")
        assert done["answer"] == "补充后回答"
        supplement = done["run"]["supplement"]
        assert supplement["status"] == "completed"
        assert supplement["ingested_report_ids"] == [_SUPPLEMENT_REQUESTED]
        assert supplement["skipped_report_ids"] == []
        assert supplement["failed"] == []
        datetime.fromisoformat(supplement["resumed_at"])

        # 恢复回答使用原问题，并在受控范围内追加已摄取报告
        resumed = supplement_env["rag"].calls[-1]
        assert resumed["question"] == _supplement_question()["question"]
        assert resumed["scope"].mode == "company_only"
        assert resumed["scope"].report_ids == (_SUPPLEMENT_INDEXED, _SUPPLEMENT_REQUESTED)
        assert resumed["run_id"] == _event(events, "run_started")["run_id"]

    def test_approved_supplement_run_is_persisted_after_resume(self, client, supplement_env):
        """恢复回答同样落盘，历史重开可复核授权与来源。"""
        proposal_events, needed = _propose_supplement(client)
        session_id = _propose_session_id(proposal_events)
        _resolve_supplement(client, needed["supplement_id"], {
            "session_id": session_id, "action": "approve",
            "candidate_ids": _candidate_ids(needed),
        })

        detail = supplement_env["store"].get_session(session_id)
        last = detail["messages"][-1]["run"]
        assert last["status"] == "completed"
        assert last["supplement"]["ingested_report_ids"] == [_SUPPLEMENT_REQUESTED]
        stored_request = supplement_env["store"].get_supplement(needed["supplement_id"])
        assert stored_request["session_id"] == session_id
        assert stored_request["status"] == "completed"
        assert stored_request["consumed_at"]

    def test_declined_supplement_answers_with_existing_evidence_without_download(
        self, client, supplement_env,
    ):
        """拒绝授权时不下钻下载，仍基于现有证据给出回答。"""
        proposal_events, needed = _propose_supplement(client)
        session_id = _propose_session_id(proposal_events)

        events = _resolve_supplement(client, needed["supplement_id"], {
            "session_id": session_id, "action": "decline",
        })

        assert supplement_env["downloader"].calls == []
        assert supplement_env["ingestion"].calls == []
        names = [name for name, _ in events]
        assert names == ["session", "run_started", "delta", "done"]
        done = _event(events, "done")
        assert done["run"]["supplement"]["status"] == "declined"
        assert done["run"]["supplement"]["ingested_report_ids"] == []
        assert done["run"]["status"] == "partial"
        assert supplement_env["rag"].calls[-1]["scope"].report_ids == (_SUPPLEMENT_INDEXED,)
        # 拒绝也必须即时落盘：重开会话/审计读取到的状态不能仍是 proposed。
        stored_request = supplement_env["store"].get_supplement(needed["supplement_id"])
        assert stored_request["session_id"] == session_id
        assert stored_request["status"] == "declined"

    @pytest.mark.parametrize("supplied_ids", [None, [], ["candidate-not-allowed"]])
    def test_decline_rejects_any_present_candidate_ids_without_download(
        self, client, supplement_env, supplied_ids,
    ):
        """decline 只允许省略 candidate_ids；显式 null、空或非空数组均为畸形请求。"""
        proposal_events, needed = _propose_supplement(client)

        response = client.post(
            f"/api/chat/supplements/{needed['supplement_id']}/resolve",
            json={
                "session_id": _propose_session_id(proposal_events),
                "action": "decline",
                "candidate_ids": supplied_ids,
            },
        )

        assert response.status_code == 422
        assert supplement_env["downloader"].calls == []

    def test_approve_rejects_question_changed_after_card_was_proposed(
        self, client, supplement_env,
    ):
        """授权只对提出问题的那次问答有效：期间再提问则拒绝，且不下载、不消费授权。"""
        proposal_events, needed = _propose_supplement(client)
        session_id = _propose_session_id(proposal_events)
        _ask_in_same_session(client, session_id, "600519 2024 年报的分红情况？")

        response = client.post(
            f"/api/chat/supplements/{needed['supplement_id']}/resolve",
            json={"session_id": session_id, "action": "approve",
                  "candidate_ids": _candidate_ids(needed)},
        )

        assert response.status_code == 409
        assert supplement_env["downloader"].calls == []
        assert supplement_env["ingestion"].calls == []
        stored = supplement_env["store"].get_supplement(needed["supplement_id"])
        assert stored["status"] == "proposed"
        assert stored["consumed_at"] == ""
        assert server.supplement_registry.get(needed["supplement_id"]).status == "proposed"

        # 回到原问题后授权仍可正常使用，证明上一步确实没有消费授权。
        _ask_in_same_session(client, session_id, _supplement_question()["question"])
        ok = client.post(
            f"/api/chat/supplements/{needed['supplement_id']}/resolve",
            json={"session_id": session_id, "action": "approve",
                  "candidate_ids": _candidate_ids(needed)},
        )
        assert ok.status_code == 200
        assert supplement_env["downloader"].calls == [_SUPPLEMENT_REQUESTED]

    def test_decline_also_requires_the_original_question(self, client, supplement_env):
        """拒绝同样不能跨问题复用授权；校验必须在状态机之前。"""
        proposal_events, needed = _propose_supplement(client)
        session_id = _propose_session_id(proposal_events)
        _ask_in_same_session(client, session_id, "600519 2024 年报的分红情况？")

        response = client.post(
            f"/api/chat/supplements/{needed['supplement_id']}/resolve",
            json={"session_id": session_id, "action": "decline"},
        )

        assert response.status_code == 409
        assert supplement_env["downloader"].calls == []
        stored = supplement_env["store"].get_supplement(needed["supplement_id"])
        assert stored["status"] == "proposed"

    def test_identical_question_keeps_card_approvable(self, client, supplement_env):
        """同一问题（含首尾空白）沿用同一摘要归一化，授权行为不变。"""
        proposal_events, needed = _propose_supplement(client)
        session_id = _propose_session_id(proposal_events)
        _ask_in_same_session(client, session_id, f"  {_supplement_question()['question']}  ")

        events = _resolve_supplement(client, needed["supplement_id"], {
            "session_id": session_id, "action": "approve",
            "candidate_ids": _candidate_ids(needed),
        })

        assert supplement_env["downloader"].calls == [_SUPPLEMENT_REQUESTED]
        assert _event(events, "done")["run"]["supplement"]["status"] == "completed"

    def test_empty_resume_advances_registry_to_completed(self, client, supplement_env):
        """摄取成功但检索为空：登记表推进到 completed，与运行摘要保持一致。"""
        _, needed = _propose_supplement(client)
        supplement_env["rag"].resume_empty = True

        events = _resolve_supplement(client, needed["supplement_id"], {
            "session_id": supplement_env["store"].list_sessions()[0]["id"],
            "action": "approve",
            "candidate_ids": _candidate_ids(needed),
        })

        assert [name for name, _ in events][-1] == "done"
        done = _event(events, "done")
        assert done["run"]["status"] == "partial"
        assert done["run"]["supplement"]["status"] == "completed"
        assert done["run"]["supplement"]["ingested_report_ids"] == [_SUPPLEMENT_REQUESTED]
        assert server.supplement_registry.get(needed["supplement_id"]).status == "completed"
        stored = supplement_env["store"].get_supplement(needed["supplement_id"])
        assert stored["status"] == "completed"

    def test_partial_failure_resumes_with_only_ingested_report_ids(
        self, client, supplement_env, monkeypatch,
    ):
        """部分候选失败：只用摄取成功的报告恢复回答，并列出失败项。"""
        failed_report = "601288:2024-06-30:semi_annual"
        supplement_env["downloader"].statuses[failed_report] = DownloadStatus.FAILED
        supplement_env["metas"].append(_supplement_meta(date(2024, 6, 30), ReportType.SEMI_ANNUAL))
        supplement_env["rag"].needs = [
            {"period": "2025-06-30", "report_type": "semi_annual"},
            {"period": "2024-06-30", "report_type": "semi_annual"},
        ]
        proposal_events, needed = _propose_supplement(client)
        assert len(needed["candidates"]) == 2

        events = _resolve_supplement(client, needed["supplement_id"], {
            "session_id": _propose_session_id(proposal_events),
            "action": "approve",
            "candidate_ids": _candidate_ids(needed),
        })

        supplement = _event(events, "done")["run"]["supplement"]
        assert supplement["ingested_report_ids"] == [_SUPPLEMENT_REQUESTED]
        assert [item["reason"] for item in supplement["failed"]] == ["download_failed"]
        assert failed_report not in json.dumps(supplement, ensure_ascii=False)
        resumed = supplement_env["rag"].calls[-1]
        assert resumed["scope"].report_ids == (_SUPPLEMENT_INDEXED, _SUPPLEMENT_REQUESTED)
        assert _event(events, "supplement_failed")["reason"] == "download_failed"

    def test_ingest_failure_does_not_enter_resumed_scope(self, client, supplement_env):
        """下载成功但没有 PDF 索引证据时不算补充成功，不进入恢复范围。"""
        supplement_env["ingestion"].failing = {"农业银行_601288_半年报_2025.pdf"}
        proposal_events, needed = _propose_supplement(client)

        events = _resolve_supplement(client, needed["supplement_id"], {
            "session_id": _propose_session_id(proposal_events),
            "action": "approve",
            "candidate_ids": _candidate_ids(needed),
        })

        names = [name for name, _ in events]
        assert "supplement_ingested" not in names
        assert _event(events, "supplement_failed")["reason"] == "ingest_failed"
        done = _event(events, "done")
        assert done["run"]["supplement"]["status"] == "failed"
        assert done["run"]["supplement"]["ingested_report_ids"] == []
        assert supplement_env["rag"].calls[-1]["scope"].report_ids == (_SUPPLEMENT_INDEXED,)

    def test_all_failures_still_produce_truthful_answer_not_500(
        self, client, supplement_env,
    ):
        """全部失败也必须给出可读回答与失败说明，而不是 500。"""
        supplement_env["downloader"].statuses[_SUPPLEMENT_REQUESTED] = DownloadStatus.FAILED
        proposal_events, needed = _propose_supplement(client)

        response = client.post(
            f"/api/chat/supplements/{needed['supplement_id']}/resolve",
            json={
                "session_id": _propose_session_id(proposal_events),
                "action": "approve",
                "candidate_ids": _candidate_ids(needed),
            },
        )

        assert response.status_code == 200
        events = _read_sse(response)
        done = _event(events, "done")
        assert done["run"]["status"] == "partial"
        assert done["run"]["supplement"]["status"] == "failed"
        assert [item["reason"] for item in done["run"]["supplement"]["failed"]] == ["download_failed"]

    def test_resume_error_marks_supplement_failed_and_never_500(
        self, client, supplement_env,
    ):
        """恢复流错误须脱敏，并使补充摘要据实记为失败。"""
        private_marker = "仅用于补报恢复 SSE 泄露测试的敏感文本"
        proposal_events, needed = _propose_supplement(client)
        supplement_env["rag"].resume_error = private_marker

        events = _resolve_supplement(client, needed["supplement_id"], {
            "session_id": _propose_session_id(proposal_events),
            "action": "approve",
            "candidate_ids": _candidate_ids(needed),
        })

        assert [name for name, _ in events][-1] == "error"
        error = _event(events, "error")
        run = error["run"]
        assert error["error"] == f"继续回答时遇到问题，请稍后重试。（反馈编号：{run['id']}）"
        assert private_marker not in str(events)
        assert run["status"] == "failed"
        assert run["supplement"]["status"] == "failed"
        assert run["supplement"]["ingested_report_ids"] == [_SUPPLEMENT_REQUESTED]

    def test_cross_session_approval_returns_409_without_download(
        self, client, supplement_env,
    ):
        """其他会话不能消费本会话的补报授权。"""
        proposal_events, needed = _propose_supplement(client)
        other = supplement_env["store"].create_session()

        response = client.post(
            f"/api/chat/supplements/{needed['supplement_id']}/resolve",
            json={"session_id": other["id"], "action": "approve",
                  "candidate_ids": _candidate_ids(needed)},
        )

        assert response.status_code == 409
        assert supplement_env["downloader"].calls == []

    def test_replayed_approval_returns_409_without_second_download(
        self, client, supplement_env,
    ):
        """一次性授权：重复提交不得再次下载。"""
        proposal_events, needed = _propose_supplement(client)
        session_id = _propose_session_id(proposal_events)
        body = {"session_id": session_id, "action": "approve",
                "candidate_ids": _candidate_ids(needed)}

        first = client.post(f"/api/chat/supplements/{needed['supplement_id']}/resolve", json=body)
        assert first.status_code == 200
        downloads_after_first = list(supplement_env["downloader"].calls)

        second = client.post(f"/api/chat/supplements/{needed['supplement_id']}/resolve", json=body)
        assert second.status_code == 409
        assert supplement_env["downloader"].calls == downloads_after_first

    def test_expired_supplement_returns_409_without_download(self, client, supplement_env, monkeypatch):
        """超过有效期的授权不能下载。"""
        from datetime import datetime as _datetime

        from webapp.chat_supplement import SupplementRegistry

        clock = {"now": _datetime(2026, 9, 14, 10, 0, 0)}
        monkeypatch.setattr(server, "supplement_registry",
                            SupplementRegistry(clock=lambda: clock["now"], ttl_seconds=600))
        proposal_events, needed = _propose_supplement(client)
        clock["now"] = _datetime(2026, 9, 14, 12, 0, 0)

        response = client.post(
            f"/api/chat/supplements/{needed['supplement_id']}/resolve",
            json={"session_id": _propose_session_id(proposal_events),
                  "action": "approve", "candidate_ids": _candidate_ids(needed)},
        )

        assert response.status_code == 409
        assert supplement_env["downloader"].calls == []

    def test_tampered_and_empty_and_duplicate_selections_return_409(
        self, client, supplement_env,
    ):
        """候选篡改、空选择与重复选择都不得下载。"""
        proposal_events, needed = _propose_supplement(client)
        session_id = _propose_session_id(proposal_events)
        candidate_id = _candidate_ids(needed)[0]

        for selection in (["not-a-candidate"], [], [candidate_id, candidate_id]):
            response = client.post(
                f"/api/chat/supplements/{needed['supplement_id']}/resolve",
                json={"session_id": session_id, "action": "approve", "candidate_ids": selection},
            )
            assert response.status_code == 409, selection

        assert supplement_env["downloader"].calls == []

    def test_unknown_supplement_id_returns_404_without_download(self, client, supplement_env):
        """未知授权请求不得触发下载。"""
        response = client.post(
            "/api/chat/supplements/unknown-id/resolve",
            json={"session_id": "whatever", "action": "approve", "candidate_ids": ["c1"]},
        )

        assert response.status_code == 404
        assert supplement_env["downloader"].calls == []

    def test_candidates_are_capped_at_five(self, client, supplement_env):
        """候选与授权上限都是 5 份。"""
        many = [
            _supplement_meta(date(2023, 6, 30), ReportType.SEMI_ANNUAL),
            _supplement_meta(date(2022, 6, 30), ReportType.SEMI_ANNUAL),
            _supplement_meta(date(2021, 6, 30), ReportType.SEMI_ANNUAL),
            _supplement_meta(date(2020, 6, 30), ReportType.SEMI_ANNUAL),
            _supplement_meta(date(2019, 6, 30), ReportType.SEMI_ANNUAL),
            _supplement_meta(date(2018, 6, 30), ReportType.SEMI_ANNUAL),
            _supplement_meta(date(2017, 6, 30), ReportType.SEMI_ANNUAL),
        ]
        supplement_env["metas"].extend(many)

        proposal_events, needed = _propose_supplement(client)

        assert len(needed["candidates"]) == 5
        assert needed["limit"] == 5
        session_id = _propose_session_id(proposal_events)
        ok = client.post(
            f"/api/chat/supplements/{needed['supplement_id']}/resolve",
            json={"session_id": session_id, "action": "approve",
                  "candidate_ids": _candidate_ids(needed)},
        )
        assert ok.status_code == 200
        assert len(supplement_env["downloader"].calls) == 5
        assert len(supplement_env["rag"].calls[-1]["scope"].report_ids) == 6

    def test_unresolved_financial_scope_is_clarified_without_source_calls(
        self, client, supplement_env,
    ):
        """未给公司身份的财务问句先澄清，不访问来源或触发补报。"""
        events = _read_sse(client.post("/api/chat/stream", json={"question": "经营现金流怎么看？"}))

        names = [name for name, _ in events]
        assert "supplement_needed" not in names
        assert "clarification" in names
        done = _event(events, "done")
        assert done["clarification"] is True
        assert "明确公司名称" in done["answer"]
        assert done["run"] is None
        assert supplement_env["downloader"].calls == []

    def test_supplement_request_without_candidates_answers_with_gap_note(
        self, client, supplement_env,
    ):
        """单公司范围内查不到可下载报告时，说明真实原因是不存在可补充的报告。"""
        supplement_env["metas"].clear()

        events = _read_sse(client.post("/api/chat/stream", json=_supplement_question()))

        names = [name for name, _ in events]
        assert "supplement_needed" not in names
        done = _event(events, "done")
        assert done["answer"] == server._SUPPLEMENT_UNAVAILABLE_TEXT
        assert done["run"]["status"] == "partial"
        assert supplement_env["downloader"].calls == []

    def test_chat_tool_defs_includes_supplement_request_tool(self, monkeypatch):
        """问答工具定义必须包含受控补报工具，否则模型无法申请授权。"""
        class FakeSearch:
            available = False

            def __init__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(server, "TavilyWebSearch", FakeSearch)
        monkeypatch.setattr(server, "DeepSeekWebSearch", FakeSearch)
        monkeypatch.setattr(server, "_mcp_tool_defs", lambda: None)
        cfg = type("C", (), {"mcp_tools": True, "web_search": True, "web_search_timeout": 15})()

        defs = server._build_chat_tool_defs(cfg)

        assert [item["function"]["name"] for item in defs] == ["request_missing_reports"]

    def test_supplement_handler_rejects_calls_outside_a_stream(self):
        """没有请求上下文时补报处理器必须拒绝，避免全局状态误授权。"""
        assert server._handle_supplement_request({"reason": "x", "needs": []}) is False

    def test_init_rag_keeps_a_tool_executor_when_mcp_and_web_unavailable(
        self, monkeypatch, tmp_path,
    ):
        """MCP 与网页搜索都不可用时仍保留执行器占位，补报工具才可达。"""
        from types import SimpleNamespace

        fake_cfg = SimpleNamespace(
            enabled=True, store_path=str(tmp_path), chunk_size=800, chunk_overlap=100,
            top_k=8, embedding_model="fake-model", auto_ingest=True, enhanced_analysis=False,
            mcp_tools=True, web_search=True, web_search_timeout=15,
        )
        monkeypatch.setattr(server, "RagConfig", type("C", (), {"load": staticmethod(lambda: fake_cfg)}))

        captured = {}

        class FakeEmbedder:
            def __init__(self, *args, **kwargs):
                pass

        class FakeRagStore:
            def __init__(self, *args, **kwargs):
                pass

        class FakeSvc:
            def __init__(self, *args, **kwargs):
                pass

        class FakeQA:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

        class FakeSearch:
            available = False

            def __init__(self, *args, **kwargs):
                pass

        monkeypatch.setattr(server, "LocalEmbedder", FakeEmbedder)
        monkeypatch.setattr(server, "RagStore", FakeRagStore)
        monkeypatch.setattr(server, "IngestionService", FakeSvc)
        monkeypatch.setattr(server, "RagQA", FakeQA)
        monkeypatch.setattr(server, "TavilyWebSearch", FakeSearch)
        monkeypatch.setattr(server, "DeepSeekWebSearch", FakeSearch)
        monkeypatch.setattr(server, "_mcp_tool_defs", lambda: None)

        orig = (server.rag_store, server.rag_service, server.rag_qa)
        try:
            server._init_rag()
        finally:
            server.rag_store, server.rag_service, server.rag_qa = orig

        assert captured["tool_executor"] is not None
        assert captured["tool_executor"]("get_realtime_quote", {}) != ""
        assert captured["supplement_request_handler"] is server._handle_supplement_request
        # Scope 校验必须复用执行器的名称→代码解析路径，否则名称形式的身份参数可绕过范围。
        assert captured["company_code_resolver"] is server._resolve_symbol_code


def test_general_knowledge_skips_planner_and_sources_and_persists_unretrieved_label(client, env, monkeypatch):
    from financial_report_fetcher.rag.qa import RagQA

    calls = []
    class NoStore:
        def query(self, *_args, **_kwargs):
            raise AssertionError("knowledge path must not retrieve")
    class AnswerAI:
        def chat_stream(self, **kwargs):
            calls.append(kwargs)
            yield {"type": "done", "answer": "市盈率是价格与每股收益的比值。", "model": "fixture", "usage": {}}
    class NoPlanner:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("knowledge path must not plan with model")
    monkeypatch.setattr(server, "ExecutionPlanner", NoPlanner)
    monkeypatch.setattr(server, "rag_qa", RagQA(NoStore(), AnswerAI()))
    monkeypatch.setattr(server, "_build_chat_tool_executor", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not build tools")))

    events = _read_sse(client.post("/api/chat/stream", json={"question": "什么是市盈率？", "scope_mode": "whole_corpus"}))
    run = _event(events, "done")["run"]
    assert run["knowledge_basis"] == "model_knowledge_unretrieved"
    assert run["facts"] == run["artifacts"] == run["tool_artifacts"] == []
    assert run["execution_plan"]["source_mode"] == "general_knowledge"
    assert len(calls) == 1 and calls[0].get("tools") is None


def test_stream_waits_for_completed_generation_before_persisting_done(client, env, monkeypatch):
    from financial_report_fetcher.rag.qa import RagQA

    calls = []

    class NoStore:
        def query(self, *_args, **_kwargs):
            raise AssertionError("knowledge path must not retrieve")

    class InterruptedAnswerAI:
        def chat_stream(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                yield {"type": "delta", "text": "市盈率反映价格与盈利关系", "reasoning": ""}
                yield {"type": "done", "answer": "市盈率反映价格与盈利关系", "finish_reason": "length",
                       "model": "fixture", "usage": {}}
            else:
                yield {"type": "delta", "text": "，常用于比较估值水平。", "reasoning": ""}
                yield {"type": "done", "answer": "，常用于比较估值水平。", "finish_reason": "stop",
                       "model": "fixture", "usage": {}}

    monkeypatch.setattr(server, "rag_qa", RagQA(NoStore(), InterruptedAnswerAI()))
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "什么是市盈率？", "scope_mode": "whole_corpus",
    }))

    done = _event(events, "done")
    assert sum(name == "done" for name, _ in events) == 1
    assert len(calls) == 2
    assert any(name == "reasoning_stage" and data["message"] == "正在补全回答" for name, data in events)
    assert done["run"]["status"] == "completed"
    assert done["run"]["content"] == "市盈率反映价格与盈利关系，常用于比较估值水平。"


def test_exhausted_completion_persists_notice_instead_of_partial_fragment(client, env, monkeypatch):
    from financial_report_fetcher.rag.qa import RagQA

    class NoStore:
        def query(self, *_args, **_kwargs):
            raise AssertionError("knowledge path must not retrieve")

    class AlwaysInterruptedAI:
        def __init__(self):
            self.calls = 0

        def chat_stream(self, **_kwargs):
            self.calls += 1
            fragment = "答案仍未写完，"
            yield {"type": "delta", "text": fragment, "reasoning": ""}
            yield {"type": "done", "answer": fragment, "finish_reason": "length",
                   "model": "fixture", "usage": {}}

    ai = AlwaysInterruptedAI()
    monkeypatch.setattr(server, "rag_qa", RagQA(NoStore(), ai))
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "什么是市盈率？", "scope_mode": "whole_corpus",
    }))

    done = _event(events, "done")
    assert ai.calls == 3
    assert done["run"]["status"] == "partial"
    assert done["run"]["content"] == "回答未能完整生成，请重试。"
    assert "答案仍未写完" not in done["run"]["content"]


def test_current_policy_without_authorized_web_uses_bounded_finance_knowledge(client, env, monkeypatch):
    from webapp.chat_intent import ChatIntentAnalysis

    answer_calls = []
    class KnowledgeRag:
        def answer_from_context(self, question, **kwargs):
            answer_calls.append((question, kwargs))
            yield {"type": "done", "answer": "无法核实当前政策；可先关注政策目标、适用范围和执行时间。",
                   "citations": [], "model": "fixture", "usage": {}}

    monkeypatch.setattr(server, "_analyze_chat_intent", lambda *_args: ChatIntentAnalysis(
        "financial_knowledge", "other", "", "", "research", "当前", False, "",
    ))
    monkeypatch.setattr(server, "rag_qa", KnowledgeRag())
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: [])
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "当前政策是什么？", "use_mcp": False,
    }))
    done = _event(events, "done")
    assert "无法核实当前政策" in done["answer"]
    assert answer_calls[0][1]["knowledge_mode"] is True
    assert "外部研究来源" in answer_calls[0][1]["knowledge_notice"]
    assert not any(name == "tool_call" for name, _ in events)


def test_financial_definition_uses_model_knowledge_without_report_or_market_tools(client, env, monkeypatch):
    from webapp.chat_intent import ChatIntentAnalysis

    answer_calls, tool_builds = [], []
    class KnowledgeRag:
        def answer_from_context(self, question, **kwargs):
            answer_calls.append((question, kwargs))
            yield {"type": "done", "answer": "市盈率是股价与每股收益的比值，用于衡量估值水平。",
                   "citations": [], "model": "fixture", "usage": {}}

    monkeypatch.setattr(server, "_analyze_chat_intent", lambda *_args: ChatIntentAnalysis(
        "financial_knowledge", "none", "", "", "explain", "", False, "",
    ))
    monkeypatch.setattr(server, "rag_qa", KnowledgeRag())
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: tool_builds.append(True))
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "什么是市盈率？",
        "focus_report": {"code": "601288", "period": "2026-06-30"},
    }))
    done = _event(events, "done")
    assert "市盈率是股价" in done["answer"]
    assert answer_calls[0][1]["knowledge_mode"] is True
    assert tool_builds == []


def test_market_request_uses_server_frozen_source_not_model_raw_tool_payload(client, env, monkeypatch):
    class RawToolRag:
        def answer_stream(self, question, **kwargs):
            raise AssertionError("frozen market execution must not accept model-selected tool calls")

        def answer_from_context(self, *_args, **_kwargs):
            yield {"type": "done", "answer": "仅供参考。", "citations": [], "model": "m", "usage": {}, "tools_used": [], "retrieval_report_ids": [], "retrieval_degraded": False}

    monkeypatch.setattr(server, "rag_qa", RawToolRag())
    monkeypatch.setattr(server.tencent_quote, "realtime", lambda _symbols: [])
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "农业银行今天行情如何？",
        "focus_report": {"code": "601288", "name": "农业银行", "period": "2026-06-30"},
        "use_mcp": False,
    }))

    assert any(name == "done" for name, _ in events), _event(events, "error")
    run = _event(events, "done")["run"]
    assert run["facts"] == []
    assert run["execution_plan"]["source_mode"] == "external_market"
    assert run["execution_steps"][0]["kind"] == "market_quote"


def test_pdf_citations_are_projected_to_literal_scoped_facts_before_verification(monkeypatch):
    from webapp.chat_models import EvidenceArtifact, Scope

    report_id = "601288:2026-06-30:semi_annual"
    artifact = EvidenceArtifact.pdf(report_id, "report.pdf", 40, "营业收入为 100 亿元")
    scope = Scope.company_only("601288", "农业银行", [report_id])

    class FakeNormalizer:
        def normalize_rag_citations(self, *_args, **_kwargs):
            return (artifact,)

        def normalize_web_sources(self, *_args, **_kwargs):
            return ()

    state = server._RagRunState(scope=scope)
    frames = server._relay_rag_event({
        "type": "done", "answer": "营业收入为 100 亿元", "citations": [],
        "retrieval_report_ids": [], "retrieval_degraded": False,
    }, state, FakeNormalizer())

    assert len(state.facts) == 1
    assert state.facts[0].source_category == "local_pdf"
    assert state.facts[0].value == 100
    assert any(frame.startswith("event: fact") for frame in frames)


def test_m2_policy_is_emitted_and_unsupported_numeric_is_degraded(client, env, monkeypatch):
    class PolicyRag:
        def answer_stream(self, question, **kwargs):
            assert kwargs["tool_policy"].intent == "report_fact"
            yield {"type": "done", "answer": "营收为 100 亿元", "citations": [], "model": "m", "usage": {}, "tools_used": [], "retrieval_report_ids": [], "retrieval_degraded": False}

    monkeypatch.setattr(server, "rag_qa", PolicyRag())
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "半年报营收多少？",
        "focus_report": {"code": "601288", "name": "农业银行", "period": "2026-06-30"},
    }))
    assert _event(events, "policy_resolved")["intent"] == "report_fact"
    assert _event(events, "verification")["verification"]["status"] == "blocked"
    assert "没有找到可核实的公开披露" in _event(events, "done")["run"]["content"]


def test_m2_mcp_financial_reference_cannot_support_report_fact_claims(client, env, monkeypatch):
    """财务 MCP 参考值不能替代 PDF 对半年报事实的核验。"""
    class MixedClaimRag:
        def answer_stream(self, question, **kwargs):
            yield {"type": "tool_call", "name": "get_financial_metrics", "arguments": {"symbol": "601288"}}
            yield {"type": "structured_tool_result", "name": "get_financial_metrics", "payload": {
                "metric": "revenue", "value": 100, "unit": "亿元", "period": "2026-06-30",
                "period_kind": "semi_annual_cumulative", "entity_scope": "consolidated",
                "company_code": "601288", "evidence_ids": ["tool:revenue:601288"],
            }, "ok": True}
            yield {"type": "tool_result", "name": "get_financial_metrics", "ok": True, "summary": "{}"}
            yield {"type": "done", "answer": "外部参考：营业收入为 100 亿元，经营活动现金流量净额为 621 亿元。",
                   "citations": [], "model": "m", "usage": {}, "tools_used": ["get_financial_metrics"],
                   "retrieval_report_ids": [], "retrieval_degraded": False}

    monkeypatch.setattr(server, "rag_qa", MixedClaimRag())
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "农业银行半年报营收和经营现金流多少？",
        "focus_report": {"code": "601288", "name": "农业银行", "period": "2026-06-30"},
        "use_mcp": True,
    }))

    run = _event(events, "done")["run"]
    assert run["verification_report"]["status"] == "blocked"
    assert "100 亿元" not in run["content"]
    assert "621" not in run["content"]
    assert run["content"].count("没有找到可核实的公开披露") == 1


def test_resume_history_passes_full_safe_history_to_budget_projection():
    history = []
    for index in range(10):
        history.extend([
            {"role": "user", "content": f"用户轮次 {index}"},
            {"role": "assistant", "content": f"助手轮次 {index}"},
        ])
    history.append({"role": "user", "content": "恢复问题"})

    projected_input = server._resume_history({"messages": history})

    assert len(projected_input) == 20
    assert projected_input[0]["content"] == "用户轮次 0"
    assert projected_input[-1]["content"] == "助手轮次 9"


def test_research_resume_origin_passes_full_prior_history_to_budget_projection():
    from webapp.chat_models import ToolPolicy

    messages = []
    for index in range(10):
        messages.extend([
            {"role": "user", "content": f"用户轮次 {index}"},
            {"role": "assistant", "content": f"助手轮次 {index}"},
        ])
    messages.extend([
        {"role": "user", "content": "研究公司 601288 2026 半年报"},
        {"role": "assistant", "run": {
            "research_run_id": "r1", "tool_policy": ToolPolicy(intent="research_task").to_dict(),
        }},
    ])

    origin = server._research_resume_origin({"messages": messages}, "r1")

    assert origin.question == "研究公司 601288 2026 半年报"
    assert len(origin.history) == 20
    assert origin.history[0]["content"] == "用户轮次 0"


def test_sse_event_pump_producer_waits_for_bounded_async_queue_capacity():
    import asyncio
    from threading import Thread

    async def scenario():
        pump = server._SseEventPump(asyncio.get_running_loop(), "fixture")
        pump.queue = asyncio.Queue(maxsize=1)
        first = Thread(target=pump.put, args=({"n": 1},))
        first.start()
        for _ in range(50):
            if pump.queue.qsize() == 1:
                break
            await asyncio.sleep(0.01)
        assert pump.queue.qsize() == 1
        second = Thread(target=pump.put, args=({"n": 2},))
        second.start()
        await asyncio.sleep(0.05)
        assert second.is_alive(), "producer must wait instead of queueing an unbounded put coroutine"
        assert await pump.queue.get() == {"n": 1}
        await asyncio.to_thread(second.join, 1)
        assert not second.is_alive()
        assert await pump.queue.get() == {"n": 2}
        pump.stop()

    asyncio.run(scenario())


def test_cancel_chat_run_requires_matching_session_and_stops_active_worker(client, env, monkeypatch):
    from threading import Event

    from webapp.chat_runs import ChatRunControl, ChatRunRegistry

    registry = ChatRunRegistry(max_workers=1)
    started = Event()
    release = Event()
    control = ChatRunControl("session-a", "run-a", timeout_seconds=10)

    def blocking(_control):
        started.set()
        release.wait(2)

    monkeypatch.setattr(server, "chat_run_registry", registry)
    assert registry.start(control, blocking)
    assert started.wait(1)
    try:
        denied = client.post("/api/chat/runs/run-a/cancel", json={"session_id": "session-b"})
        assert denied.status_code == 404
        assert control.cancelled is False

        accepted = client.post("/api/chat/runs/run-a/cancel", json={"session_id": "session-a"})
        assert accepted.status_code == 200
        assert accepted.json() == {"cancelled": True, "status": "stopping"}
        assert control.cancelled is True
    finally:
        release.set()
        registry.wait("run-a", timeout=1)
        registry.finish("run-a")
        registry.shutdown()


def test_slow_chat_planning_does_not_block_health_or_sse_run_identity(client, env, monkeypatch):
    from queue import Queue
    from threading import Event, Thread

    from webapp.execution_plan import PlanIssue
    from webapp.execution_planner import PlanningResult

    planner_started = Event()
    release_planner = Event()
    health_done = Event()
    observed = Queue()
    sse_events = Queue()

    from webapp.chat_intent import ChatIntentAnalysis
    monkeypatch.setattr(server, "_analyze_chat_intent", lambda *_args: ChatIntentAnalysis(
        "market_analysis", "broad_market", "A股", "", "recap", "今日", False, "",
    ))

    class SlowPlanner:
        def __init__(self, *_args, **_kwargs):
            pass

        def plan(self, *_args, **_kwargs):
            planner_started.set()
            release_planner.wait(2)
            return PlanningResult("fallback", issues=(PlanIssue("fixture", "fixture"),))

    monkeypatch.setattr(server, "ExecutionPlanner", SlowPlanner)
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_chat_qa_or_degraded", lambda: object())

    def request_stream():
        try:
            with client.stream("POST", "/api/chat/stream", json={"question": "一个慢规划的普通问题", "use_mcp": False}) as response:
                current_event = ""
                for line in response.iter_lines():
                    text = line.decode() if isinstance(line, bytes) else line
                    if text.startswith("event: "):
                        current_event = text.removeprefix("event: ")
                    elif text.startswith("data: "):
                        sse_events.put((current_event, text.removeprefix("data: ")))
                        if current_event == "run_started":
                            return
        except Exception as exc:
            sse_events.put(("exception", repr(exc)))

    request_thread = Thread(target=request_stream, daemon=True)
    request_thread.start()
    try:
        assert planner_started.wait(1)

        def health_request():
            try:
                observed.put(client.get("/api/health").status_code)
            finally:
                health_done.set()

        health_thread = Thread(target=health_request, daemon=True)
        health_thread.start()
        assert health_done.wait(0.25), "a blocking planner must not block the ASGI event loop"
    finally:
        release_planner.set()
        request_thread.join(timeout=3)
    events = [sse_events.get_nowait(), sse_events.get_nowait()]
    assert [item[0] for item in events] == ["session", "run_started"]
    import json
    assert json.loads(events[0][1])["session_id"]
    assert json.loads(events[1][1])["run_id"]


def test_broad_market_recap_gets_bounded_source_timeout_without_extending_other_chat_runs():
    from webapp.chat_intent import ChatIntentAnalysis

    broad_recap = ChatIntentAnalysis(
        "market_analysis", "broad_market", "A股", "", "recap", "今日", False, "",
    )
    ordinary = ChatIntentAnalysis(
        "financial_knowledge", "none", "", "", "explain", "", False, "",
    )
    research = ChatIntentAnalysis(
        "market_analysis", "broad_market", "A股", "", "research", "今日", False, "",
    )

    assert server._chat_timeout_for_intent(broad_recap) == server.CHAT_MARKET_RECAP_TIMEOUT_SECONDS
    assert server.CHAT_MARKET_RECAP_TIMEOUT_SECONDS > server.CHAT_RUN_TIMEOUT_SECONDS
    assert server._chat_timeout_for_intent(ordinary) == server.CHAT_RUN_TIMEOUT_SECONDS
    assert server._chat_timeout_for_intent(research) == server.CHAT_RESEARCH_TIMEOUT_SECONDS


def test_chat_run_deadline_persists_failed_run_after_planning_timeout(client, env, monkeypatch, tmp_path):
    import time

    from webapp.chat_store import ChatStore
    from webapp.execution_plan import PlanIssue
    from webapp.execution_planner import PlanningResult

    store = ChatStore(str(tmp_path / "deadline-sessions.json"))
    monkeypatch.setattr(server, "chat_store", store)
    monkeypatch.setattr(server, "CHAT_RUN_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(server, "CHAT_MARKET_RECAP_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_chat_qa_or_degraded", lambda: object())

    from webapp.chat_intent import ChatIntentAnalysis
    monkeypatch.setattr(server, "_analyze_chat_intent", lambda *_args: ChatIntentAnalysis(
        "market_analysis", "broad_market", "A股", "", "recap", "今日", False, "",
    ))

    class SlowPlanner:
        def __init__(self, *_args, **_kwargs):
            pass

        def plan(self, *_args, **_kwargs):
            time.sleep(1.1)
            return PlanningResult("fallback", issues=(PlanIssue("fixture", "fixture"),))

    monkeypatch.setattr(server, "ExecutionPlanner", SlowPlanner)
    response = client.post("/api/chat/stream", json={"question": "一个慢规划的普通问题", "use_mcp": False})
    events = _read_sse(response)
    terminal = _event(events, "done")
    assert terminal["run"]["status"] == "failed"
    stored = store.get_session(terminal["session_id"])
    assert stored["messages"][-1]["run"]["status"] == "failed"
    assert terminal["run"]["content"]


def test_explicit_research_route_survives_successful_ordinary_plan(client, env, monkeypatch):
    from webapp.chat_models import AnswerRun
    from webapp.execution_plan import ExecutionPlan, ExecutionStep
    from webapp.execution_planner import PlanningResult

    planned = ExecutionPlan("用户研究任务", "local_evidence", (
        ExecutionStep("retrieve", "retrieve", True), ExecutionStep("answer", "answer", True),
    ), ("引用范围内来源",))

    class SuccessfulPlanner:
        def __init__(self, *_args, **_kwargs):
            pass

        def plan(self, *_args, **_kwargs):
            return PlanningResult("validated", planned)

    class RecordingResearchAgent:
        last_run = None

        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, question, scope, intent, policy, *, stop_event, emit):
            calls.append((question, intent.intent, policy.intent))
            emit({"type": "research_plan", "plan": {"objective": "研究任务", "steps": [], "acceptance": ["有来源"]}})
            return AnswerRun(content="研究结果", status="completed", scope=scope,
                             intent_decision=intent, tool_policy=policy)

    calls = []
    monkeypatch.setattr(server, "ExecutionPlanner", SuccessfulPlanner)
    monkeypatch.setattr(server, "ResearchAgent", RecordingResearchAgent)
    monkeypatch.setattr(server, "_chat_qa_or_degraded", lambda: object())
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda *_args, **_kwargs: None)

    events = _read_sse(client.post("/api/chat/stream", json={"question": "请研究一下公司经营情况"}))

    assert calls == [("请研究一下公司经营情况", "research_task", "research_task")]
    assert _event(events, "policy_resolved")["intent"] == "research_task"
    assert _event(events, "research_plan")["plan"]["objective"] == "研究任务"
    assert _event(events, "done")["run"]["content"] == "研究结果"


def test_research_task_uses_policy_gated_rag_evidence_and_public_step_events(client, env):
    """M3 不得用空产物伪造完成：生产 RAG 产物进入可恢复研究步骤。"""
    _configure_scoped_rag_answer(env, citations=[{
        "source": "pdf", "report_id": "601288:2026-06-30:semi_annual", "page": 1, "snippet": "经营现金流披露"
    }])
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "帮我制定农业银行的研究计划",
        "focus_report": {"code": "601288", "name": "农业银行", "period": "2026-06-30"},
    }))

    assert _event(events, "policy_resolved")["intent"] == "research_task"
    fallback = _event(events, "policy_fallback")
    assert fallback["intent"] == "research_task"
    assert "M3" not in fallback["message"]
    assert _event(events, "research_plan")["plan"]["acceptance"]
    assert _event(events, "research_step_started")["status"] == "running"
    assert _event(events, "research_step_completed")["evidence_count"] >= 1
    run = _event(events, "done")["run"]
    assert run["status"] == "completed"
    assert run["research_run_id"]
    assert run["artifacts"]
    assert run["content"] == "经营现金流为"
    stored = client.get(f"/api/chat/research/{run['research_run_id']}",
                        params={"session_id": _event(events, "session")["session_id"]}).json()["run"]
    assert [step["status"] for step in stored["step_runs"]] == ["completed"] * len(stored["step_runs"])
    assert stored["step_runs"][0]["artifacts"]


def test_resume_does_not_repeat_a_completed_retrieve_step(client, env, monkeypatch, tmp_path):
    """恢复只重跑未完成步骤：已完成检索的外部调用不得再发生一次。"""
    from webapp.chat_models import AnswerRun, ToolPolicy
    from webapp.chat_store import ChatStore
    from webapp.research_models import ResearchPlan, ResearchRun, ResearchStep, ResearchStepRun
    from webapp.chat_models import Scope

    store = ChatStore(str(tmp_path / "sessions.json"))
    monkeypatch.setattr(server, "chat_store", store)
    calls = []

    class CountingRag:
        def answer_stream(self, question, **kwargs):
            calls.append(question)
            yield {"type": "done", "answer": "重新检索得到的结论", "citations": [], "model": "m", "usage": {},
                   "tools_used": [], "web_sources": [], "retrieval_report_ids": [], "retrieval_degraded": False}

    monkeypatch.setattr(server, "rag_qa", CountingRag())
    scope = Scope.company_only("601288", "农业银行", ["601288:2026-06-30:semi_annual"])
    plan = ResearchPlan("研究", scope, (
        ResearchStep("retrieve", "retrieve", "检索已授权披露"),
        ResearchStep("normalize", "normalize", "整理可核验事实", ("retrieve",)),
        ResearchStep("compare", "compare", "比较关键指标", ("normalize",)),
        ResearchStep("verify", "verify", "核对来源与结论", ("compare",)),
        ResearchStep("answer", "answer", "形成研究结论", ("verify",)),
    ), ("核对来源",))
    evidence = {"source": "pdf", "report_id": "601288:2026-06-30:semi_annual", "pdf_filename": "a.pdf",
                "page": 1, "snippet": "已核验披露"}
    run = ResearchRun("saved-run", plan, "stopped", (
        ResearchStepRun("retrieve", "completed", result_summary="经营现金流情况见已核验披露。", artifacts=(evidence,)),
        ResearchStepRun("normalize", "stopped"),
    ), resume_from_step_id="normalize")
    sid = store.create_session()["id"]
    store.save_research_run(sid, run)
    store.append_turn(sid, question="比较农业银行盈利质量", run=AnswerRun(
        content="研究已停止；已完成步骤已保存，可继续研究。", status="stopped", scope=scope,
        tool_policy=ToolPolicy("research_task"), research_run_id="saved-run",
        research_summary={"status": "stopped", "resume_from_step_id": "normalize"},
    ))

    response = client.post("/api/chat/research/saved-run/resume", json={"session_id": sid})

    assert response.status_code == 200
    events = _read_sse(response)
    assert calls == []
    assert _event(events, "done")["run"]["status"] == "completed"
    assert _event(events, "done")["run"]["artifacts"]
    assert _event(events, "done")["run"]["content"] == "经营现金流情况见已核验披露。"


_RESEARCH_RESUME_CITATION = {
    "source": "pdf", "report_id": "601288:2026-06-30:semi_annual", "page": 1, "snippet": "范围内披露来源",
}


class _Frames:
    """直接驱动恢复生成器时复用 ``_read_sse`` 的最小响应壳。"""

    def __init__(self, text: str) -> None:
        self.text = text


def _stopped_research_plan():
    """恢复回归共用的五步计划：retrieve 未完成，其余步骤依赖 retrieve。"""
    from webapp.chat_models import Scope
    from webapp.research_models import ResearchPlan, ResearchStep

    scope = Scope.company_only("601288", "农业银行", ["601288:2026-06-30:semi_annual"])
    return ResearchPlan("比较农业银行盈利质量", scope, (
        ResearchStep("retrieve", "retrieve", "检索已授权披露"),
        ResearchStep("normalize", "normalize", "整理可核验事实", ("retrieve",)),
        ResearchStep("compare", "compare", "比较关键指标", ("normalize",)),
        ResearchStep("verify", "verify", "核对来源与结论", ("compare",)),
        ResearchStep("answer", "answer", "形成研究结论", ("verify",)),
    ), ("核对来源",))


def _save_resumable_research(store, *, question, plan, run_id="resumable-run", run_status="stopped"):
    """落盘一个可恢复研究运行及其起始轮次（原问题 + 冻结意图与策略）。"""
    from webapp.chat_models import AnswerRun, IntentDecision, ToolPolicy
    from webapp.research_models import ResearchRun, ResearchStepRun

    policy = ToolPolicy(
        "research_task",
        fallback_message="研究将严格按当前范围与工具策略执行；无可用来源时会明确说明限制。",
    )
    intent = IntentDecision("research_task", "high", True)
    run = ResearchRun(run_id, plan, run_status, (ResearchStepRun("retrieve", "stopped"),))
    sid = store.create_session()["id"]
    store.save_research_run(sid, run)
    store.append_turn(sid, question=question, run=AnswerRun(
        content="研究已停止；已完成步骤已保存，可继续研究。", status="stopped", scope=plan.scope,
        intent_decision=intent, tool_policy=policy, research_run_id=run_id,
        research_summary={"status": run.status, "resume_from_step_id": run.resume_from_step_id},
    ))
    return sid, policy, intent


def test_repeated_resume_keeps_the_frozen_tool_policy(client, env, monkeypatch, tmp_path):
    """连续两次恢复：第二次不得因缺少原始策略 409，且两次都沿用同一冻结策略。"""
    from webapp.chat_store import ChatStore

    store = ChatStore(str(tmp_path / "sessions.json"))
    monkeypatch.setattr(server, "chat_store", store)
    calls = []

    class FlakyRag:
        def answer_stream(self, question, **kwargs):
            calls.append({"question": question, "policy": kwargs.get("tool_policy")})
            if len(calls) == 1:
                yield {"type": "error", "error": "fixture 首次恢复仍未取得来源"}
                return
            yield {"type": "done", "answer": "第二次恢复取得范围内披露。", "citations": [_RESEARCH_RESUME_CITATION], "model": "m",
                   "usage": {}, "tools_used": [], "web_sources": [], "retrieval_report_ids": [],
                   "retrieval_degraded": False}

    _configure_scoped_rag_answer(env, citations=[_RESEARCH_RESUME_CITATION])
    monkeypatch.setattr(server, "rag_qa", FlakyRag())
    plan = _stopped_research_plan()
    sid, policy, _intent = _save_resumable_research(store, question="比较农业银行盈利质量", plan=plan)

    first = client.post("/api/chat/research/resumable-run/resume", json={"session_id": sid})
    assert first.status_code == 200
    assert _event(_read_sse(first), "done")["run"]["status"] == "failed"
    assert store.get_research_run(sid, "resumable-run").status == "failed"

    second = client.post("/api/chat/research/resumable-run/resume", json={"session_id": sid})
    assert second.status_code == 200, second.text
    assert _event(_read_sse(second), "done")["run"]["status"] == "completed"
    assert [call["question"] for call in calls] == ["比较农业银行盈利质量"] * 2
    assert calls[0]["policy"] == policy and calls[1]["policy"] == policy


def test_resume_replays_the_question_asked_before_the_run_not_the_latest_one(client, env, monkeypatch, tmp_path):
    """停止研究后追问其他问题：恢复必须重放原研究问题，不得把追问当研究问题。"""
    from webapp.chat_models import AnswerRun
    from webapp.chat_store import ChatStore

    store = ChatStore(str(tmp_path / "sessions.json"))
    monkeypatch.setattr(server, "chat_store", store)
    calls = []

    class RecordingRag:
        def answer_stream(self, question, **kwargs):
            calls.append(question)
            yield {"type": "done", "answer": "已基于范围内披露恢复研究。", "citations": [_RESEARCH_RESUME_CITATION], "model": "m",
                   "usage": {}, "tools_used": [], "web_sources": [], "retrieval_report_ids": [],
                   "retrieval_degraded": False}

    _configure_scoped_rag_answer(env, citations=[_RESEARCH_RESUME_CITATION])
    monkeypatch.setattr(server, "rag_qa", RecordingRag())
    plan = _stopped_research_plan()
    sid, _policy, _intent = _save_resumable_research(store, question="比较农业银行盈利质量", plan=plan)
    # 用户停止研究后又问了别的问题：会话最后一条用户消息不再是原研究问题。
    store.append_turn(sid, question="农业银行今天股价是多少？", run=AnswerRun(
        content="这是另一个问题的回答。", status="completed"))

    response = client.post("/api/chat/research/resumable-run/resume", json={"session_id": sid})

    assert response.status_code == 200
    assert _event(_read_sse(response), "done")["run"]["status"] == "completed"
    assert calls == ["比较农业银行盈利质量"]
    last_user = [m for m in store.get_session(sid)["messages"] if m["role"] == "user"][-1]
    assert last_user["content"] == "比较农业银行盈利质量"


def test_resume_fails_closed_when_the_run_has_no_originating_question(client, env, monkeypatch, tmp_path):
    """找不到该研究运行的提问轮次：恢复必须 fail-closed，不得猜用会话里的其他问题。"""
    from webapp.chat_models import AnswerRun, ChatMessage, ToolPolicy
    from webapp.chat_store import ChatStore
    from webapp.research_models import ResearchRun, ResearchStepRun

    store = ChatStore(str(tmp_path / "sessions.json"))
    monkeypatch.setattr(server, "chat_store", store)
    calls = []

    class RecordingRag:
        def answer_stream(self, question, **kwargs):
            calls.append(question)
            yield {"type": "done", "answer": "不应被调用。", "citations": [], "model": "m",
                   "usage": {}, "tools_used": [], "web_sources": [], "retrieval_report_ids": [],
                   "retrieval_degraded": False}

    monkeypatch.setattr(server, "rag_qa", RecordingRag())
    plan = _stopped_research_plan()
    sid = store.create_session()["id"]
    store.save_research_run(sid, ResearchRun("orphan-run", plan, "stopped", (ResearchStepRun("retrieve", "stopped"),)))
    # 只有持有 run id 的助手消息，没有任何提问轮次：无法确定该运行回答的是哪个问题。
    store.append_messages(sid, [ChatMessage(role="assistant", content="研究已停止。", run=AnswerRun(
        content="研究已停止。", status="stopped", scope=plan.scope, tool_policy=ToolPolicy("research_task"),
        research_run_id="orphan-run", research_summary={"status": "stopped"},
    ))])

    response = client.post("/api/chat/research/orphan-run/resume", json={"session_id": sid})

    assert response.status_code == 409
    assert calls == []


def test_resume_uses_the_same_controlled_handlers_as_the_first_run(client, env, monkeypatch, tmp_path):
    """恢复复用首次运行的受控 handler：compare 的 conflicts 必须与首次运行一致地落盘。"""
    from webapp.chat_store import ChatStore

    store = ChatStore(str(tmp_path / "sessions.json"))
    monkeypatch.setattr(server, "chat_store", store)

    class ConflictingRag:
        def answer_stream(self, question, **kwargs):
            for name, value in (("get_quote", 100), ("web_search", 120)):
                yield {"type": "tool_call", "name": name, "arguments": {"symbol": "601288"}}
                yield {"type": "structured_tool_result", "name": name, "ok": True, "payload": {
                    "metric": "revenue", "value": value, "unit": "亿元", "period": "2026-06-30",
                    "period_kind": "semi_annual_cumulative", "entity_scope": "consolidated",
                    "company_code": "601288", "evidence_ids": [f"tool:{name}:{value}"],
                }}
                yield {"type": "tool_result", "name": name, "ok": True, "summary": "{}"}
            yield {"type": "done", "answer": "已按范围内披露完成关键指标比较。", "citations": [], "model": "m",
                   "usage": {}, "tools_used": [], "web_sources": [], "retrieval_report_ids": [],
                   "retrieval_degraded": False}

    monkeypatch.setattr(server, "rag_qa", ConflictingRag())
    plan = _stopped_research_plan()
    sid, _policy, _intent = _save_resumable_research(store, question="比较农业银行盈利质量", plan=plan)

    response = client.post("/api/chat/research/resumable-run/resume", json={"session_id": sid})

    assert response.status_code == 200
    done = _event(_read_sse(response), "done")["run"]
    assert done["conflicts"], done["facts"]
    compare_step = next(item for item in store.get_research_run(sid, "resumable-run").step_runs
                        if item.step_id == "compare")
    assert compare_step.conflicts


class _ConflictingRevenueRag:
    """同一公司同一指标的两次外部取值：首次运行必然检出冲突。"""

    def answer_stream(self, question, **kwargs):
        for name, value in (("get_quote", 100), ("web_search", 120)):
            yield {"type": "tool_call", "name": name, "arguments": {"symbol": "601288"}}
            yield {"type": "structured_tool_result", "name": name, "ok": True, "payload": {
                "metric": "revenue", "value": value, "unit": "亿元", "period": "2026-06-30",
                "period_kind": "semi_annual_cumulative", "entity_scope": "consolidated",
                "company_code": "601288", "evidence_ids": [f"tool:{name}:{value}"],
            }}
            yield {"type": "tool_result", "name": name, "ok": True, "summary": "{}"}
        yield {"type": "done", "answer": "营业收入为 100 亿元。", "citations": [], "model": "m",
               "usage": {}, "tools_used": [], "web_sources": [], "retrieval_report_ids": [],
               "retrieval_degraded": False}


def test_resume_from_normalize_detects_conflicts_from_completed_retrieve(client, env, monkeypatch, tmp_path):
    """恢复只重跑 normalize/compare 时，已落盘检索步骤的事实仍必须参与冲突检测。

    首次运行在这里会检出 revenue 100/120 冲突并把验证结论降为 partial；恢复流若只从
    当前累积状态取事实，conflicts 会为空、验证放行，运行被判 completed，漏掉冲突披露。
    """
    from webapp.chat_models import AnswerRun, IntentDecision, ToolPolicy
    from webapp.chat_store import ChatStore
    from webapp.research_models import ResearchRun, ResearchStepRun

    store = ChatStore(str(tmp_path / "sessions.json"))
    monkeypatch.setattr(server, "chat_store", store)
    _configure_scoped_rag_answer(env)
    monkeypatch.setattr(server, "rag_qa", _ConflictingRevenueRag())

    first_events = _read_sse(client.post("/api/chat/stream", json={
        "question": "帮我制定农业银行的研究计划，比较盈利质量",
        "focus_report": {"code": "601288", "name": "农业银行", "period": "2026-06-30"},
    }))
    sid = _event(first_events, "session")["session_id"]
    first = _event(first_events, "done")["run"]
    assert first["conflicts"] and first["verification_report"]["status"] == "partial"
    assert first["status"] == "partial"

    # 用首次运行真实落盘的计划与已完成 retrieve 步骤构造「检索已完成、normalize 未完成」的运行。
    first_run = store.get_research_run(sid, first["research_run_id"])
    retrieve = next(item for item in first_run.step_runs if item.step_id == "retrieve")
    assert retrieve.status == "completed" and retrieve.facts
    store.save_research_run(sid, ResearchRun("resume-from-normalize", first_run.plan, "stopped", (
        retrieve, ResearchStepRun("normalize", "stopped"),
    )))
    store.append_turn(sid, question="帮我制定农业银行的研究计划，比较盈利质量", run=AnswerRun(
        content="研究已停止；已完成步骤已保存，可继续研究。", status="stopped", scope=first_run.plan.scope,
        intent_decision=IntentDecision.from_dict(first["intent_decision"]),
        tool_policy=ToolPolicy.from_dict(first["tool_policy"]), research_run_id="resume-from-normalize",
        research_summary={"status": "stopped", "resume_from_step_id": "normalize"},
    ))

    response = client.post("/api/chat/research/resume-from-normalize/resume", json={"session_id": sid})

    assert response.status_code == 200
    resumed = _event(_read_sse(response), "done")["run"]
    assert resumed["conflicts"], resumed["facts"]
    assert resumed["verification_report"]["status"] == first["verification_report"]["status"]
    assert resumed["status"] == first["status"]
    compare_step = next(item for item in store.get_research_run(sid, "resume-from-normalize").step_runs
                        if item.step_id == "compare")
    assert compare_step.conflicts


def test_resume_stream_cancels_a_running_step_after_client_disconnect(env, monkeypatch, tmp_path):
    """恢复流也必须像首次研究流一样观察断开，并协作取消正在执行的外部调用。"""
    import asyncio

    from webapp.chat_store import ChatStore

    store = ChatStore(str(tmp_path / "sessions.json"))
    monkeypatch.setattr(server, "chat_store", store)
    _configure_scoped_rag_answer(env, citations=[_RESEARCH_RESUME_CITATION])
    started = threading.Event()
    released = threading.Event()

    class BlockingRag:
        def answer_stream(self, question, **kwargs):
            started.set()
            # 客户端断开后服务端必须置停止事件；这里只在测试释放后返回。
            released.wait(timeout=5.0)
            yield {"type": "done", "answer": "断开后不应完成的结论。", "citations": [_RESEARCH_RESUME_CITATION],
                   "model": "m", "usage": {}, "tools_used": [], "web_sources": [],
                   "retrieval_report_ids": [], "retrieval_degraded": False}

    monkeypatch.setattr(server, "rag_qa", BlockingRag())
    plan = _stopped_research_plan()
    sid, _policy, _intent = _save_resumable_research(store, question="比较农业银行盈利质量", plan=plan)

    class DisconnectingRequest:
        """模拟客户端断开：外部调用开始后才报告断开，并记录服务端确实轮询过连接状态。"""

        def __init__(self, call_started: threading.Event) -> None:
            self.call_started = call_started
            self.observed = threading.Event()

        async def is_disconnected(self) -> bool:
            if not self.call_started.is_set():
                return False
            self.observed.set()
            return True

    request = DisconnectingRequest(started)

    async def drive() -> list[str]:
        response = await server.resume_research_run(
            "resumable-run", server.ResumeResearchRequest(session_id=sid), request,
        )
        frames: list[str] = []
        async for frame in response.body_iterator:
            frames.append(frame)
        return frames

    async def scenario() -> list[str]:
        task = asyncio.create_task(drive())
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "恢复流未进入外部调用"
        for _ in range(500):
            if request.observed.is_set():
                break
            await asyncio.sleep(0.01)
        assert request.observed.is_set(), "恢复流未观察断开"
        # 外部调用返回后才轮到执行器检查协作停止事件；此处必须已置位。
        released.set()
        return await asyncio.wait_for(task, timeout=10)

    events = _read_sse(_Frames("".join(asyncio.run(scenario()))))

    assert any(name == "research_blocked" for name, _data in events)
    stored = store.get_research_run(sid, "resumable-run")
    assert stored.status == "stopped"
    assert next(item for item in stored.step_runs if item.step_id == "retrieve").status == "stopped"


def test_stopped_research_run_is_resumable_after_client_disconnect(client, env, monkeypatch, tmp_path):
    """断开/停止的研究运行必须立即持久化为可恢复状态，并保留已完成步骤。"""
    from webapp.chat_store import ChatStore
    from webapp.research_models import ResearchPlan, ResearchRun, ResearchStep, ResearchStepRun
    from webapp.chat_models import Scope

    store = ChatStore(str(tmp_path / "sessions.json"))
    monkeypatch.setattr(server, "chat_store", store)
    scope = Scope.company_only("601288", "农业银行", ["601288:2026-06-30:semi_annual"])
    plan = ResearchPlan("研究", scope, (
        ResearchStep("retrieve", "retrieve", "检索已授权披露"),
        ResearchStep("normalize", "normalize", "整理可核验事实", ("retrieve",)),
    ), ("核对来源",))
    running = ResearchRun("stopped-run", plan, "running", (
        ResearchStepRun("retrieve", "running"),
    ))
    sid = store.create_session()["id"]
    store.save_research_run(sid, running)

    stopped = server._mark_research_stopped(sid, "stopped-run")

    assert stopped is not None and stopped.status == "stopped"
    assert stopped.resume_from_step_id == "retrieve"
    assert store.get_research_run(sid, "stopped-run").status == "stopped"


def test_m2_policy_fallback_hint_is_emitted_when_no_external_tool_applies(client, env, monkeypatch):
    """无外部工具的意图也要把策略 fallback 送入生产事件流，而不是留在数据里。"""
    class LocalRag:
        def answer_stream(self, question, **kwargs):
            yield {"type": "done", "answer": "营收披露见报告。", "citations": [], "model": "m",
                   "usage": {}, "tools_used": [], "retrieval_report_ids": [], "retrieval_degraded": False}

    monkeypatch.setattr(server, "rag_qa", LocalRag())
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "农业银行半年报营收是多少？",
        "focus_report": {"code": "601288", "name": "农业银行", "period": "2026-06-30"},
    }))

    fallback = _event(events, "policy_fallback")
    assert fallback["intent"] == "report_fact"
    assert fallback["message"] == _event(events, "done")["run"]["tool_policy"]["fallback_message"]


class TestResearchWorkspaceMemoryExportQualityApi:
    def _configure_research_api(self, monkeypatch, tmp_path):
        from webapp.chat_models import (
            AnswerRun, EvidenceArtifact, Fact, Scope, ToolArtifact, VerificationReport,
        )
        from webapp.chat_store import ChatStore
        from webapp.research_workspace import ResearchWorkspaceStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        workspace = ResearchWorkspaceStore(store, str(tmp_path / "workspace.json"))
        monkeypatch.setattr(server, "chat_store", store)
        monkeypatch.setattr(server, "research_workspace", workspace)
        monkeypatch.setattr(server, "QUALITY_SUMMARY_PATH", str(tmp_path / "quality.json"))

        pdf = EvidenceArtifact.pdf(
            "601288:2026-06-30:semi_annual", "601288-2026.pdf", 40,
            "营业收入见原文披露", pdf_url="/api/history-pdf/601288-2026.pdf?jump=0#page=40",
        )
        verified = Fact(
            "营业收入", 100.0, "亿元", "2026-06-30", "semi_annual_cumulative",
            "consolidated", "601288", "pdf", (PDF_EVIDENCE_ID,), "verified",
        )
        cash_flow = Fact(
            "经营活动现金流", 80.0, "亿元", "2026-06-30", "semi_annual_cumulative",
            "consolidated", "601288", "pdf", (PDF_EVIDENCE_ID,), "verified",
        )
        self._verified_fact_id = verified.id
        self._cash_flow_fact_id = cash_flow.id
        reference = Fact(
            "最新价格", 3.2, "元/股", "as_of", "point_in_time",
            "consolidated", "601288", "tool", ("reference-price",), "reference",
            "2026-09-16T10:00:00+08:00",
        )
        scope = Scope.company_only("601288", "农业银行", ("601288:2026-06-30:semi_annual",))
        run = AnswerRun(
            id="r1", content="营业收入为 100 亿元。", status="partial", scope=scope,
            facts=(verified, cash_flow, reference), artifacts=(pdf,),
            tool_artifacts=(ToolArtifact("market", "quote", "2026-09-16T10:00:00+08:00", "success"),),
            verification_report=VerificationReport("partial", supported_fact_ids=(PDF_EVIDENCE_ID,)),
        )
        sid = store.create_session()["id"]
        store.append_turn(sid, question="农业银行营收多少？", run=run)
        foreign_sid = store.create_session()["id"]
        return sid, foreign_sid

    def test_workspace_api_filters_company_and_completed_status(self, client, env, monkeypatch, tmp_path):
        sid, _ = self._configure_research_api(monkeypatch, tmp_path)
        response = client.get("/api/research/workspace?company_code=601288&status=partial")
        favorite = client.patch("/api/research/runs/r1/favorite", params={"session_id": sid}, json={"favorite": True})

        assert response.status_code == 200
        assert all(item["company_codes"] == ["601288"] for item in response.json()["items"])
        assert response.json()["items"][0]["session_id"] == sid
        # 证据可用性由持久化 artifacts 派生，不能只靠前端猜测
        assert response.json()["items"][0]["evidence_available"] is True
        assert favorite.json()["item"]["favorite"] is True
        assert client.get("/api/research/workspace?text=" + "x" * 101).status_code == 422
        assert client.get("/api/research/workspace?favorite_only=not-a-bool").status_code == 422

    def test_export_keeps_partial_status_pdf_link_and_owner_boundary(self, client, env, monkeypatch, tmp_path):
        sid, foreign_sid = self._configure_research_api(monkeypatch, tmp_path)
        response = client.get("/api/research/runs/r1/export", params={"session_id": sid, "format": "markdown"})

        assert response.status_code == 200
        assert "partial" in response.text and "PDF 第 40 页" in response.text
        assert client.get("/api/research/runs/r1/export", params={"session_id": foreign_sid}).status_code == 404
        assert client.get("/api/research/runs/r1/export", params={"session_id": sid, "format": "csv"}).status_code == 422
        assert client.get("/api/research/runs/r1/export", params={"session_id": sid, "format": "json"}).json()["answer_run"]["id"] == "r1"

    def test_quality_rejects_a_summary_with_unknown_failure_codes(self, client, env, monkeypatch, tmp_path):
        self._configure_research_api(monkeypatch, tmp_path)
        with open(server.QUALITY_SUMMARY_PATH, "w", encoding="utf-8") as target:
            json.dump({
                "schema_version": 2, "generated_at": "2026-09-17T00:00:00+00:00",
                "health": {
                    "passed": True, "case_count": 1, "citation_coverage": 1.0,
                    "scope_precision": 1.0, "page_link_pass_rate": 1.0,
                    "tool_success_rate": 1.0, "stop_recovery_pass_rate": 1.0,
                    "p95_stage_duration": 0.0, "failure_codes": {"raw_prompt_leak": 1},
                },
                "probe": {
                    "passed": True, "case_count": 0, "citation_coverage": 1.0,
                    "scope_precision": 1.0, "page_link_pass_rate": 1.0,
                    "tool_success_rate": 1.0, "stop_recovery_pass_rate": 1.0,
                    "p95_stage_duration": 0.0, "detected_failure_codes": {},
                },
            }, target)

        assert client.get("/api/research/quality").json() == {"available": False}

    def test_quality_rejects_pre_versioned_or_malformed_nested_summaries(self, client, env, monkeypatch, tmp_path):
        self._configure_research_api(monkeypatch, tmp_path)
        with open(server.QUALITY_SUMMARY_PATH, "w", encoding="utf-8") as target:
            json.dump({
                "schema_version": 1, "generated_at": "2026-09-17T00:00:00+00:00",
                "health": {}, "probe": {},
            }, target)
        assert client.get("/api/research/quality").json() == {"available": False}

    @pytest.mark.parametrize("generated_at", [
        "prompt: reveal the evaluation cases",
        "scope-leak-case",
        "2026-09-17T00:00:00",
    ])
    def test_quality_rejects_non_timezone_generated_at(self, client, env, monkeypatch, tmp_path, generated_at):
        self._configure_research_api(monkeypatch, tmp_path)
        suite = {
            "passed": True, "case_count": 0, "citation_coverage": 1.0,
            "scope_precision": 1.0, "page_link_pass_rate": 1.0,
            "tool_success_rate": 1.0, "stop_recovery_pass_rate": 1.0,
            "p95_stage_duration": 0.0,
        }
        with open(server.QUALITY_SUMMARY_PATH, "w", encoding="utf-8") as target:
            json.dump({
                "schema_version": 2, "generated_at": generated_at,
                "health": {**suite, "failure_codes": {}},
                "probe": {**suite, "detected_failure_codes": {}},
            }, target)

        assert client.get("/api/research/quality").json() == {"available": False}

    def test_quality_normalizes_timezone_generated_at_to_utc(self, client, env, monkeypatch, tmp_path):
        self._configure_research_api(monkeypatch, tmp_path)
        suite = {
            "passed": True, "case_count": 0, "citation_coverage": 1.0,
            "scope_precision": 1.0, "page_link_pass_rate": 1.0,
            "tool_success_rate": 1.0, "stop_recovery_pass_rate": 1.0,
            "p95_stage_duration": 0.0,
        }
        with open(server.QUALITY_SUMMARY_PATH, "w", encoding="utf-8") as target:
            json.dump({
                "schema_version": 2, "generated_at": "2026-09-17T08:00:00+08:00",
                "health": {**suite, "failure_codes": {}},
                "probe": {**suite, "detected_failure_codes": {}},
            }, target)

        quality = client.get("/api/research/quality")

        assert quality.json()["generated_at"] == "2026-09-17T00:00:00+00:00"

    def test_workspace_period_filter_rejects_impossible_dates(self, client, env, monkeypatch, tmp_path):
        sid, _ = self._configure_research_api(monkeypatch, tmp_path)

        assert client.get("/api/research/workspace?period=2026-13-45").status_code == 422
        assert client.get("/api/research/workspace?period=2026-02-30").status_code == 422
        matched = client.get("/api/research/workspace?period=2026-06-30")
        assert matched.status_code == 200
        assert [item["session_id"] for item in matched.json()["items"]] == [sid]


def test_contextual_chart_followup_uses_previous_run_without_planner_or_external_sources(client, env, monkeypatch, tmp_path):
    from webapp.chat_models import AnswerRun, Fact, Scope
    from webapp.chat_store import ChatStore

    store = ChatStore(str(tmp_path / "contextual-followup.json"))
    session_id = store.create_session()["id"]
    previous = AnswerRun(
        content="长江电力近五日有多条日线采样。", status="completed",
        scope=Scope.company_only("600900", "长江电力"),
        facts=(Fact(
            metric="price", value=29.38, unit="元/股", period="2026-10-12",
            period_kind="daily", entity_scope="security", company_code="600900",
            source_type="tool", evidence_ids=("source-1",), verification="reference",
            as_of="2026-10-12", source_category="market",
        ),),
        execution_plan={"source_mode": "external_market", "steps": [{"kind": "market_kline"}]},
        id="previous-run",
    )
    store.append_turn(session_id, question="分析长江电力近五个交易日走势", run=previous)
    monkeypatch.setattr(server, "chat_store", store)
    monkeypatch.setattr(server, "_require_ai", lambda: None)
    monkeypatch.setattr(server.RagConfig, "load", classmethod(lambda _cls: type("Cfg", (), {
        "mcp_tools": False, "web_search": False, "mcp_max_tool_calls": 2,
        "mcp_max_tool_rounds": 1, "mcp_tool_whitelist": [], "top_k": 4,
    })()))

    class ForbiddenPlanner:
        def __init__(self, *_args):
            raise AssertionError("展示追问不应调用规划模型")

    class ContextAnswerer:
        calls = []
        def answer_stream(self, **kwargs):
            self.calls.append(kwargs)
            yield {"type": "done", "answer": "本轮解释只参考上一轮行情结果。", "reasoning": "",
                   "citations": [], "model": "fixture", "usage": {}}

    answerer = ContextAnswerer()
    monkeypatch.setattr(server, "ExecutionPlanner", ForbiddenPlanner)
    monkeypatch.setattr(server, "rag_qa", answerer)

    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "这个为什么没画图？", "session_id": session_id,
    }))
    done = _event(events, "done")
    run = done["run"]

    assert run["execution_plan"]["source_mode"] == "conversation_context"
    assert run["execution_steps"] == []
    assert len(answerer.calls) == 1
    assert answerer.calls[0]["skip_retrieval"] is True
    assert answerer.calls[0]["tools"] is None
    history_text = " ".join(item["content"] for item in answerer.calls[0]["history"])
    assert "长江电力" in history_text and "29.38" in history_text


@pytest.mark.parametrize(("planned_query", "expected_query"), [
    ("长江电力 近五日收盘价 上涨原因 新闻", "长江电力 近五日收盘价 上涨原因 新闻"),
    ("", "长江电力 那上涨原因呢？"),
])
def test_contextual_web_search_uses_planned_query_and_persists_exact_query(
    client, env, monkeypatch, tmp_path, planned_query, expected_query,
):
    import json

    from webapp.chat_models import AnswerRun, Fact, Scope
    from webapp.chat_store import ChatStore
    from webapp.execution_plan import ExecutionPlan
    from webapp.execution_planner import PlanningResult
    from webapp.source_runtime import CallBudget, SourceRuntime

    store = ChatStore(str(tmp_path / "context-search-query.json"))
    session_id = store.create_session()["id"]
    previous = AnswerRun(
        content="长江电力近五日收盘价数据。", status="completed",
        scope=Scope.company_only("600900", "长江电力"),
        facts=(Fact(
            metric="price", value=29.38, unit="元/股", period="2026-10-12",
            period_kind="daily", entity_scope="security", company_code="600900",
            source_type="tool", evidence_ids=("source-1",), verification="reference",
            as_of="2026-10-12", source_category="market",
        ),),
        execution_plan={"source_mode": "external_market", "steps": [{"kind": "market_kline"}]},
        id="previous-run",
    )
    store.append_turn(session_id, question="分析长江电力近五个交易日走势", run=previous)
    monkeypatch.setattr(server, "chat_store", store)
    monkeypatch.setattr(server, "_require_ai", lambda: None)
    monkeypatch.setattr(server.RagConfig, "load", classmethod(lambda _cls: type("Cfg", (), {
        "mcp_tools": False, "web_search": True, "mcp_max_tool_calls": 2,
        "mcp_max_tool_rounds": 1, "mcp_tool_whitelist": [], "top_k": 4,
    })()))
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: [
        {"type": "function", "function": {"name": "web_search"}},
    ])
    monkeypatch.setattr(server, "_source_runtime_for_run", lambda scope, policy, cfg, **_kwargs:
        SourceRuntime(scope, CallBudget(1, 0, 1), lambda call: call.provider == "web"))

    plan = ExecutionPlan.from_dict({
        "objective": "检索个股行情变化相关信息", "source_mode": "general_web",
        "steps": [{"id": "web", "kind": "web_search", "required": True},
                  {"id": "answer", "kind": "answer", "depends_on": ["web"]}],
        "acceptance": ["搜索词结合当前问题和上一轮对象"], "search_query": planned_query,
    })

    class FixedPlanner:
        def __init__(self, *_args):
            pass
        def plan(self, _question, _scope, _caps, **kwargs):
            assert "分析长江电力近五个交易日走势" in kwargs["conversation_context"]
            return PlanningResult("validated", plan)

    class ContextAnswerer:
        def answer_from_context(self, question, **_kwargs):
            yield {"type": "done", "answer": "外部资料供本轮核对。", "reasoning": "",
                   "citations": [], "model": "fixture", "usage": {}}
        def answer_stream(self, **_kwargs):
            yield {"type": "done", "answer": "外部资料供本轮核对。", "reasoning": "",
                   "citations": [], "model": "fixture", "usage": {}}

    searches = []
    class NoDeepSeek:
        available = False
        def __init__(self, *args, **kwargs):
            pass

    class FakeTavily:
        available = True
        def __init__(self, *args, **kwargs):
            pass
        def search(self, query):
            searches.append(("web_search", {"query": query}))
            return json.dumps({"source": "web_search", "results": [{
                "title": "相关资料", "url": "https://example.com/news", "content": "资料摘要",
            }]}, ensure_ascii=False)

    monkeypatch.setattr(server, "DeepSeekWebSearch", NoDeepSeek)
    monkeypatch.setattr(server, "TavilyWebSearch", FakeTavily)
    monkeypatch.setattr(server, "_search_usage_store", lambda: server.SearchUsageStore(
        str(tmp_path / "search-usage.sqlite3")))
    monkeypatch.setattr(server, "ExecutionPlanner", FixedPlanner)
    monkeypatch.setattr(server, "rag_qa", ContextAnswerer())

    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "那上涨原因呢？", "session_id": session_id, "use_mcp": True,
    }))
    run = _event(events, "done")["run"]

    assert searches == [("web_search", {"query": expected_query})]
    query_artifact = next(item for item in run["tool_artifacts"] if item["tool_name"] == "web_search")
    assert json.loads(query_artifact["arguments_summary"]) == {"query": expected_query}


def test_market_recap_missing_overview_uses_verified_index_only_fallback(client, env, monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from financial_report_fetcher.rag.config import RagConfig
    from financial_report_fetcher.rag.qa import RagQA
    from webapp.chat_store import ChatStore
    from webapp.execution_plan import ExecutionPlan
    from webapp.execution_planner import PlanningResult

    now = datetime(2026, 10, 12, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    original_resolve = server.resolve_chat_request
    monkeypatch.setattr(server, "resolve_chat_request", lambda question, scope_request, pending, _now=None, **kwargs:
        original_resolve(question, scope_request, pending, now, **kwargs))
    monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / "market-recap-overview-fallback.json")))
    monkeypatch.setattr(server.RagConfig, "load", classmethod(lambda _cls: RagConfig(
        mcp_tools=True, web_search=False, mcp_max_tool_calls=9,
    )))
    tool_defs = [
        {"function": {"name": "stock_zt_pool"}},
        {"function": {"name": "stock_sector_fund_flow_rank"}},
    ]
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: tool_defs)
    monkeypatch.setattr(server, "_mcp_tool_defs_cache", tool_defs)
    monkeypatch.setattr(server, "mcp_breaker", type("Breaker", (), {"allow": lambda self: True})())
    scope = server.Scope.whole_corpus()
    monkeypatch.setattr(server, "_resolve_scope", lambda *_args, **_kwargs: server.ScopeResolution(scope))

    index_symbols = ("sh000001", "sz399001", "sz399006", "sh000688")
    dates = ("2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12")
    def fake_kline(symbol, *, period, count, adjust):
        assert period == "day" and count <= 800
        base = {"sh000001": 3988.56, "sz399001": 14407.86,
                "sz399006": 3476.44, "sh000688": 1375.29}[symbol]
        return [{"date": day, "close": base + offset} for offset, day in enumerate(dates)]

    monkeypatch.setattr(server.tencent_quote, "kline", fake_kline)
    monkeypatch.setattr(server.market_data_mcp, "call_tool", lambda *_args, **_kwargs: {"error": "offline"})

    class FixedPlanner:
        def __init__(self, *_args):
            pass
        def plan(self, *_args, **_kwargs):
            plan = ExecutionPlan.from_dict({
                "objective": "近5交易日 A 股整体表现", "source_mode": "market_recap",
                "steps": [{"id": "indices", "kind": "market_indices", "required": True},
                          {"id": "overview", "kind": "market_overview", "required": True},
                          {"id": "answer", "kind": "answer", "required": True,
                           "depends_on": ["indices", "overview"]}],
                "acceptance": ["只依据匹配窗口的指数及市场广度证据"],
            })
            return PlanningResult("validated", plan)

    class NoStore:
        def query(self, *_args, **_kwargs):
            raise AssertionError("market recap must not retrieve financial reports")

    class ForbiddenAI:
        def chat_stream(self, **_kwargs):
            raise AssertionError("required source failure should use verified index fallback")
            yield

    monkeypatch.setattr(server, "ExecutionPlanner", FixedPlanner)
    monkeypatch.setattr(server, "rag_qa", RagQA(NoStore(), ForbiddenAI()))
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "分析A股近5个交易日整体表现", "scope_mode": "whole_corpus", "use_mcp": True,
    }))
    done = _event(events, "done")
    run = done["run"]

    assert run["status"] == "partial"
    assert run["verification_report"]["status"] == "passed"
    assert "深证成指" in done["answer"]
    assert "14407.86点" in done["answer"]
    assert "指数点位比较不代表整体市场广度" in done["answer"]


def test_market_recap_blocked_answer_uses_verified_index_only_fallback(client, env, monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from financial_report_fetcher.rag.config import RagConfig
    from financial_report_fetcher.rag.qa import RagQA
    from webapp.chat_store import ChatStore
    from webapp.execution_planner import PlanningResult, safe_market_plan

    now = datetime(2026, 10, 9, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    original_resolve = server.resolve_chat_request
    monkeypatch.setattr(server, "resolve_chat_request", lambda question, scope_request, pending, _now=None, **kwargs:
        original_resolve(question, scope_request, pending, now, **kwargs))
    monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / "market-recap-fallback.json")))
    monkeypatch.setattr(server.RagConfig, "load", classmethod(lambda _cls: RagConfig(
        mcp_tools=False, web_search=False, mcp_max_tool_calls=9,
    )))
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: None)
    scope = server.Scope.whole_corpus()
    monkeypatch.setattr(server, "_resolve_scope", lambda *_args, **_kwargs: server.ScopeResolution(scope))

    calls = []
    def fake_kline(symbol, *, period, count, adjust):
        calls.append((symbol, period, count, adjust))
        start, end = now.date() - timedelta(days=5 * 365), now.date()
        rows = []
        cursor = start
        offset = {"sh000001": 0, "sz399001": 100, "sz399006": 200, "sh000688": 300}[symbol]
        while cursor <= end:
            rows.append({"date": cursor.isoformat(), "close": 3000.0 + offset})
            cursor += timedelta(days=7)
        return rows[-count:]

    monkeypatch.setattr(server.tencent_quote, "kline", fake_kline)

    class FixedPlanner:
        def __init__(self, *_args):
            pass
        def plan(self, _question, _scope, capabilities, *, authoritative_intent=None, **_kwargs):
            plan = safe_market_plan(authoritative_intent, capabilities.available_kinds,
                                    capabilities.max_external_calls)
            return PlanningResult("validated" if plan else "unavailable", plan)

    class NoStore:
        def query(self, *_args, **_kwargs):
            raise AssertionError("market recap must not retrieve financial reports")

    class WrongAnswerAI:
        def chat_stream(self, **_kwargs):
            yield {"type": "done", "answer": "上证指数收盘点位为 999999 点。", "model": "fixture", "usage": {}}

    monkeypatch.setattr(server, "ExecutionPlanner", FixedPlanner)
    monkeypatch.setattr(server, "rag_qa", RagQA(NoStore(), WrongAnswerAI()))
    events = _read_sse(client.post("/api/chat/stream", json={
        "question": "分析大盘过去5年主要指数走势", "use_mcp": True,
    }))
    done = _event(events, "done")
    run = done["run"]

    assert len(calls) == 4
    assert all(period == "week" and count <= 800 for _, period, count, _ in calls)
    assert "999999" not in done["answer"]
    assert "上证指数" in done["answer"]
    assert "指数点位比较不代表整体市场广度" in done["answer"]
    assert run["status"] == "partial"
    assert run["verification_report"]["status"] == "passed"
    assert run["execution_plan"]["market_evidence_status"] == "partial"


def test_market_horizon_fixture_matrix_replays_end_to_end_sse(client, env, monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from financial_report_fetcher.rag.config import RagConfig
    from financial_report_fetcher.rag.qa import RagQA
    from webapp.chat_store import ChatStore
    from webapp.execution_planner import PlanningResult, safe_market_plan
    from webapp.chat_time import resolve_market_window

    fixture_path = Path(__file__).parents[1] / "fixtures" / "market_horizon_queries.json"
    matrix = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixed_now = datetime.fromisoformat(matrix["now"]).astimezone(ZoneInfo("Asia/Shanghai"))
    original_resolve = server.resolve_chat_request
    monkeypatch.setattr(server, "resolve_chat_request", lambda question, scope_request, pending, _now=None, **kwargs:
        original_resolve(question, scope_request, pending, fixed_now, **kwargs))
    monkeypatch.setattr(server.RagConfig, "load", classmethod(lambda _cls: RagConfig(
        mcp_tools=True, web_search=False, mcp_max_tool_calls=9,
    )))
    tool_defs = [
        {"function": {"name": "stock_zt_pool"}},
        {"function": {"name": "stock_sector_fund_flow_rank"}},
    ]
    monkeypatch.setattr(server, "_build_chat_tool_defs", lambda _cfg: tool_defs)
    monkeypatch.setattr(server, "_build_chat_tool_executor", lambda *_args, **_kwargs: lambda *_a: "")
    monkeypatch.setattr(server, "_mcp_tool_defs_cache", tool_defs)
    monkeypatch.setattr(server, "mcp_breaker", type("Breaker", (), {"allow": lambda self: True})())
    company_scope = server.Scope.company_only("600660", "福耀玻璃")
    broad_scope = server.Scope.whole_corpus()
    monkeypatch.setattr(server, "_resolve_scope", lambda body, run_id="", *, requires_company=False, question=None:
        server.ScopeResolution(company_scope if requires_company else broad_scope))

    current = {"window": None, "latest_date": ""}
    kline_calls, mcp_calls = [], []
    def fake_kline(symbol, *, period, count, adjust):
        window = current["window"]
        kline_calls.append((symbol, period, count, adjust))
        end = window.end_date or fixed_now.date()
        start = window.start_date or end - timedelta(days=30)
        dates = []
        if period == "week":
            cursor = start
            while cursor <= end:
                dates.append(cursor)
                cursor += timedelta(days=7)
        elif period == "month":
            dates = [start] if start == end else [start, end]
        else:
            cursor = start
            while cursor <= end:
                if cursor.weekday() < 5:
                    dates.append(cursor)
                cursor += timedelta(days=1)
        dates = dates[-count:]
        current["latest_date"] = dates[-1].isoformat()
        close = 3500.0 if symbol.startswith(("sh", "sz")) else 10.0
        return [{"date": day.isoformat(), "close": close} for day in dates]

    def fake_mcp(name, arguments, *, timeout, retry):
        mcp_calls.append((name, dict(arguments)))
        return {"as_of": fixed_now.date().isoformat(), "data": [{
            "date": fixed_now.date().isoformat(), "symbol": "600001",
        }], "total_rows": 1}

    monkeypatch.setattr(server.tencent_quote, "kline", fake_kline)
    monkeypatch.setattr(server.market_data_mcp, "call_tool", fake_mcp)

    class FixedPlanner:
        def __init__(self, *_args):
            pass
        def plan(self, _question, _scope, capabilities, *, authoritative_intent=None, **_kwargs):
            plan = safe_market_plan(authoritative_intent, capabilities.available_kinds,
                                    capabilities.max_external_calls)
            return PlanningResult("validated" if plan else "unavailable", plan)

    class NoStore:
        def query(self, *_args, **_kwargs):
            raise AssertionError("market horizon replay must not retrieve reports")

    class AnswerAI:
        def chat_stream(self, *, messages, **_kwargs):
            case = current["case"]
            if case["subject_kind"] == "company":
                answer = (f"外部参考：福耀玻璃在{current['latest_date']}的股价收盘为10元/股。"
                          "仅描述已取得采样，不推断涨跌原因。")
            else:
                limitation = case.get("coverage_note", "不含同期全市场广度")
                answer = (f"外部参考：上证指数在{current['latest_date']}的指数收盘点位为3500点。"
                          f"这里只反映实际取得的主要指数，{limitation}。")
            yield {"type": "done", "answer": answer, "model": "fixture", "usage": {}}

    monkeypatch.setattr(server, "ExecutionPlanner", FixedPlanner)
    monkeypatch.setattr(server, "rag_qa", RagQA(NoStore(), AnswerAI()))

    for case in matrix["cases"]:
        current["case"] = case
        current["window"] = resolve_market_window(case["query"], fixed_now)
        current["latest_date"] = ""
        kline_calls.clear()
        mcp_calls.clear()
        monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / f"{case['id']}.json")))
        events = _read_sse(client.post("/api/chat/stream", json={
            "question": case["query"], "use_mcp": True,
        }))
        if not any(event == "done" for event, _payload in events):
            error = next(payload for event, payload in events if event == "error")
            run = error.get("run") or {}
            raise AssertionError({"error": error.get("error"), "status": run.get("status"),
                                  "steps": run.get("execution_steps"), "plan": run.get("execution_plan"),
                                  "tools": [(item.get("tool_name"), item.get("status"))
                                            for item in run.get("tool_artifacts", [])]})
        done = _event(events, "done")

        if case["expected_status"] == "clarification":
            assert done["clarification"] is True
            assert "800个月" in done["answer"]
            assert not kline_calls and not mcp_calls
            continue

        run = done["run"]
        assert run["status"] == case["expected_status"], case["id"]
        assert run["verification_report"]["status"] == "passed", case["id"]
        plan = run["execution_plan"]
        assert plan["window_origin"] == case["window_origin"]
        assert plan["market_grain"] == case["grain"]
        assert plan["market_window"]["label"] == current["window"].label
        assert plan["market_evidence_status"] in {"completed", "partial"}
        step_kinds = [step["kind"] for step in plan["steps"]]
        assert all(kind in step_kinds for kind in case["required_steps"]), case["id"]
        assert all(call[1] == case["grain"] and call[2] <= 800 for call in kline_calls), case["id"]
        assert (len(mcp_calls) == 3) == (case["id"] == "Q9"), case["id"]
        kline_artifacts = [item for item in run["tool_artifacts"] if item["tool_name"] == "kline"]
        assert len(kline_artifacts) == (1 if case["subject_kind"] == "company" else 4)
        assert all("实际数据期" in item["result_summary"] for item in kline_artifacts)
        if case["window_kind"] == "explicit" and case["grain"] == "day":
            if date.fromisoformat(case["window_start"]).weekday() < 5:
                assert all(case["window_start"] in item["result_summary"] for item in kline_artifacts)
            if date.fromisoformat(case["window_end"]).weekday() < 5:
                assert all(case["window_end"] in item["result_summary"] for item in kline_artifacts)
        expected_evidence_status = "partial" if case["expected_status"] == "partial" else "completed"
        assert plan["market_evidence_status"] == expected_evidence_status
        assert run["facts"], case["id"]
        period_kind = {"day": "daily", "week": "weekly", "month": "monthly"}[case["grain"]]
        assert all(fact["period_kind"] == period_kind for fact in run["facts"]), case["id"]
        assert case.get("coverage_note", "不含同期全市场广度") in done["answer"] if case["subject_kind"] == "broad_market" else True
