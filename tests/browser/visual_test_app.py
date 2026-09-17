"""浏览器验收专用启动器：真实 FastAPI 应用，但注入本地 fake 协作方。

验收目标是前端可视化生命周期与可信问答的端到端契约，与真实 RAG 检索无关。
真实 PDF 摄取会在应用关闭时长时间等待后台线程，使验收结果依赖开发者本地的
``reports/`` 内容；外部行情接口则让测试依赖网络。两者都在这里换成不联网的
本地 fake，应用本身仍是 ``webapp.server:app``。

可信问答回归（tests/browser/test_chat_trust_flow.py）与补报授权回归
（tests/browser/test_chat_pdf_supplement.py）需要一个可控的
``rag_qa.answer_stream`` 来产出确定性的 Scope/PDF 证据/网页证据/停止运行与
补报请求，避免消耗模型配额或依赖真实网络。因此这里注入：

- ``_FakeRagStore``：提供固定本地报告身份，供 Scope 解析与同业样本判定；
- ``_FakeStockIndex`` / ``_FakeStockMcp``：固定公司名与行业分类，杜绝联网；
- ``_FakeRagQA``：确定性 ``answer_stream``；含「补报验收」的问题产出受控补报
  请求并在恢复轮据 Scope 是否包含补充报告给出答案；以「停止」结尾的问题不出
done，让服务端按 ``stopped`` 持久化，而不是伪装完整；
- ``_SupplementDatasource``：只对 fixture 公司返回补报元数据，其余身份失败快；
- ``_SupplementDownloader`` / ``_SupplementIngestion``：只写本地最小 PDF 并记录
  调用，不读取真实下载地址、不建立真实索引；
- 固定 reports/analysis fixture（含第 40 页来源 PDF），并重定向
  ``REPORTS_DIR``/``ANALYSIS_DIR``，保证 PDF 跳页 URL 指向真实存在的本地 PDF。

所有协作方身份均为 ``fixture://``，仅在测试进程内生效。每个进程启动都创建独立
临时目录与 ``ChatStore``，避免跨测试/历史运行残留。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import date

import requests


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webapp.server as server  # noqa: E402  （必须先定位仓库根目录再导入）
from financial_report_fetcher.models import DownloadStatus, ReportMeta, ReportType  # noqa: E402
from financial_report_fetcher.report_identity import build_report_filename  # noqa: E402
from webapp.chat_evidence import pdf_page_url  # noqa: E402
from webapp.chat_models import (  # noqa: E402
    AnswerRun, EvidenceArtifact, Fact, FactConflict, IntentDecision, Scope, VerificationReport,
)
from webapp.chat_store import ChatStore  # noqa: E402
from webapp.research_memory import ResearchMemoryStore  # noqa: E402
from webapp.research_models import ResearchPlan, ResearchRun, ResearchStep, ResearchStepRun  # noqa: E402
from webapp.research_workspace import ResearchWorkspaceStore  # noqa: E402


FIXTURE_REPORT_ID = "601288:2026-06-30:semi_annual"
FIXTURE_PEER_REPORT_ID = "600036:2026-06-30:semi_annual"
FIXTURE_PDF_FILENAME = "农业银行_601288_半年报_2026.pdf"

# 非空的最小 PDF 占位文件：历史 PDF 路由只校验文件存在与非空，浏览器测试不渲染其内容。
_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"% trusted-chat-browser fixture\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
    b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
)


class _FakeRagStore:
    """提供固定本地报告身份：目标报告 + 一家同行业 peer，供 Scope 解析使用。"""

    def list_report_ids(self):
        return [FIXTURE_REPORT_ID, FIXTURE_PEER_REPORT_ID]


class _FakeStockIndex:
    """固定公司名映射；``company_name`` 决定 Scope 首部的公司展示。"""

    _names = {"601288": "农业银行", "600036": "招商银行"}
    is_ready = True

    def company_name(self, code):
        return self._names.get(code)


class _FakeStockMcp:
    """固定行业分类；``get_stock_basic_info`` 只返回「银行业」，绝不联网。"""

    def call_tool(self, name, arguments=None, timeout=None):
        if name == "get_stock_basic_info":
            return json.dumps({"industry": "银行业"})
        return json.dumps({})


class _FixtureRequestLog:
    """浏览器验收协作方的可读请求记录；只允许 fixture:// 身份。"""

    def __init__(self) -> None:
        self.path = os.environ.get("BROWSER_FIXTURE_LOG", "")
        self.entries = []

    def record(self, kind: str, url: str) -> None:
        self.entries.append({"kind": kind, "url": url})
        if self.path:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(self.entries, handle, ensure_ascii=False)


