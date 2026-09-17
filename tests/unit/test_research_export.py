"""Research exports retain immutable scope, evidence, conflicts, and runtime state."""

import pytest

from webapp.chat_models import (
    AnswerRun,
    CompanyRef,
    EvidenceArtifact,
    Fact,
    FactConflict,
    Scope,
    ToolArtifact,
    VerificationIssue,
    VerificationReport,
)
from webapp.research_models import ResearchPlan, ResearchRun, ResearchStep, ResearchStepRun


def _scope() -> Scope:
    return Scope.company_only(
        "601288", "农业银行", ("601288:2026-06-30:semi_annual",)
    )


def _pdf(*, pdf_url: str | None = "/api/history-pdf/601288-2026.pdf?jump=0#page=40") -> EvidenceArtifact:
    return EvidenceArtifact.pdf(
        "601288:2026-06-30:semi_annual",
        "601288-2026.pdf",
        40,
        "营业收入见原文披露",
        pdf_url=pdf_url,
    )


def _fact(*, verification: str = "verified") -> Fact:
    return Fact(
        "营业收入", 100.0, "亿元", "2026-06-30", "semi_annual_cumulative",
        "consolidated", "601288", "pdf", ("pdf-1",), verification,
    )


def _research_run() -> ResearchRun:
    scope = _scope()
    plan = ResearchPlan(
        "核验营业收入", scope,
        (ResearchStep("retrieve", "retrieve", "检索范围内 PDF"),),
        ("保留页码证据",),
    )
    return ResearchRun(
        "research-1", plan, "completed",
        (ResearchStepRun("retrieve", "completed", result_summary="已检索 PDF"),),
    )


def _completed_run() -> AnswerRun:
    return AnswerRun(
        id="answer-1",
        content="营业收入为 100 亿元。",
        status="completed",
        scope=_scope(),
        facts=(_fact(),),
        artifacts=(_pdf(),),
        tool_artifacts=(ToolArtifact("market", "quote", "2026-09-16T10:00:00+08:00", "success"),),
        verification_report=VerificationReport("passed", supported_fact_ids=("pdf-1",)),
        research_run_id="research-1",
        created_at="2026-09-16T09:00:00+08:00",
        completed_at="2026-09-16T10:00:00+08:00",
    )


def test_markdown_export_contains_scope_status_facts_and_pdf_page_links():
    from webapp.research_export import ResearchExporter

    text = ResearchExporter().to_markdown(_completed_run(), _research_run())

    assert "## 范围" in text
    assert "## 运行状态" in text
    assert "PDF 第 40 页" in text
    assert "/api/history-pdf/601288-2026.pdf?jump=0#page=40" in text
    assert "数据截至：2026-09-16T10:00:00+08:00" in text
    assert text.count("运行状态：completed") == 2
    assert [text.index(f"## {section}") for section in (
        "范围", "结论", "关键事实", "证据", "外部数据", "冲突与限制", "运行状态", "研究步骤",
    )] == sorted(text.index(f"## {section}") for section in (
        "范围", "结论", "关键事实", "证据", "外部数据", "冲突与限制", "运行状态", "研究步骤",
    ))


def test_export_includes_conflicts_and_does_not_hide_partial_status():
    from webapp.research_export import ResearchExporter

    verified = _fact()
    conflict_fact = _fact(verification="conflict")
    run = AnswerRun(
        id="answer-partial",
        content="不同披露口径不能合并为确定结论。",
        status="partial",
        scope=_scope(),
        facts=(verified, conflict_fact),
        conflicts=(FactConflict("营业收入", (verified, conflict_fact), "存在口径/时间差异"),),
        artifacts=(_pdf(),),
        verification_report=VerificationReport(
            "partial", (VerificationIssue("conflict", "warning", "需要人工复核"),), ("pdf-1",),
        ),
    )

    text = ResearchExporter().to_markdown(run, None)

    assert text.count("运行状态：partial") == 2
    assert "存在口径/时间差异" in text
    assert "验证状态：partial" in text


def test_export_rejects_run_without_scope_or_evidence():
    from webapp.research_export import ExportValidationError, ResearchExporter

    exporter = ResearchExporter()
    with pytest.raises(ExportValidationError, match="范围"):
        exporter.to_json(AnswerRun(content="旧回答", status="completed"), None)
    with pytest.raises(ExportValidationError, match="证据"):
        exporter.to_json(AnswerRun(content="无证据", status="completed", scope=_scope()), None)


def test_export_escapes_untrusted_markdown_and_only_links_validated_artifact_urls():
    from webapp.research_export import ResearchExporter

    run = AnswerRun(
        id="escaped",
        content="[模型链接](javascript:alert(1))",
        status="completed",
        scope=_scope(),
        artifacts=(_pdf(pdf_url="https://untrusted.example/report.pdf#page=40"),),
    )

    text = ResearchExporter().to_markdown(run, None)

    assert "\\[模型链接\\]\\(javascript:alert\\(1\\)\\)" in text
    assert "[PDF 第 40 页](" not in text
    assert "PDF 第 40 页（链接不可用）" in text


def test_json_export_is_full_canonical_payload_with_export_timestamp():
    from webapp.research_export import ResearchExporter

    run = _completed_run()
    research_run = _research_run()

    payload = ResearchExporter().to_json(run, research_run)

    assert payload["answer_run"] == run.to_dict()
    assert payload["research_run"] == research_run.to_dict()
    assert payload["exported_at"]
