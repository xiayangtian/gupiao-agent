"""M2 policy evaluation cases run through the real RagQA tool-policy gate.

The fixture cases exercise the production FastAPI stream, frozen Scope, intent and
ToolPolicy resolution, RagQA's model-visible tool filtering and executor gate,
FactNormalizer, ClaimVerifier, SSE, and AnswerRun persistence.  Only the AI,
RAG store and external executor are deterministic offline collaborators.
"""

import json
import os
from datetime import date
from pathlib import Path

import pytest

import webapp.server as server
from financial_report_fetcher.models import ReportMeta, ReportType
from financial_report_fetcher.report_identity import build_report_filename
from financial_report_fetcher.rag.mcp_tools import to_openai_tools
from financial_report_fetcher.rag.qa import RagQA
from tests.unit.test_server_api import _event, _read_sse, client, env  # noqa: F401

CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "chat_policy_eval_cases.json").read_text(encoding="utf-8"))
REQUIRED = {"question", "scope", "intent", "allowed_sources", "expected_fact_ids",
            "forbidden_report_ids", "expected_status", "expected_verification"}

FOCUS_CODE = "601288"
FOCUS_NAME = "农业银行"
SEMI_REPORT_ID = "601288:2026-06-30:semi_annual"
ANNUAL_REPORT_ID = "601288:2025-12-31:annual"
PEER_CODE = "600036"
PEER_REPORT_ID = "600036:2026-06-30:semi_annual"
OTHER_CODE = "600900"
OTHER_REPORT_ID = "600900:2026-06-30:semi_annual"

_PDF_FILENAMES = {
    SEMI_REPORT_ID: "农业银行_601288_半年报_2026.pdf",
    ANNUAL_REPORT_ID: "农业银行_601288_年报_2025.pdf",
    PEER_REPORT_ID: "招商银行_600036_半年报_2026.pdf",
}
_PROVIDER_TOOLS = ["get_realtime_quote", "get_realtime_data", "get_financial_metrics", "get_balance_sheet"]
_CONTROLLED_TOOL = "request_missing_reports"


def _pdf_meta(report_id):
    code, period, report_type = report_id.split(":")
    names = {FOCUS_CODE: FOCUS_NAME, PEER_CODE: "招商银行", OTHER_CODE: "长江电力"}
    types = {"semi_annual": ReportType.SEMI_ANNUAL, "annual": ReportType.ANNUAL}
    return ReportMeta(
        company_id=code, company_name=names[code], report_type=types[report_type],
        period=date.fromisoformat(period), download_url="fixture://eval/report.pdf",
        title=f"{names[code]}{period} {report_type}",
    )


class _EvalStore:
    """Offline chunks that still pass through RagQA's actual Scope-filtered query."""

    def __init__(self):
        self.query_calls = []
        self._chunks = {
            SEMI_REPORT_ID: {"id": "semi", "report_id": SEMI_REPORT_ID, "section": "利润表", "page": 40,
                             "source": "pdf", "text": "营业收入 4108.71 亿元"},
            ANNUAL_REPORT_ID: {"id": "annual", "report_id": ANNUAL_REPORT_ID, "section": "利润表", "page": 15,
                               "source": "pdf", "text": "营业收入 7105.55 亿元"},
            PEER_REPORT_ID: {"id": "peer", "report_id": PEER_REPORT_ID, "section": "利润表", "page": 20,
                             "source": "pdf", "text": "营业收入 1790.00 亿元"},
            OTHER_REPORT_ID: {"id": "other", "report_id": OTHER_REPORT_ID, "section": "利润表", "page": 20,
                              "source": "pdf", "text": "营业收入 999.00 亿元"},
        }

    def list_report_ids(self):
        return list(self._chunks)

    def query(self, question, top_k=8, where=None):
        self.query_calls.append({"question": question, "where": where})
        report_ids = None if where is None else (where.get("report_id") or {}).get("$in")
        chunks = [dict(chunk) for report_id, chunk in self._chunks.items() if report_ids is None or report_id in report_ids]
        if question == "农业银行近两年营收趋势？":
            chunks[0]["page"] = 12
        elif question == "农业银行与同业对比？":
            chunks[0]["page"] = 20
        return chunks[:top_k]