class _SupplementDatasource:
    """仅对 fixture 公司返回确定性补报元数据；其余公司保持失败快（绝不联网）。

    真实数据源不会为任意股票代码凭空给出报告，因此这里也不得为任意代码返回行
    数据：否则其他浏览器回归会因夹具行被替换而侜幸通过。
    """

    FIXTURE_CODE = "601288"

    def __init__(self, request_log: _FixtureRequestLog) -> None:
        self.request_log = request_log
        self.reports = [
            ReportMeta(
                company_id="601288", company_name="农业银行",
                report_type=ReportType.SEMI_ANNUAL, period=date(2025, 6, 30),
                download_url="fixture://supplement/2025-semi.pdf",
                title="农业银行2025年半年度报告", disclosure_date=date(2025, 8, 29),
            ),
            ReportMeta(
                company_id="601288", company_name="农业银行",
                report_type=ReportType.ANNUAL, period=date(2024, 12, 31),
                download_url="fixture://supplement/2024-annual.pdf",
                title="农业银行2024年年度报告", disclosure_date=date(2025, 3, 28),
            ),
            ReportMeta(
                company_id="601288", company_name="农业银行",
                report_type=ReportType.QUARTERLY, period=date(2024, 9, 30),
                download_url="fixture://supplement/2024-q3.pdf",
                title="农业银行2024年第三季度报告", disclosure_date=date(2024, 10, 30),
            ),
            ReportMeta(
                company_id="601288", company_name="农业银行",
                report_type=ReportType.SEMI_ANNUAL, period=date(2024, 6, 30),
                download_url="fixture://supplement/2024-semi.pdf",
                title="农业银行2024年半年度报告", disclosure_date=date(2024, 8, 29),
            ),
            ReportMeta(
                company_id="601288", company_name="农业银行",
                report_type=ReportType.ANNUAL, period=date(2023, 12, 31),
                download_url="fixture://supplement/2023-annual.pdf",
                title="农业银行2023年年度报告", disclosure_date=date(2024, 3, 28),
            ),
        ]

    def fetch_reports(self, stock_code, report_types=None, start_date=None, end_date=None):
        if stock_code != self.FIXTURE_CODE:
            # 非 fixture 公司：与真实离线数据源一致地失败快，绝不凭空返回报告。
            raise requests.exceptions.RequestException(
                "浏览器验收只提供 fixture 公司报告，不访问外部数据源"
            )
        self.request_log.record("candidate_metadata", "fixture://supplement/candidates")
        wanted_types = {getattr(item, "value", item) for item in (report_types or [])}
        selected = []
        for report in self.reports:
            if wanted_types and report.report_type.value not in wanted_types:
                continue
            if start_date is not None and report.period < start_date:
                continue
            if end_date is not None and report.period > end_date:
                continue
            selected.append(report)
        return selected


class _SupplementDownloader:
    """只写最小本地 PDF 的下载替身；记录调用而绝不读取 report.download_url。"""

    def __init__(self, request_log: _FixtureRequestLog) -> None:
        self.request_log = request_log

    def download_one(self, report, storage_dir):
        self.request_log.record("download", "fixture://supplement/download")
        os.makedirs(storage_dir, exist_ok=True)
        with open(os.path.join(storage_dir, build_report_filename(report)), "wb") as handle:
            handle.write(_MINIMAL_PDF)
        return DownloadStatus.SUCCESS


class _SupplementIngestion:
    """不建立真实 RAG 索引的 PDF 摄取替身。

    仍需实现只读的 ``status``/``list_files``：共享启动器的 ``/api/rag/status`` 与
    ``/api/rag/files`` 会直接调用它们，缺方法会使页面请求变成 500。
    """

    def __init__(self, request_log: _FixtureRequestLog) -> None:
        self.request_log = request_log

    def auto_ingest_pdf(self, pdf_path):
        self.request_log.record("ingest", "fixture://supplement/ingest")
        return True

    def status(self):
        return {"store_path": "", "reports": {}, "total_chunks": 0, "warnings": []}

    def list_files(self):
        return []


