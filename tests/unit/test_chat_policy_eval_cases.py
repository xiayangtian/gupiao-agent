"""M2 评测用例：每一条都要真正跑过问答流水线，而不是只做 schema 校验。

用例通过真实 FastAPI 端点（``/api/chat/stream``）驱动真实生产代码：Scope 解析与
冻结、IntentRouter/ToolPolicyResolver、``_build_chat_tool_defs`` 的真实工具名、
FactNormalizer、冲突识别、ClaimVerifier 与答案持久化。只有模型与外部工具这两类
不确定、需要联网的协作方由本模块按用例脚本替代。

每个用例声明 ``question/scope/intent/allowed_sources/expected_fact_ids/
forbidden_report_ids/expected_status/expected_verification``，断言：

- ``policy_resolved.intent`` 等于声明的 intent（声明必须与真实分类一致）；
- 实际使用的来源是声明 ``allowed_sources`` 的子集；声明 market_data/web 时策略必须
  真的授予对应工具家族（否则实时/事件意图只剩网页搜索甚至无工具）；
- ``forbidden_report_ids`` 不出现在检索结果、证据、事实，且不在冻结 Scope 内；
- 持久化 run 的 ``status`` 等于 ``expected_status``（AnswerStatus：执行是否完整），
  且 ``verification_report.status`` 等于 ``expected_verification``（可信状态：passed/
  partial/blocked）——两者是不同的信号，回答不可核验时 run 仍可能是 completed。
- ``expected_fact_ids`` 是运行结果里的真实证据 id（Fact.evidence_ids ∪
  ``report_id#p页码`` 的 PDF 证据 ∪ ``web:URL`` ∪ ``tool:provider:tool``）。

``expected_fact_ids`` 为空且 ``no_fact_outcome`` 为真时，断言这轮没有采纳任何事实，
且运行不得既 completed 又核验 passed；``expected_verification`` 为 blocked 时，
不可核验的数值不得留在回答里。
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
from tests.unit.test_server_api import _event, _read_sse, client, env  # noqa: F401  复用真实 FastAPI 夹具

CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "chat_policy_eval_cases.json").read_text(encoding="utf-8"))
REQUIRED = {"question", "scope", "intent", "allowed_sources", "expected_fact_ids",
            "forbidden_report_ids", "expected_status", "expected_verification"}

# fixture 公司与报告身份（与浏览器验收同一套本地样本）
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

# 真实 provider 工具名（含实时行情家族与一个非家族工具，用于证明未列出的工具不被授权）
_PROVIDER_TOOLS = ["get_realtime_quote", "get_realtime_data", "get_financial_metrics", "get_balance_sheet"]


# ── 用例脚本：模型/工具的确定性替代品 ────────────────────────────────

def _pdf_citation(report_id, page, snippet):
    return {
        "source": "pdf", "report_id": report_id, "section": "利润表",
        "page": page, "snippet": snippet,
    }


def _structured(name, payload, ok=True):
    return {"type": "structured_tool_result", "name": name, "payload": payload, "ok": ok}


def _tool_call(name, arguments):
    return {"type": "tool_call", "name": name, "arguments": arguments}


def _done(answer, **overrides):
    payload = {
        "type": "done", "answer": answer, "citations": [], "model": "eval-script",
        "usage": {}, "tools_used": [], "web_sources": [],
        "retrieval_report_ids": [], "retrieval_degraded": False,
    }
    payload.update(overrides)
    return payload


def _report_fact_script(**_):
    """本报告数字：只引用第 40 页原文，不输出未经核验的数值。"""
    answer = "半年报营业收入原文见 PDF 第 40 页披露。"
    yield _done(answer, citations=[_pdf_citation(SEMI_REPORT_ID, 40, "营业收入 4108.71 亿元")],
                retrieval_report_ids=[SEMI_REPORT_ID])


def _company_trend_script(**_):
    """跨期比较：引用同一公司两个报告期，不跨公司。"""
    answer = "近两年营业收入变化见 PDF 第 12 页与第 15 页披露。"
    yield _done(
        answer,
        citations=[_pdf_citation(SEMI_REPORT_ID, 12, "营业收入 4108.71 亿元"),
                    _pdf_citation(ANNUAL_REPORT_ID, 15, "营业收入 7105.55 亿元")],
        retrieval_report_ids=[SEMI_REPORT_ID, ANNUAL_REPORT_ID],
    )


def _industry_benchmark_script(**_):
    """行业比较：只用本地已索引的同业样本，不用网页/实时数据。"""
    answer = "已基于本地可检索同业样本进行比较，原文见 PDF 第 20 页。"
    yield _done(
        answer,
        citations=[_pdf_citation(SEMI_REPORT_ID, 20, "营业收入 4108.71 亿元"),
                    _pdf_citation(PEER_REPORT_ID, 20, "营业收入 1790.00 亿元")],
        retrieval_report_ids=[SEMI_REPORT_ID, PEER_REPORT_ID],
    )


def _realtime_market_script(**_):
    """实时行情：行情工具家族真的被调用，外部参考必须带数据时间。"""
    yield _tool_call("get_realtime_data", {"symbol": FOCUS_CODE})
    yield _structured("get_realtime_data", {
        "metric": "price", "value": 3.2, "unit": "元/股", "period": "as_of",
        "period_kind": "point_in_time", "entity_scope": "consolidated",
        "company_code": FOCUS_CODE, "evidence_ids": ["tool:quote:601288"],
    })
    yield {"type": "tool_result", "name": "get_realtime_data", "ok": True, "summary": '{"price": 3.2}'}
    # 未标示「外部参考」：外部数值必须被核验降级为 partial
    answer = "农业银行当前价格为 3.2 元/股。"
    yield _done(answer, tools_used=["get_realtime_data"],
                retrieval_report_ids=[SEMI_REPORT_ID])


def _event_attribution_script(**_):
    """新闻/公告归因：网页搜索不可用时如实说明无法归因，不编造来源。"""
    yield _tool_call("web_search", {"query": "农业银行 公告 异动"})
    yield {"type": "tool_result", "name": "web_search", "ok": False,
           "summary": "工具调用失败：网页搜索不可用"}
    yield _done("外部事件来源暂不可用；不能确认归因。")


def _scope_violation_script(**_):
    """越界范围：提问指向另一家公司，本范围没有可核验证据，必须受控失败。"""
    yield _done("长江电力营业收入为 100 亿元。")


def _source_conflict_script(**_):
    """来源冲突：同一指标两个来源数值不同，必须披露差异而不是自行选边。"""
    for name, value, evidence_id in (
        ("get_realtime_data", 3.2, "tool:stock-data-mcp:get_realtime_data"),
        ("web_search", 3.5, "tool:web_search:web_search"),
    ):
        yield _tool_call(name, {"symbol": FOCUS_CODE, "query": "农业银行 股价"})
        yield _structured(name, {
            "metric": "price", "value": value, "unit": "元/股", "period": "as_of",
            "period_kind": "point_in_time", "entity_scope": "consolidated",
            "company_code": FOCUS_CODE, "evidence_ids": [evidence_id],
        })
        yield {"type": "tool_result", "name": name, "ok": True, "summary": "{}"}
    yield _done("农业银行当前价格为 3.2 元/股。", tools_used=["get_realtime_data", "web_search"])


def _tool_failure_script(**_):
    """工具失败：行情不可用时保留本地结论并说明降级，不冒充实时数据。"""
    yield _tool_call("get_realtime_data", {"symbol": FOCUS_CODE})
    yield {"type": "tool_result", "name": "get_realtime_data", "ok": False,
           "summary": "工具调用失败：行情服务暂不可用"}
    yield _done("实时数据暂不可用；请以本地披露为准。")


_SCRIPTS = {
    "农业银行半年报营收是多少？": _report_fact_script,
    "农业银行近两年营收趋势？": _company_trend_script,
    "农业银行与同业对比？": _industry_benchmark_script,
    "农业银行今天涨跌如何？": _realtime_market_script,
    "农业银行异动原因？": _event_attribution_script,
    "长江电力营收是多少？": _scope_violation_script,
    "农业银行今天价格与网页口径冲突如何解释？": _source_conflict_script,
    "农业银行实时数据工具失败怎么办？": _tool_failure_script,
}


class _ScriptedRagQA:
    """按用例问题产出确定性事件；没有脚本的用例直接失败，不允许静默跳过。"""

    def answer_stream(self, question, history=None, filters=None, tools=None,
                      priority_report_id=None, scope=None, run_id=None, tool_policy=None):
        script = _SCRIPTS.get(str(question or "").strip())
        if script is None:
            raise AssertionError(f"评测用例缺少流水线脚本：{question!r}")
        yield from script()


def _pdf_meta(report_id):
    code, period, report_type = report_id.split(":")
    names = {FOCUS_CODE: FOCUS_NAME, PEER_CODE: "招商银行", OTHER_CODE: "长江电力"}
    types = {"semi_annual": ReportType.SEMI_ANNUAL, "annual": ReportType.ANNUAL}
    return ReportMeta(
        company_id=code, company_name=names[code], report_type=types[report_type],
        period=date.fromisoformat(period), download_url="fixture://eval/report.pdf",
        title=f"{names[code]}{period} {report_type}",
    )


def _install_pipeline(monkeypatch, env, tmp_path):  # noqa: F811
    """注入可控协作方：本地报告身份、行业分类、真实工具名与脚本化模型。"""
    monkeypatch.setattr(server, "BASE_DIR", str(tmp_path))  # 行业分类缓存不写仓库

    class FakeRagStore:
        def list_report_ids(self):
            return [SEMI_REPORT_ID, ANNUAL_REPORT_ID, PEER_REPORT_ID, OTHER_REPORT_ID]

    class FakeStockMcp:
        def call_tool(self, name, arguments=None, timeout=None):
            if name == "get_stock_basic_info":
                # 只有两家银行属于同一行业：600900 不能被选作同业样本
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

    monkeypatch.setattr(server, "rag_store", FakeRagStore())
    monkeypatch.setattr(server, "stock_mcp", FakeStockMcp())
    monkeypatch.setattr(server, "TavilyWebSearch", FakeSearch)
    monkeypatch.setattr(server, "rag_qa", _ScriptedRagQA())
    monkeypatch.setattr(server, "_mcp_tool_defs", lambda: to_openai_tools(
        [{"name": name} for name in _PROVIDER_TOOLS],
    ))


def _evidence_ids(run):
    ids = {evidence_id for fact in run["facts"] for evidence_id in fact["evidence_ids"]}
    for artifact in run["artifacts"]:
        ids.add(f"{artifact['report_id']}#p{artifact['page']}" if artifact["source"] == "pdf"
                else f"web:{artifact['url']}")
    for tool in run["tool_artifacts"]:
        ids.add(f"tool:{tool['provider']}:{tool['tool_name']}")
    return ids


def _used_sources(run):
    """运行实际使用的来源；只统计真正出现在证据/工具/事实里的来源。"""
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
    response = client.post("/api/chat/stream", json={
        "question": case["question"],
        "scope_mode": "auto" if case["scope"] == "company_industry" else "company_only",
        "focus_report": {"code": FOCUS_CODE, "period": "2026-06-30"},
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


def test_m2_every_evaluation_case_has_a_pipeline_script():
    """用例集与脚本一一对应：新增用例必须补脚本，否则评测会静默失真。"""
    assert set(_SCRIPTS) == {case["question"] for case in CASES}


@pytest.mark.parametrize("case", CASES, ids=[case["intent"] for case in CASES])
def test_m2_evaluation_case_runs_through_the_pipeline(client, env, monkeypatch, tmp_path, case):  # noqa: F811
    _install_pipeline(monkeypatch, env, tmp_path)
    events, run = _run_case(client, case)

    # 1) 真实意图分类必须与用例声明一致
    policy_event = _event(events, "policy_resolved")
    assert policy_event["intent"] == case["intent"]

    # 2) 允许来源：声明 market_data/web 时策略必须真的授予对应工具家族，
    #    且实际使用的来源不得超出声明范围
    allowed_tools = set(run["tool_policy"]["allowed_tools"])
    assert allowed_tools <= set(_PROVIDER_TOOLS + ["web_search"])
    assert "request_missing_reports" not in allowed_tools
    if "market_data" in case["allowed_sources"]:
        assert any(name.startswith("get_realtime") or name.startswith("get_quote") for name in allowed_tools)
    if "web" in case["allowed_sources"]:
        assert "web_search" in allowed_tools
    assert _used_sources(run) <= set(case["allowed_sources"])

    # 3) 禁止混入的报告：既不能进入检索/证据/事实，也不能落在冻结 Scope 内
    forbidden = set(case["forbidden_report_ids"])
    if forbidden:
        assert not (forbidden & set(run["retrieval_report_ids"]))
        assert not (forbidden & {artifact["report_id"] for artifact in run["artifacts"]})
        assert not (forbidden & {fact["company_code"] for fact in run["facts"]})
        forbidden_codes = {report_id.split(":", 1)[0] for report_id in forbidden}
        assert not (forbidden_codes & {company["code"] for company in (run["scope"] or {}).get("companies", [])})
        assert not (forbidden & set((run["scope"] or {}).get("report_ids", [])))

    # 4) 证据与状态：run 状态（执行）与核验状态（可信）分别是两个信号
    assert set(case["expected_fact_ids"]) <= _evidence_ids(run)
    assert run["status"] == case["expected_status"]
    assert run["verification_report"]["status"] == case["expected_verification"]
    if case.get("no_fact_outcome"):
        # 没有采纳任何事实时，运行不得既 completed 又核验 passed。
        assert run["facts"] == []
        assert run["status"] != "completed" or run["verification_report"]["status"] != "passed"
    if case["expected_verification"] == "blocked":
        # 不可核验的数值论断不得留在回答里。
        assert run["content"] == "未找到可核验的披露，不能确认该数值。"
