"""webapp.server API 测试（TestClient + monkeypatch 替换模块级组件）"""

import json
import os
import threading
import time
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import webapp.server as server
from financial_report_fetcher.models import DownloadStatus, ReportMeta, ReportType
from financial_report_fetcher.rag.ingest import IngestResult


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


class FakeIndex:
    """模拟 StockIndex（方法面足够即可，不继承）"""

    def start(self):
        pass

    def wait_ready(self, timeout=5.0):
        return True

    def search(self, q, limit=10):
        return [{"code": "600900", "name": "长江电力"}] if q == "长江" else []

    def company_name(self, code):
        return "长江电力" if code == "600900" else None

    def is_valid_code(self, code):
        return code == "600900"

    @property
    def is_ready(self):
        return True


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

    monkeypatch.setattr(server, "datasource", fake_ds)
    monkeypatch.setattr(server, "stock_index", FakeIndex())
    monkeypatch.setattr(server, "ai_client", fake_ai)
    monkeypatch.setattr(server, "analyzer", fake_analyzer)
    monkeypatch.setattr(server, "downloader", fake_dl)
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
        "make_task_manager": make_task_manager,
    }

    for manager in task_managers:
        manager.shutdown()


@pytest.fixture()
def client(env):
    with TestClient(server.app) as c:
        yield c


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
        assert result["dimensions"][0]["name"] == "财务摘要"
        assert result["markdown_path"].endswith(".md")

    def test_analyze_task_exposes_dimension_progress_while_running(self, client, env):
        """分析尚未完成时，任务接口应返回当前维度对应的持久进度。"""
        reported = threading.Event()
        release = threading.Event()
        report = env["fake_analyzer"].analyze.return_value

        def progressive_analyze(*args, **kwargs):
            kwargs["progress_callback"]({
                "stage": "dimension_started",
                "completed": 1,
                "total": 2,
                "dimension": "risk_warning",
                "name": "风险识别",
            })
            reported.set()
            release.wait(timeout=5.0)
            return report

        env["fake_analyzer"].analyze.side_effect = progressive_analyze
        try:
            response = client.post(
                "/api/reports/600900/2025-12-31/analyze",
                json={"dimensions": ["financial_summary", "risk_warning"]},
            )
            task_id = response.json()["task_id"]
            assert reported.wait(timeout=3.0)

            task = client.get(f"/api/tasks/{task_id}").json()
            assert task["status"] == "running"
            assert task["progress"] == pytest.approx(0.525)
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

        periods = [call.kwargs["meta"]["period"] for call in env["fake_analyzer"].analyze.call_args_list]
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
        # 第二次提问时 qa() 收到的 history 应含第一轮两条消息
        client.post(
            "/api/reports/600900/2025-12-31/chat",
            json={"question": "再细说下"},
        )
        history_arg = env["fake_analyzer"].qa.call_args.kwargs["history"]
        assert len(history_arg) == 2

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
        """未传 dimensions 时默认维度取自配置 analysis_dimensions"""
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
        call = env["fake_analyzer"].analyze.call_args
        assert call.kwargs["dimensions"] == ["financial_summary", "governance"]

    def test_analyze_pre_ingests_before_analysis(self, client, env, monkeypatch):
        """分析任务开始时先确保 RAG 摄取（前置 ingest），分析后再次连带分析报告"""
        events = []
        fake_svc = type("Fake", (), {
            "auto_ingest_report": lambda self, p: events.append("ingest"),
        })()
        monkeypatch.setattr(server, "rag_service", fake_svc)

        fake_analyzer = env["fake_analyzer"]

        def _analyze(*a, **k):
            events.append("analyze")
            return _FakeReport()

        fake_analyzer.analyze.side_effect = _analyze
        r = client.post("/api/reports/600900/2025-12-31/analyze", json={"dimensions": ["financial_summary"]})
        tid = r.json()["task_id"]
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if server.task_manager.get(tid)["status"] == "done":
                break
            time.sleep(0.01)
        assert events[:2] == ["ingest", "analyze"], f"应先摄取再分析：{events}"
        assert events.count("ingest") >= 2


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
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None):
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

    def test_chat_stream_sse_and_session_persist(self, client, env, monkeypatch, tmp_path):
        """流式端点：SSE 事件含 session/delta/done；会话消息持久化"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None):
                yield {"type": "delta", "text": "营收", "reasoning": ""}
                yield {"type": "delta", "text": "增长", "reasoning": ""}
                yield {"type": "done", "answer": "营收增长",
                       "reasoning": "", "citations": [], "model": "m",
                       "usage": {"total_tokens": 5}}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        r = client.post("/api/chat/stream", json={"question": "营收如何？"})
        assert r.status_code == 200
        body = r.text
        assert "event: session" in body
        assert "event: delta" in body
        assert "营收" in body
        assert "event: done" in body
        # 会话已持久化（user + assistant）
        sessions = store.list_sessions()
        assert len(sessions) == 1
        detail = store.get_session(sessions[0]["id"])
        assert detail["messages"] == [
            {"role": "user", "content": "营收如何？"},
            {"role": "assistant", "content": "营收增长"},
        ]

    def test_chat_stream_never_exposes_model_reasoning(self, client, env, monkeypatch, tmp_path):
        """SSE 只发送回答内容与可解释工具阶段，不发送模型私有推理。"""
        from webapp.chat_store import ChatStore

        monkeypatch.setattr(server, "chat_store", ChatStore(str(tmp_path / "sessions.json")))

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None):
                yield {"type": "delta", "text": "", "reasoning": "这是不应暴露的私有推理"}
                yield {"type": "delta", "text": "公开回答", "reasoning": "另一个私有片段"}
                yield {"type": "done", "answer": "公开回答",
                       "reasoning": "完整私有推理", "citations": [], "model": "m",
                       "usage": {"total_tokens": 3}}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        response = client.post("/api/chat/stream", json={"question": "问题"})

        assert response.status_code == 200
        assert "公开回答" in response.text
        assert "reasoning" not in response.text
        assert "私有推理" not in response.text

    def test_chat_stream_empty_retrieval_default_answer(self, client, env, monkeypatch, tmp_path):
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None):
                yield {"type": "empty"}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        r = client.post("/api/chat/stream", json={"question": "x"})
        body = r.text
        assert "未检索到相关内容" in body
        assert "event: done" in body
        assert store.list_sessions()[0]["message_count"] == 2

    def test_chat_stream_requires_rag(self, client, env, monkeypatch):
        monkeypatch.setattr(server, "rag_qa", None)
        r = client.post("/api/chat/stream", json={"question": "x"})
        assert r.status_code == 503


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
        """真实 TaskManager：取消后 stop_event 置位，analyze 抛取消异常 → 任务 cancelled"""
        import threading as _threading

        from financial_report_fetcher.exceptions import AnalysisCancelledError

        tm = env["make_task_manager"]()
        monkeypatch.setattr(server, "task_manager", tm)
        entered = _threading.Event()

        def _analyze(*a, **k):
            stop = k.get("stop_event")
            entered.set()
            if stop is not None:
                stop.wait(timeout=5.0)
                if stop.is_set():
                    raise AnalysisCancelledError("已停止")
            return _FakeReport()

        env["fake_analyzer"].analyze.side_effect = _analyze
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
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None):
                yield {"type": "delta", "text": "部分回答", "reasoning": ""}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        r = client.post("/api/chat/stream", json={"question": "营收如何？"})
        assert r.status_code == 200
        assert "部分回答" in r.text
        sid = store.list_sessions()[0]["id"]
        detail = store.get_session(sid)
        assert [m["content"] for m in detail["messages"]] == ["营收如何？", "部分回答"]


class TestChatStreamConcurrency:
    def test_chat_stream_concurrent_sessions(self, client, env, monkeypatch, tmp_path):
        """多个会话可同时发起流式请求：互不阻塞，各自完成并落盘会话"""
        import time as _time

        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None):
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
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None):
                yield {"type": "tool_call", "name": "get_financial_metrics",
                       "arguments": {"symbol": "600519"}}
                yield {"type": "tool_result", "name": "get_financial_metrics",
                       "summary": '{"net_profit": 345}'}
                yield {"type": "done", "answer": "结合MCP数据：净利345亿",
                       "reasoning": "", "citations": [], "model": "m",
                       "usage": {}, "tools_used": ["get_financial_metrics"]}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        r = client.post("/api/chat/stream", json={"question": "净利如何？"})
        body = r.text
        assert "event: tool_call" in body
        assert "get_financial_metrics" in body
        assert "event: tool_result" in body
        assert "tools_used" in body
        assert "event: done" in body
        assert store.list_sessions()[0]["message_count"] == 2

    def test_chat_stream_use_mcp_false_still_streams(self, client, env, monkeypatch, tmp_path):
        """use_mcp=false 时仍正常流式（工具开关不改变响应结构）"""
        from webapp.chat_store import ChatStore

        store = ChatStore(str(tmp_path / "sessions.json"))
        monkeypatch.setattr(server, "chat_store", store)

        class FakeRagQA:
            def answer_stream(self, question, history=None, filters=None, tools=None, priority_report_id=None):
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
                              priority_report_id=None):
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

        monkeypatch.setattr(server, "stock_mcp", FakeMCP())
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
        monkeypatch.setattr(server, "stock_mcp", FakeMCP())
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
    def test_mcp_tool_defs_none_when_list_fails(self, monkeypatch):
        """MCP 清单获取失败时不注入工具（避免模型调用必失败的工具）"""
        class FakeMCP:
            def list_tools(self, timeout=None):
                raise RuntimeError("MCP 不可用")

        monkeypatch.setattr(server, "stock_mcp", FakeMCP())
        monkeypatch.setattr(server, "_mcp_tool_defs_cache", None)
        monkeypatch.setattr(server, "_mcp_tool_defs_ready", False)
        assert server._mcp_tool_defs() is None

    def test_mcp_tool_defs_built_when_available(self, monkeypatch):
        """MCP 可用时构建工具定义并缓存"""
        class FakeMCP:
            def list_tools(self, timeout=None):
                return [
                    {"name": "get_realtime_quote", "description": "实时行情",
                     "input_schema": {"type": "object", "properties": {}}},
                ]

        monkeypatch.setattr(server, "stock_mcp", FakeMCP())
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
        monkeypatch.setattr(server, "stock_mcp", FakeMCP())
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
        """非实时行情工具仍走 stock_mcp"""
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
        monkeypatch.setattr(server, "stock_mcp", FakeMCP())
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
        monkeypatch.setattr(server, "stock_mcp", type("M", (), {"call_tool": lambda *a, **k: "{}"})())
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
        """诊断端点启动服务检测并返回结果"""
        class FakeMCP:
            def list_tools(self, timeout=None):
                return [{"name": "get_realtime_data"}, {"name": "get_financial_metrics"}]

        monkeypatch.setattr(server, "stock_mcp", FakeMCP())
        r = client.post("/api/mcp/diagnose")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert "2" in r.json()["message"]


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
        app_src = "/static/app.js?v="
        assert workflow_src in body
        assert "/static/app.js?v=" in body
        assert "/static/style.css?v=" in body
        assert body.index(workflow_src) < body.index(app_src)


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
                              priority_report_id=None):
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
                              priority_report_id=None):
                captured["priority"] = priority_report_id
                yield {"type": "done", "answer": "x", "reasoning": "", "citations": [],
                       "model": "m", "usage": {}, "tools_used": []}

        monkeypatch.setattr(server, "rag_qa", FakeRagQA())
        r = client.post("/api/chat/stream", json={"question": "营收如何？"})
        assert r.status_code == 200
        assert captured["priority"] is None