class _FakeRagQA:
    """可控 RAG 问答替身：产出确定性的 Scope 证据/网页来源/停止运行事件。

    约定：问题以「停止」结尾时不产出 ``done``，仅产出部分 ``delta`` 后返回，
    服务端因此按 ``stopped`` 持久化（绝不伪装完整）。其余问题产出 completed
    运行，携带第 40 页 PDF 引用与一个网页来源。
    """

    def answer_stream(
        self,
        question,
        history=None,
        filters=None,
        tools=None,
        priority_report_id=None,
        scope=None,
        run_id=None,
        tool_policy=None,
    ):
        question = str(question or "").strip()
        if "补报验收" in question and not getattr(self, "_supplement_requested", False):
            self._supplement_requested = True
            yield {
                "type": "supplement_request",
                "reason": "需要补充财报原文以核对经营现金流。",
                "needs": [{"period": "2025-06-30", "report_type": "semi_annual"}],
            }
            return
        if "补报验收" in question:
            supplemented = "601288:2025-06-30:semi_annual" in tuple(getattr(scope, "report_ids", ()) or ())
            answer = (
                "已基于补充财报原文恢复回答。"
                if supplemented else "未补充财报，已基于现有信息回答。"
            )
            yield {"type": "delta", "text": answer}
            yield {
                "type": "done", "answer": answer,
                "citations": ([{
                    "source": "pdf", "report_id": "601288:2025-06-30:semi_annual",
                    "section": "现金流量表", "page": 40,
                    "snippet": "补充财报原文已索引",
                }] if supplemented else []),
                "web_sources": [], "tools_used": [],
                "retrieval_report_ids": (["601288:2025-06-30:semi_annual"] if supplemented else []),
                "retrieval_degraded": False, "model": "browser-acceptance-fake",
            }
            return
        if "研究计划" in question and "停止验收" in question:
            # Keep the real SSE request open long enough for browser UI to issue
            # its normal stop action; no direct event injection is used.
            time.sleep(2.0)
            answer = "已基于范围内披露完成来源核对。"
            yield {"type": "done", "answer": answer, "citations": [{
                "source": "pdf", "report_id": FIXTURE_REPORT_ID, "page": 40, "snippet": "范围内披露来源",
            }], "web_sources": [], "tools_used": [], "retrieval_report_ids": [FIXTURE_REPORT_ID],
                   "retrieval_degraded": False, "model": "browser-acceptance-fake"}
            return
        if "研究计划" in question and "失败验收" in question:
            attempts = getattr(self, "_research_failure_attempts", 0) + 1
            self._research_failure_attempts = attempts
            if attempts == 1:
                yield {"type": "error", "error": "fixture 研究步骤失败"}
                return
            answer = "已基于范围内披露恢复研究。"
            yield {"type": "done", "answer": answer, "citations": [{
                "source": "pdf", "report_id": FIXTURE_REPORT_ID, "page": 40, "snippet": "范围内披露来源",
            }], "web_sources": [], "tools_used": [], "retrieval_report_ids": [FIXTURE_REPORT_ID],
                   "retrieval_degraded": False, "model": "browser-acceptance-fake"}
            return
        if question.endswith("停止"):
            yield {"type": "delta", "text": "经营活动现金流量净额为 -621.69 亿元"}
            yield {"type": "delta", "text": "（最后一步尚未完成）"}
            return
        if "研究计划" in question:
            answer = "已基于范围内披露完成来源核对。"
            yield {"type": "delta", "text": answer}
            yield {"type": "done", "answer": answer, "citations": [{
                "source": "pdf", "report_id": FIXTURE_REPORT_ID, "section": "现金流量表",
                "page": 40, "snippet": "范围内披露来源",
            }], "web_sources": [], "tools_used": [], "retrieval_report_ids": [FIXTURE_REPORT_ID],
                   "retrieval_degraded": False, "model": "browser-acceptance-fake"}
            return
        if "实时验收" in question:
            yield {"type": "tool_call", "name": "get_quote", "arguments": {"symbol": "601288"}}
            yield {"type": "structured_tool_result", "name": "get_quote", "payload": {
                "metric": "price", "value": 3.2, "unit": "元/股", "period": "as_of",
                "period_kind": "point_in_time", "entity_scope": "consolidated",
                "company_code": "601288", "evidence_ids": ["tool:quote:601288"],
            }, "ok": True}
            yield {"type": "tool_result", "name": "get_quote", "summary": '{"price": 3.2}', "ok": True}
            yield {"type": "delta", "text": "外部参考：价格为 3.2 元/股。"}
            yield {"type": "done", "answer": "外部参考：价格为 3.2 元/股。", "citations": [],
                   "web_sources": [], "tools_used": ["get_quote"], "retrieval_report_ids": [],
                   "retrieval_degraded": False, "model": "browser-acceptance-fake"}
            return
        if "公告事件验收" in question:
            answer = "外部参考：公告事件仍需结合后续披露核对。"
            yield {"type": "delta", "text": answer}
            yield {"type": "done", "answer": answer, "citations": [], "tools_used": [],
                   "web_sources": [{"url": "https://www.abchina.com/cn/announcement/event",
                                    "title": "农业银行公告", "published_date": "2026-09-16",
                                    "content": "农业银行公告事件"}], "retrieval_report_ids": [],
                   "retrieval_degraded": False, "model": "browser-acceptance-fake"}
            return
        if "冲突验收" in question:
            for name, value in (("get_quote", 100), ("web_search", 120)):
                yield {"type": "tool_call", "name": name, "arguments": {"symbol": "601288"}}
                yield {"type": "structured_tool_result", "name": name, "payload": {
                    "metric": "revenue", "value": value, "unit": "亿元", "period": "2026-06-30",
                    "period_kind": "semi_annual_cumulative", "entity_scope": "consolidated",
                    "company_code": "601288", "evidence_ids": [f"tool:{name}:{value}"],
                }, "ok": True}
                yield {"type": "tool_result", "name": name, "summary": "{}", "ok": True}
            yield {"type": "delta", "text": "营业收入为 100 亿元。"}
            yield {"type": "done", "answer": "营业收入为 100 亿元。", "citations": [],
                   "web_sources": [], "tools_used": ["get_quote", "web_search"], "retrieval_report_ids": [],
                   "retrieval_degraded": False, "model": "browser-acceptance-fake"}
            return
        if "工具失败验收" in question:
            yield {"type": "tool_call", "name": "get_quote", "arguments": {"symbol": "601288"}}
            yield {"type": "tool_result", "name": "get_quote", "summary": "工具调用失败：fixture", "ok": False}
            yield {"type": "delta", "text": "实时数据暂不可用。"}
            yield {"type": "done", "answer": "实时数据暂不可用。", "citations": [], "web_sources": [],
                   "tools_used": [], "retrieval_report_ids": [], "retrieval_degraded": False,
                   "model": "browser-acceptance-fake"}
            return
        if "范围越界验收" in question:
            yield {"type": "tool_call", "name": "get_quote", "arguments": {"symbol": "600900"}}
            yield {"type": "structured_tool_result", "name": "get_quote", "payload": {
                "metric": "price", "value": 10, "unit": "元/股", "period": "as_of",
                "period_kind": "point_in_time", "entity_scope": "consolidated",
                "company_code": "600900", "evidence_ids": ["tool:quote:600900"],
            }, "ok": True}
            yield {"type": "tool_result", "name": "get_quote", "summary": '{"price": 10}', "ok": True}
            yield {"type": "delta", "text": "范围外数据未被采纳。"}
            yield {"type": "done", "answer": "范围外数据未被采纳。", "citations": [], "web_sources": [],
                   "tools_used": [], "retrieval_report_ids": [], "retrieval_degraded": False,
                   "model": "browser-acceptance-fake"}
            return
        if "行业验收" in question:
            answer = "已基于本地可检索同业样本进行比较。"
            yield {"type": "delta", "text": answer}
            yield {"type": "done", "answer": answer, "citations": [], "web_sources": [], "tools_used": [],
                   "retrieval_report_ids": [FIXTURE_REPORT_ID, FIXTURE_PEER_REPORT_ID],
                   "retrieval_degraded": False, "model": "browser-acceptance-fake"}
            return

        yield {"type": "delta", "text": "经营活动现金流量净额为 -621.69 亿元"}
        yield {
            "type": "done",
            "answer": "经营活动现金流量净额为 -621.69 亿元，主要受客户贷款及垫款净增加影响。",
            "citations": [
                {
                    "source": "pdf",
                    "report_id": FIXTURE_REPORT_ID,
                    "section": "现金流量表",
                    "page": 40,
                    "snippet": "经营活动产生的现金流量净额 -621.69 亿元",
                }
            ],
            "web_sources": [
                {
                    "url": "https://www.abchina.com/cn/announcement/2026-semi",
                    "title": "农业银行 2026 年半年度报告",
                    "published_date": "2026-08-31",
                    "content": "农业银行发布 2026 年半年度报告",
                }
            ],
            "tools_used": [],
            "retrieval_report_ids": [FIXTURE_REPORT_ID],
            "retrieval_degraded": False,
            "model": "browser-acceptance-fake",
        }


