"""Export immutable trusted-chat runs as reviewable Markdown or canonical JSON.

The exporter is deliberately a view: it neither updates an ``AnswerRun`` nor
synthesizes a scope, evidence, or execution state that was not persisted.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
import re
from typing import Any
from urllib.parse import unquote, urlparse

from webapp.chat_models import AnswerRun, EvidenceArtifact, Fact
from webapp.research_models import ResearchRun


class ExportValidationError(ValueError):
    """Raised when an immutable run cannot support a reviewable export."""


_PDF_URL_RE = re.compile(
    r"^/api/history-pdf/([^/?#]+)\?jump=(0|[1-9]\d*)#page=([1-9]\d*)$"
)


def _exported_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _markdown_text(value: object) -> str:
    """Render untrusted persisted text as literal Markdown, never syntax."""
    if not isinstance(value, str):
        return ""
    return (
        value.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("<", "\\<")
        .replace(">", "\\>")
    )


def _validated_pdf_url(artifact: EvidenceArtifact) -> str | None:
    """Accept only the exact local PDF-page URL shape emitted by M1.

    ``EvidenceArtifact`` intentionally preserves unavailable/missing-file evidence,
    and its ``pdf_url`` field is only a string at the model boundary.  Rechecking
    the M1 local route shape before interpolating it into Markdown avoids turning a
    historic or malformed artifact value into an export-time link.
    """
    if artifact.source != "pdf" or not artifact.pdf_url or not isinstance(artifact.page, int):
        return None
    match = _PDF_URL_RE.fullmatch(artifact.pdf_url)
    if match is None or int(match.group(3)) != artifact.page:
        return None
    filename = unquote(match.group(1))
    if (
        not filename
        or filename != os.path.basename(filename)
        or "/" in filename
        or "\\" in filename
        or not filename.lower().endswith(".pdf")
        or any(ord(char) < 32 for char in filename)
    ):
        return None
    return artifact.pdf_url


def _validated_web_url(artifact: EvidenceArtifact) -> str | None:
    """Return an M1-valid web artifact URL only when safe for a Markdown href."""
    if artifact.source != "web" or not artifact.url:
        return None
    if any(char.isspace() or ord(char) < 32 or char in "[]<>" for char in artifact.url):
        return None
    try:
        parsed = urlparse(artifact.url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return artifact.url


class ResearchExporter:
    """Build reviewable exports from persisted, immutable M1--M3 contracts."""

    @staticmethod
    def _validate(run: AnswerRun, research_run: ResearchRun | None) -> None:
        if not isinstance(run, AnswerRun):
            raise ExportValidationError("导出必须引用 AnswerRun")
        if run.scope is None:
            raise ExportValidationError("导出缺少冻结范围")
        if run.status not in {"completed", "partial", "stopped", "failed", "waiting_consent"}:
            raise ExportValidationError("导出缺少运行状态")
        if run.legacy_evidence_unavailable or not (run.artifacts or run.tool_artifacts):
            raise ExportValidationError("导出缺少可复核证据")
        if research_run is not None:
            if not isinstance(research_run, ResearchRun):
                raise ExportValidationError("研究步骤必须引用 ResearchRun")
            # An export is a review record: a research run that is not this answer's
            # own run (or that froze another scope) must never be merged into it.
            if run.research_run_id != research_run.id:
                raise ExportValidationError("研究步骤与 AnswerRun 的来源不一致")
            if run.scope != research_run.plan.scope:
                raise ExportValidationError("研究步骤与 AnswerRun 的范围不一致")

    def to_json(self, run: AnswerRun, research_run: ResearchRun | None) -> dict[str, Any]:
        """Return the full canonical payload, with no presentation-time summary."""
        self._validate(run, research_run)
        return {
            "answer_run": run.to_dict(),
            "research_run": research_run.to_dict() if research_run is not None else None,
            "exported_at": _exported_at(),
        }

    def to_markdown(self, run: AnswerRun, research_run: ResearchRun | None) -> str:
        """Return a fixed-order review record while keeping incomplete states visible."""
        self._validate(run, research_run)
        scope = run.scope
        assert scope is not None  # narrowed by _validate

        lines = [
            "# 可复核研究纪要",
            "",
            f"> 运行状态：{run.status}",
            "",
            "## 范围",
            f"- 范围模式：{scope.mode}",
            f"- 公司：{self._companies(scope.companies)}",
            f"- 报告：{self._items(scope.report_ids)}",
            f"- 行业：{_markdown_text(scope.industry.name) if scope.industry else '未限定'}",
            f"- 来源策略：{self._source_policy(scope.source_policy.to_dict())}",
            "",
            "## 结论",
            _markdown_text(run.content) or "（无结论正文）",
            "",
            "## 关键事实",
            *self._facts(run.facts),
            "",
            "## 证据",
            *self._evidence(run.artifacts),
            "",
            "## 外部数据",
            *self._external_data(run),
            "",
            "## 冲突与限制",
            *self._conflicts_and_limits(run),
            "",
            "## 运行状态",
            f"- 运行状态：{run.status}",
            f"- 验证状态：{run.verification_report.status if run.verification_report else '未提供'}",
            f"- AnswerRun：{_markdown_text(run.id) or '未提供'}",
        ]
        if run.created_at:
            lines.append(f"- 创建时间：{_markdown_text(run.created_at)}")
        if run.completed_at:
            lines.append(f"- 完成时间：{_markdown_text(run.completed_at)}")
        if research_run is not None:
            lines.extend(("", "## 研究步骤", *self._research_steps(research_run)))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _items(values: tuple[str, ...]) -> str:
        return "、".join(_markdown_text(value) for value in values) or "未限定"

    @staticmethod
    def _companies(companies: tuple[Any, ...]) -> str:
        return "、".join(
            f"{_markdown_text(company.name)}（{_markdown_text(company.code)}）"
            for company in companies
        ) or "未限定"

    @staticmethod
    def _source_policy(policy: dict[str, bool]) -> str:
        return "；".join(
            f"{_markdown_text(name)}={'允许' if enabled else '禁止'}"
            for name, enabled in policy.items()
        )

    @staticmethod
    def _facts(facts: tuple[Fact, ...]) -> list[str]:
        if not facts:
            return ["- 未形成结构化事实。"]
        result = []
        for fact in facts:
            as_of = f"；数据截至：{_markdown_text(fact.as_of)}" if fact.as_of else ""
            result.append(
                f"- {_markdown_text(fact.metric)}：{fact.value} {_markdown_text(fact.unit)}"
                f"（期间：{_markdown_text(fact.period)}；核验：{fact.verification}；"
                f"证据：{ResearchExporter._items(fact.evidence_ids)}{as_of}）"
            )
        return result

    @staticmethod
    def _evidence(artifacts: tuple[EvidenceArtifact, ...]) -> list[str]:
        if not artifacts:
            return ["- 未提供文档或网页证据；外部工具证据见“外部数据”。"]
        result = []
        for artifact in artifacts:
            if artifact.source == "pdf":
                label = f"PDF 第 {artifact.page} 页"
                url = _validated_pdf_url(artifact)
                reference = f"[{label}]({url})" if url else f"{label}（链接不可用）"
                result.append(
                    f"- {reference}：{_markdown_text(artifact.report_id)}；"
                    f"{_markdown_text(artifact.snippet)}；可用性：{_markdown_text(artifact.availability)}"
                )
                continue
            url = _validated_web_url(artifact)
            label = _markdown_text(artifact.title) or "网页来源"
            reference = f"[{label}]({url})" if url else f"{label}（链接不可用）"
            result.append(
                f"- {reference}；抓取时间：{_markdown_text(artifact.fetched_at)}；"
                f"{_markdown_text(artifact.snippet)}"
            )
        return result

    @staticmethod
    def _external_data(run: AnswerRun) -> list[str]:
        result: list[str] = []
        for artifact in run.tool_artifacts:
            as_of = _markdown_text(artifact.as_of) or "未提供"
            result.append(
                f"- 工具：{_markdown_text(artifact.provider)}/{_markdown_text(artifact.tool_name)}；"
                f"状态：{_markdown_text(artifact.status)}；数据截至：{as_of}"
            )
        for artifact in run.artifacts:
            if artifact.source == "web":
                result.append(
                    f"- 网页来源：{_markdown_text(artifact.title)}；"
                    f"数据截至：{_markdown_text(artifact.fetched_at) or '未提供'}"
                )
        for fact in run.facts:
            if fact.source_type in {"tool", "web"}:
                result.append(
                    f"- 参考事实：{_markdown_text(fact.metric)}；"
                    f"核验：{fact.verification}；数据截至：{_markdown_text(fact.as_of) or '未提供'}"
                )
        return result or ["- 无外部数据。"]

    @staticmethod
    def _conflicts_and_limits(run: AnswerRun) -> list[str]:
        result = [
            f"- 验证状态：{run.verification_report.status if run.verification_report else '未提供'}"
        ]
        for conflict in run.conflicts:
            result.append(
                f"- 冲突：{_markdown_text(conflict.metric)}；{_markdown_text(conflict.reason)}"
            )
        if run.verification_report:
            for issue in run.verification_report.issues:
                result.append(
                    f"- 核验限制（{_markdown_text(issue.severity)}/{_markdown_text(issue.code)}）："
                    f"{_markdown_text(issue.message)}"
                )
        if not run.conflicts and not (run.verification_report and run.verification_report.issues):
            result.append("- 未记录冲突或额外核验限制。")
        return result

    @staticmethod
    def _research_steps(research_run: ResearchRun) -> list[str]:
        result = [
            f"- ResearchRun：{_markdown_text(research_run.id)}；状态：{research_run.status}",
            f"- 研究目标：{_markdown_text(research_run.plan.objective)}",
        ]
        step_runs = {step_run.step_id: step_run for step_run in research_run.step_runs}
        for step in research_run.plan.steps:
            step_run = step_runs.get(step.id)
            status = step_run.status if step_run else "pending"
            summary = step_run.result_summary if step_run else ""
            result.append(
                f"- 步骤：{_markdown_text(step.label)}（{step.kind}）；状态：{status}"
                + (f"；结果：{_markdown_text(summary)}" if summary else "")
            )
        return result