class _EvalAI:
    """A fake model: emits OpenAI-style calls; RagQA, not this fake, gates them."""

    _ACTIONS = {
        "农业银行今天涨跌如何？": (("get_realtime_data", {"symbol": FOCUS_CODE}),),
        "农业银行异动原因？": (("web_search", {"query": "农业银行 公告 异动"}),),
        "农业银行今天价格与网页口径冲突如何解释？": (
            ("get_realtime_data", {"symbol": FOCUS_CODE}),
            ("web_search", {"query": "农业银行 股价"}),
        ),
        "农业银行实时数据工具失败怎么办？": (("get_realtime_data", {"symbol": FOCUS_CODE, "scenario": "failure"}),),
        # This non-fixture probe makes the real RagQA reject both a filtered tool
        # name and a free-text web query that resolves outside the frozen Scope.
        "农业银行今天策略门控？": (
            ("get_financial_metrics", {"symbol": FOCUS_CODE}),
            ("web_search", {"query": "长江电力 公告"}),
        ),
    }

    _ANSWERS = {
        "农业银行半年报营收是多少？": "半年报营业收入原文见 PDF 第 40 页披露。[1]",
        "农业银行近两年营收趋势？": "近两年营业收入变化见 PDF 第 40 页与第 15 页披露。[1][2]",
        "农业银行与同业对比？": "已基于本地可检索同业样本进行比较，原文见 PDF 第 40 页与第 20 页。[1][2]",
        "农业银行今天涨跌如何？": "农业银行当前价格为 3.2 元/股。",
        "农业银行异动原因？": "外部事件来源暂不可用；不能确认归因。",
        "长江电力营收是多少？": "长江电力营业收入为 100 亿元。",
        "农业银行今天价格与网页口径冲突如何解释？": "农业银行当前价格为 3.2 元/股。",
        "农业银行实时数据工具失败怎么办？": "实时数据暂不可用；请以本地披露为准。",
        "农业银行今天策略门控？": "没有可用的受控外部结果。",
    }

    def __init__(self):
        self.visible_tools = []

    def chat_stream(self, messages, *, system=None, tools=None):
        del system
        question = next(message["content"] for message in reversed(messages) if message["role"] == "user")
        self.visible_tools.append({
            "question": question,
            "names": [str((tool.get("function") or {}).get("name") or "") for tool in (tools or [])],
        })
        if not any(message["role"] == "tool" for message in messages) and question in self._ACTIONS:
            calls = [
                {"id": f"call-{index}", "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}
                for index, (name, arguments) in enumerate(self._ACTIONS[question], start=1)
            ]
            yield {"type": "tool_calls", "tool_calls": calls}
            return
        yield {"type": "done", "answer": self._ANSWERS[question], "model": "eval-fake", "usage": {}}


class _EvalExecutor:
    """Offline external collaborator; every invocation is recorded for assertions."""

    def __init__(self):
        self.calls = []

    def __call__(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "get_realtime_data" and arguments.get("scenario") == "failure":
            return "工具调用失败：行情服务暂不可用"
        if name == "get_realtime_data" and arguments.get("symbol") == FOCUS_CODE:
            return json.dumps({
                "metric": "price", "value": 3.2, "unit": "元/股", "period": "as_of",
                "period_kind": "point_in_time", "entity_scope": "consolidated", "company_code": FOCUS_CODE,
                "evidence_ids": ["tool:stock-data-mcp:get_realtime_data"],
            })
        if name == "web_search" and "股价" in str(arguments.get("query")):
            return json.dumps({
                "metric": "price", "value": 3.5, "unit": "元/股", "period": "as_of",
                "period_kind": "point_in_time", "entity_scope": "consolidated", "company_code": FOCUS_CODE,
                "evidence_ids": ["tool:web_search:web_search"],
            })
        if name == "web_search":
            return "工具调用失败：网页搜索不可用"
        return "工具调用失败：未预期的测试工具"


def _install_pipeline(monkeypatch, env, tmp_path):  # noqa: F811
    """Install real RagQA with fake offline collaborators, never a scripted RAG."""
    monkeypatch.setattr(server, "BASE_DIR", str(tmp_path))
    store = _EvalStore()
    ai = _EvalAI()
    executor = _EvalExecutor()

    class FakeStockMcp:
        def call_tool(self, name, arguments=None, timeout=None):
            del timeout
            if name == "get_stock_basic_info":
                industry = "银行业" if (arguments or {}).get("symbol") in {FOCUS_CODE, PEER_CODE} else "电力"
                return json.dumps({"industry": industry})
            return json.dumps({})

    class FakeSearch:
        available = True

        def __init__(self, *args, **kwargs):
            pass

    reports_dir = server.REPORTS_DIR
    os.makedirs(reports_dir, exist_ok=True)
    for report_id, filename in _PDF_FILENAMES.items():
        assert filename == build_report_filename(_pdf_meta(report_id))
        with open(os.path.join(reports_dir, filename), "wb") as handle:
            handle.write(b"%PDF-1.4 eval fixture")

    monkeypatch.setattr(server, "rag_store", store)
    monkeypatch.setattr(server, "stock_mcp", FakeStockMcp())
    monkeypatch.setattr(server, "TavilyWebSearch", FakeSearch)
    monkeypatch.setattr(server, "_mcp_tool_defs", lambda: to_openai_tools(
        [{"name": name} for name in _PROVIDER_TOOLS],
    ))
    monkeypatch.setattr(server, "rag_qa", RagQA(
        store, ai, tool_executor=executor,
        company_code_resolver=lambda value: {
            FOCUS_NAME: FOCUS_CODE, "农行": FOCUS_CODE, "长江电力": OTHER_CODE, "长电": OTHER_CODE,
        }.get(value),
    ))
    return store, ai, executor


def _evidence_ids(run):
    ids = {evidence_id for fact in run["facts"] for evidence_id in fact["evidence_ids"]}
    for artifact in run["artifacts"]:
        ids.add(f"{artifact['report_id']}#p{artifact['page']}" if artifact["source"] == "pdf"
                else f"web:{artifact['url']}")
    for tool in run["tool_artifacts"]:
        ids.add(f"tool:{tool['provider']}:{tool['tool_name']}")
    return ids


def _used_sources(run):
    sources = set()
    if any(artifact["source"] == "pdf" for artifact in run["artifacts"]):
        sources.add("local_pdf")
    if any(artifact["source"] == "web" for artifact in run["artifacts"]):
        sources.add("web")
    for tool in run["tool_artifacts"]:
        sources.add("web" if tool["tool_name"] == "web_search" or tool["provider"] == "web_search"
                    else "market_data")
    for fact in run["facts"]:
        sources.add("web" if fact["source_type"] == "web" else "market_data")
    return sources


def _run_case(client, case):  # noqa: F811
    focus_report = {"code": FOCUS_CODE, "period": "2026-06-30"}
    # Cross-period evaluation must let the real ScopeResolver retain both local
    # reports; other cases deliberately freeze the focused report.
    if case["question"] == "农业银行近两年营收趋势？":
        focus_report = {"code": FOCUS_CODE}
    response = client.post("/api/chat/stream", json={
        "question": case["question"],
        "scope_mode": "auto" if case["scope"] == "company_industry" else "company_only",
        "focus_report": focus_report,
        "use_mcp": True,
    })
    assert response.status_code == 200
    events = _read_sse(response)
    return events, _event(events, "done")["run"]


def test_m2_policy_evaluation_cases_are_complete_and_fail_closed():
    assert len(CASES) >= 8
    intents = {case["intent"] for case in CASES}
    assert {"report_fact", "company_trend", "industry_benchmark", "realtime_market", "event_attribution"} <= intents
    for case in CASES:
        assert REQUIRED <= set(case)
        assert case["question"] and case["scope"] and case["expected_status"]
        assert case["expected_verification"] in {"passed", "partial", "blocked"}
        assert isinstance(case["allowed_sources"], list)
        assert case["expected_fact_ids"] or case.get("no_fact_outcome") is True


def test_m2_evaluation_pipeline_uses_real_ragqa(client, env, monkeypatch, tmp_path):  # noqa: F811
    _install_pipeline(monkeypatch, env, tmp_path)
    assert isinstance(server.rag_qa, RagQA)


@pytest.mark.parametrize("case", CASES, ids=[case["intent"] for case in CASES])
def test_m2_evaluation_case_runs_through_real_ragqa_policy_gate(client, env, monkeypatch, tmp_path, case):  # noqa: F811
    store, ai, executor = _install_pipeline(monkeypatch, env, tmp_path)
    events, run = _run_case(client, case)

    policy_event = _event(events, "policy_resolved")
    assert policy_event["intent"] == case["intent"]
    allowed_tools = set(run["tool_policy"]["allowed_tools"])
    assert allowed_tools <= set(_PROVIDER_TOOLS + ["web_search"])
    assert "request_missing_reports" not in allowed_tools
    if "market_data" in case["allowed_sources"]:
        assert any(name.startswith("get_realtime") or name.startswith("get_quote") for name in allowed_tools)
    if "web" in case["allowed_sources"]:
        assert "web_search" in allowed_tools
    assert _used_sources(run) <= set(case["allowed_sources"])

    # RagQA, rather than the fake model, filters definitions visible to the model.
    visible = [entry["names"] for entry in ai.visible_tools if entry["question"] == case["question"]]
    assert visible
    assert all(set(names) <= allowed_tools | {_CONTROLLED_TOOL} for names in visible)
    assert "get_financial_metrics" not in {name for names in visible for name in names}

    forbidden = set(case["forbidden_report_ids"])
    if forbidden:
        assert not (forbidden & set(run["retrieval_report_ids"]))
        assert not (forbidden & {artifact["report_id"] for artifact in run["artifacts"]})
        assert not (forbidden & {fact["company_code"] for fact in run["facts"]})
        forbidden_codes = {report_id.split(":", 1)[0] for report_id in forbidden}
        assert not (forbidden_codes & {company["code"] for company in (run["scope"] or {}).get("companies", [])})
        assert not (forbidden & set((run["scope"] or {}).get("report_ids", [])))

    assert set(case["expected_fact_ids"]) <= _evidence_ids(run)
    assert run["status"] == case["expected_status"]
    assert run["verification_report"]["status"] == case["expected_verification"]
    if case.get("no_fact_outcome"):
        assert run["facts"] == []
        assert run["status"] != "completed" or run["verification_report"]["status"] != "passed"
    if case["expected_verification"] == "blocked":
        assert run["content"] == "未找到可核验的披露，不能确认该数值。"

    # The fake executor is the only external collaborator and receives precisely
    # the calls RagQA admitted; the persisted run is the same SSE completion.
    executed_names = [name for name, _ in executor.calls]
    if case["intent"] == "event_attribution":
        assert executed_names == ["web_search"]
    elif case["question"] == "农业银行今天价格与网页口径冲突如何解释？":
        assert executed_names == ["get_realtime_data", "web_search"]
    elif case["intent"] == "realtime_market" and not case.get("no_fact_outcome"):
        assert executed_names == ["get_realtime_data"]
    elif case["intent"] in {"report_fact", "company_trend", "industry_benchmark"}:
        assert executed_names == []
    assert store.query_calls
    session = server.chat_store.get_session(_event(events, "session")["session_id"])
    assert session["messages"][-1]["run"] == run


def test_m2_real_ragqa_rejects_filtered_tool_and_out_of_scope_web_query(client, env, monkeypatch, tmp_path):  # noqa: F811
    _, ai, executor = _install_pipeline(monkeypatch, env, tmp_path)
    events, run = _run_case(client, {
        "question": "农业银行今天策略门控？", "scope": "company_only",
    })

    visible = [entry["names"] for entry in ai.visible_tools if entry["question"] == "农业银行今天策略门控？"]
    assert visible and all("get_financial_metrics" not in names for names in visible)
    failed = [event for event in events if event[0] == "tool_result" and event[1]["ok"] is False]
    assert {event[1]["name"] for event in failed} == {"get_financial_metrics", "web_search"}
    assert executor.calls == []
    assert {artifact["tool_name"] for artifact in run["tool_artifacts"]} == {"get_financial_metrics", "web_search"}
    assert all(artifact["status"] == "failed" for artifact in run["tool_artifacts"])