def _fixture_research_run(scope: Scope, run_id: str, status: str) -> ResearchRun:
    """Create a minimal persisted M3 run without any model or external collaborator."""
    plan = ResearchPlan(
        objective="fixture 研究验收",
        scope=scope,
        steps=(ResearchStep("retrieve", "retrieve", "本地证据核对", report_ids=scope.report_ids),),
        acceptance=("fixture evidence is persisted",),
    )
    step_status = "completed" if status == "completed" else "stopped"
    return ResearchRun(
        id=run_id,
        plan=plan,
        status=status,
        step_runs=(ResearchStepRun("retrieve", status=step_status, result_summary="fixture 摘要"),),
        started_at="2026-09-17T09:00:00+00:00",
        finished_at="2026-09-17T09:01:00+00:00",
    )


def _fixture_run_lookup(run_id: str):
    """Resolve a fixture AnswerRun from the launcher's temporary ChatStore only."""
    for record in server.chat_store.iter_session_runs():
        if record.run.id == run_id:
            return record.run
    return None


def _seed_workspace_fixtures(store: ChatStore) -> tuple[str, str, str]:
    """Persist completed/partial/stopped M4 fixtures and an explicit saved decision.

    Fixtures are immutable AnswerRun/Fact/Artifact/ResearchRun records.  They are
    intentionally written through ChatStore into the launcher's temporary directory
    and never call RAG ingestion, MCP, AI, web, or production data providers.
    """
    scope = Scope.company_only("601288", "农业银行", (FIXTURE_REPORT_ID,))
    artifact = EvidenceArtifact.pdf(
        FIXTURE_REPORT_ID,
        FIXTURE_PDF_FILENAME,
        40,
        "fixture 已验证 PDF 事实",
        pdf_url=pdf_page_url(FIXTURE_PDF_FILENAME, 40, 0),
    )
    fact = Fact(
        metric="经营活动现金流量净额", value=-621.69, unit="亿元", period="2026-06-30",
        period_kind="semi_annual_cumulative", entity_scope="consolidated", company_code="601288",
        source_type="pdf", evidence_ids=(f"{FIXTURE_REPORT_ID}#p40",), verification="verified",
    )
    external_reference = Fact(
        metric="外部参考价格", value=3.2, unit="元/股", period="as_of", period_kind="point_in_time",
        entity_scope="consolidated", company_code="601288", source_type="tool",
        evidence_ids=("tool:fixture:quote",), verification="reference", as_of="2026-09-17T09:00:00+00:00",
    )
    completed_research = _fixture_research_run(scope, "fixture-completed-research", "completed")
    completed = AnswerRun(
        id="fixture-completed-run", content="fixture 已验证 PDF 结论", status="completed", scope=scope,
        facts=(fact, external_reference), artifacts=(artifact,), intent_decision=IntentDecision(intent="report_fact"),
        verification_report=VerificationReport("passed", supported_fact_ids=fact.evidence_ids),
        research_run_id=completed_research.id,
        research_summary={"status": "completed", "decision_summaries": ["关注现金流变化"]},
        created_at="2026-09-17T09:00:00+00:00", completed_at="2026-09-17T09:01:00+00:00",
        model="browser-acceptance-fake",
    )

    reference = Fact(
        metric="参考价格", value=3.2, unit="元/股", period="as_of", period_kind="point_in_time",
        entity_scope="consolidated", company_code="601288", source_type="tool",
        evidence_ids=("tool:fixture:quote",), verification="reference", as_of="2026-09-17T09:00:00+00:00",
    )
    conflict = Fact(
        metric="营业收入", value=120, unit="亿元", period="2026-06-30", period_kind="semi_annual_cumulative",
        entity_scope="consolidated", company_code="601288", source_type="tool",
        evidence_ids=("tool:fixture:revenue",), verification="conflict", as_of="2026-09-17T09:00:00+00:00",
    )
    partial_research = _fixture_research_run(scope, "fixture-partial-research", "partial")
    partial = AnswerRun(
        id="fixture-reference-run", content="fixture 外部参考与冲突", status="partial", scope=scope,
        facts=(reference, conflict), artifacts=(artifact,), intent_decision=IntentDecision(intent="realtime_market", needs_local_pdf=False, needs_market_data=True),
        conflicts=(FactConflict("营业收入", (reference, conflict), "fixture 口径冲突"),),
        verification_report=VerificationReport("partial"), research_run_id=partial_research.id,
        research_summary={"status": "partial", "decision_summaries": []},
        created_at="2026-09-17T09:02:00+00:00", completed_at="2026-09-17T09:03:00+00:00",
        model="browser-acceptance-fake",
    )
    stopped_research = _fixture_research_run(scope, "fixture-stopped-research", "stopped")
    stopped = AnswerRun(
        id="fixture-stopped-run", content="fixture 已停止残片", status="stopped", scope=scope,
        artifacts=(artifact,), intent_decision=IntentDecision(intent="research_task"),
        research_run_id=stopped_research.id, research_summary={"status": "stopped"},
        created_at="2026-09-17T09:04:00+00:00", completed_at="2026-09-17T09:05:00+00:00",
        model="browser-acceptance-fake",
    )

    records = (("fixture 完成研究", completed, completed_research), ("fixture 部分研究", partial, partial_research), ("fixture 已停止研究", stopped, stopped_research))
    session_ids = []
    for title, run, research_run in records:
        session = store.create_session()
        assert store.append_turn(session["id"], question=title, run=run) is not None
        assert store.save_research_run(session["id"], research_run) is not None
        session_ids.append(session["id"])
    return tuple(session_ids)


