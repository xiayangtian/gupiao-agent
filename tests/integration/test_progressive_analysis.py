"""渐进式分析从提交、事件重连到终态快照的离线验收。"""

import time
from datetime import date
from types import SimpleNamespace

from fastapi.testclient import TestClient

import webapp.server as server
from financial_report_fetcher.models import ReportMeta, ReportType
from webapp.tasks import TaskManager


class _SeededDocument:
    def to_dict(self):
        return {
            "schema_version": 3,
            "analysis_id": "seeded-analysis",
            "report_id": "600900:2025-12-31:annual",
            "interests": ["cash_flow"],
            "stage": "completed",
            "quick": {"conclusions": [{"conclusion_id": "q1", "claim": "现金流改善"}]},
            "sections": [{
                "section_id": "cash-quality", "title": "现金流质量",
                "findings": [{"claim": "经营现金流改善"}] * 3,
            }],
            "evidence_catalog": {"e1": {"value": "100", "entity_scope": "consolidated"}},
        }


class _SeededPipeline:
    def run(self, request, emit, stop_event):
        document = _SeededDocument().to_dict()
        emit("job.stage_changed", {"stage": "fast_ready"})
        emit("quick.ready", {
            "quick": document["quick"], "evidence_catalog": document["evidence_catalog"]
        })
        emit("job.stage_changed", {"stage": "deep_processing"})
        emit("section.ready", {"section": document["sections"][0]})
        emit("job.completed", {"analysis": document})
        return SimpleNamespace(to_dict=lambda: document)


def _event_blocks(text):
    blocks = []
    for block in text.strip().split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                fields[key] = value
        if "event" in fields:
            blocks.append(fields)
    return blocks


def test_quick_then_deep_survives_reconnect_and_page_switch(monkeypatch, tmp_path):
    manager = TaskManager(db_path=str(tmp_path / "tasks.sqlite3"))
    meta = ReportMeta(
        company_id="600900", company_name="长江电力", report_type=ReportType.ANNUAL,
        period=date(2025, 12, 31), download_url="https://example.invalid/report.pdf",
        title="长江电力 2025 年报",
    )
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4\nseeded\n")
    monkeypatch.setattr(server, "task_manager", manager)
    monkeypatch.setattr(server, "progressive_pipeline", _SeededPipeline())
    monkeypatch.setattr(server, "ai_client", SimpleNamespace(api_key="sk-test"))
    monkeypatch.setattr(server, "_find_report_meta", lambda code, period: meta)
    monkeypatch.setattr(server, "_ensure_pdf", lambda report: str(pdf))
    monkeypatch.setattr(server, "_auto_ingest_report", lambda path: None)

    try:
        with TestClient(server.app) as client:
            started = client.post(
                "/api/reports/600900/2025-12-31/analyze",
                json={"interests": ["cash_flow"]},
            ).json()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                snapshot = client.get(started["status_url"]).json()
                if snapshot["status"] == "done":
                    break
                time.sleep(0.01)

            first = _event_blocks(client.get(started["event_url"] + "?after=0").text)
            quick = next(event for event in first if event["event"] == "quick.ready")
            replay = _event_blocks(client.get(
                started["event_url"] + "?after=" + quick["id"]
            ).text)

            assert first[0]["event"] == "job.stage_changed"
            assert any(event["event"] == "section.ready" for event in replay)
            assert all(int(event["id"]) > int(quick["id"]) for event in replay)
            assert snapshot["result"]["quick"]["conclusions"]
            assert snapshot["result"]["sections"]
            assert snapshot["result"]["stage"] == "completed"
    finally:
        manager.shutdown()