def build_app():
    """返回关闭 RAG 摄取/外部数据源、注入可控 fake RAG 的实时应用。"""
    tmp_dir = tempfile.mkdtemp(prefix="trusted-chat-browser-")
    atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)
    # ``TaskManager()`` resolves its SQLite path from ``TASK_DB_PATH``, so without
    # this the acceptance app would open and write the repository's own
    # ``data/tasks.sqlite3`` instead of the launcher's temporary directory.
    os.environ["TASK_DB_PATH"] = os.path.join(tmp_dir, "tasks.sqlite3")
    reports_dir = os.path.join(tmp_dir, "reports")
    analysis_dir = os.path.join(reports_dir, "analysis")
    os.makedirs(analysis_dir, exist_ok=True)
    with open(os.path.join(reports_dir, FIXTURE_PDF_FILENAME), "wb") as handle:
        handle.write(_MINIMAL_PDF)

    request_log = _FixtureRequestLog()
    server.rag_store = _FakeRagStore()              # 固定本地报告身份
    server.rag_qa = _FakeRagQA()                    # 可控 answer_stream
    server.stock_index = _FakeStockIndex()          # 固定公司名
    server.stock_mcp = _FakeStockMcp()              # 固定行业分类
    server.ai_client.api_key = "browser-acceptance"  # _require_ai 通过；fake 不真正调模型
    server.datasource = _SupplementDatasource(request_log)
    server.downloader = _SupplementDownloader(request_log)
    server.rag_service = _SupplementIngestion(request_log)
    server.supplement_registry = server.SupplementRegistry()
    server._supplement_reports = {}
    server.REPORTS_DIR = reports_dir
    server.ANALYSIS_DIR = analysis_dir
    server.chat_store = ChatStore(os.path.join(tmp_dir, "chat_sessions.json"))
    # server.py's module globals are instantiated at import time; replace these
    # sidecars as well so browser acceptance cannot read repository/user state.
    server.research_workspace = ResearchWorkspaceStore(
        server.chat_store, os.path.join(tmp_dir, "research_workspace.json")
    )
    completed_session, _partial_session, _stopped_session = _seed_workspace_fixtures(server.chat_store)
    # This is fixture construction for an already explicit decision, not a product
    # auto-save path.  It verifies session deletion leaves independent memory alone.
    # The owner is the fixture session that already owns the source run.
    server.research_memory = ResearchMemoryStore(
        os.path.join(tmp_dir, "research_memory.json"),
        run_lookup=_fixture_run_lookup,
        owner_session_id=completed_session,
    )
    server.research_memory.save_decision(
        "关注现金流变化", "fixture-completed-run", (f"{FIXTURE_REPORT_ID}#p40",)
    )
    return server.app


def main() -> None:
    import uvicorn

    uvicorn.run(build_app(), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
