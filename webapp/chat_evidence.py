"""证据标准化：把 RAG 引用、网页来源与工具事件转换为可信问答契约对象。

规范化只做「降低可信度」方向的转换，绝不补造来源：

- PDF 证据的跳页 URL 只在 ``report_id`` 能映射到本地已下载 PDF 时才生成；映射依次取
  ``build_flat_history`` 的真实文件名与分析产物 ``meta.source_file``，文件缺失时保留
  artifact 但标记 ``missing_file`` 且不生成 URL。
- 网页证据只接受 http/https URL，并必须带抓取时间，否则不生成 artifact。
- 工具参数按白名单保留并脱敏，工具返回体只保留截断摘要。
- 财报 Fact 只能由 ``source_type == "pdf"`` 且带页码的证据产生，因此本模块不会把
  工具/网页 artifact 自动升级为财报 Fact；工具结果只可能产出 ``reference`` 级 Fact。
"""

from __future__ import annotations

import json
import os
import re
from numbers import Real
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlparse

from .chat_models import EvidenceArtifact, Fact, ToolArtifact
from financial_report_fetcher.report_identity import derive_analysis_report_id

from .history import ANALYSIS_SUFFIX, build_flat_history, get_analysis_detail

# 历史记录 PDF 路由（与 webapp/server.py /api/history-pdf/{filename} 对齐）
HISTORY_PDF_PATH = "/api/history-pdf/"
PDF_EXTENSION = ".pdf"

# 工具返回摘要上限：不保存完整不可控返回体
RESULT_SUMMARY_MAX_CHARS = 500

# 工具参数白名单：只保留业务参数，密钥类字段一律丢弃
SAFE_ARGUMENT_KEYS = frozenset({
    "code",
    "company_code",
    "symbol",
    "name",
    "report_id",
    "report_type",
    "period",
    "report_period",
    "date",
    "start_date",
    "end_date",
    "metric",
    "metrics",
    "indicator",
    "indicators",
    "industry",
    "market",
    "query",
    "keyword",
    "keywords",
    "page",
    "limit",
    "top_k",
})

# 结构化工具结果受控字段：缺任一字段都不构成可核验 Fact
STRUCTURED_FACT_FIELDS = ("metric", "value", "unit", "period", "company_code", "as_of")

_ARGUMENT_VALUE_MAX_CHARS = 120
_SECRET_IN_VALUE_RE = re.compile(
    r"(?i)\b(api[-_]?key|access[-_]?token|token|secret|password|passwd|authorization)\b\s*[:=]\s*\S+"
)


def _text(value: Any) -> str:
    """宽松文本取值：非字符串（含 None）一律视为空。"""
    return value.strip() if isinstance(value, str) else ""


def _positive_page(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _redact_secrets(text: str) -> str:
    """抹掉 ``token=xxx`` 这类内嵌在参数值/返回体里的凭据。"""
    return _SECRET_IN_VALUE_RE.sub(lambda match: f"{match.group(1)}=[已隐藏]", text)


def pdf_page_url(filename: str, page: int, jump_version: int) -> str | None:
    """生成历史 PDF 跳页链接；输入不可信/不可用时返回 None（绝不生成伪链接）。

    仅接受单层文件名 + 正整数页码；``jump_version`` 作为缓存破坏参数由调用方提供，
    用于 iframe 重复跳同一页时强制重新加载。
    """
    if not isinstance(filename, str) or not filename:
        return None
    if "/" in filename or "\\" in filename or filename != os.path.basename(filename):
        return None
    if not filename.lower().endswith(PDF_EXTENSION):
        return None
    resolved_page = _positive_page(page)
    if resolved_page is None:
        return None
    if isinstance(jump_version, bool) or not isinstance(jump_version, int) or jump_version < 0:
        return None
    return f"{HISTORY_PDF_PATH}{quote(filename, safe='')}?jump={jump_version}#page={resolved_page}"


def _history_pdf_filenames(analysis_dir: str, reports_dir: str) -> dict[str, str]:
    """``report_id`` 的 ``code:period`` → 实际 ``pdf_filename``（仅本地存在 PDF 的行）"""
    mapping: dict[str, str] = {}
    for item in build_flat_history(analysis_dir, reports_dir):
        pdf_filename = _text(item.get("pdf_filename"))
        code = _text(item.get("code"))
        period = _text(item.get("period"))
        if pdf_filename and code and period:
            mapping[f"{code}:{period}"] = pdf_filename
    return mapping


def _analysis_source_filenames(analysis_dir: str) -> dict[str, str]:
    """``report_id`` → 分析产物记录的原始 PDF 文件名（PDF 已被删除时仍可定位来源）。

    只在历史扫描没给出文件名时使用；文件名取 ``meta.source_file`` 的 basename，
    不猜测、不拼接，缺失记录的文件保持缺失语义（``missing_file``）。
    """
    mapping: dict[str, str] = {}
    if not os.path.isdir(analysis_dir):
        return mapping
    for filename in sorted(os.listdir(analysis_dir)):
        if not filename.endswith(ANALYSIS_SUFFIX):
            continue
        content = get_analysis_detail(analysis_dir, filename)
        if content is None:
            continue
        meta = content.get("meta") if isinstance(content, Mapping) else None
        if not isinstance(meta, Mapping):
            continue
        report_id = derive_analysis_report_id(filename, dict(meta))
        source_file = os.path.basename(_text(meta.get("source_file")))
        if report_id and source_file.lower().endswith(PDF_EXTENSION):
            mapping.setdefault(report_id, source_file)
    return mapping


def _local_pdf_exists(reports_dir: str, pdf_filename: str) -> bool:
    try:
        path = os.path.join(reports_dir, os.path.basename(pdf_filename))
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _report_scope_key(report_id: str) -> str:
    """``code:period:type`` → ``code:period``（与 build_flat_history 的键一致）"""
    parts = report_id.split(":")
    if len(parts) < 2:
        return ""
    return f"{parts[0]}:{parts[1]}"


def _safe_arguments_summary(arguments: Any) -> str:
    """白名单过滤 + 脱敏后的参数摘要；非受控结构一律丢弃。"""
    if not isinstance(arguments, Mapping):
        return ""
    filtered: dict[str, Any] = {}
    for key, value in arguments.items():
        if not isinstance(key, str) or key not in SAFE_ARGUMENT_KEYS:
            continue
        if isinstance(value, str):
            filtered[key] = _redact_secrets(value)[:_ARGUMENT_VALUE_MAX_CHARS]
        elif isinstance(value, bool) or value is None:
            filtered[key] = value
        elif isinstance(value, (int, float)):
            filtered[key] = value
        elif isinstance(value, (list, tuple)):
            filtered[key] = _redact_secrets(
                json.dumps([item for item in value if isinstance(item, (str, int, float, bool))], ensure_ascii=False)
            )[:_ARGUMENT_VALUE_MAX_CHARS]
    return json.dumps(filtered, ensure_ascii=False, sort_keys=True)


def _safe_result_summary(summary: Any) -> str:
    """脱敏并截断的工具返回摘要；不保存完整不可控返回体。"""
    if not isinstance(summary, str):
        return ""
    collapsed = re.sub(r"\s+", " ", summary).strip()
    return _redact_secrets(collapsed)[:RESULT_SUMMARY_MAX_CHARS]


class EvidenceNormalizer:
    """问答证据标准化器；全部方法为纯函数式，输入取自 RAG/工具/网页的原始事件。"""

    def normalize_rag_citations(
        self,
        citations: Sequence[Mapping[str, Any]],
        *,
        analysis_dir: str,
        reports_dir: str,
        jump_version: int,
    ) -> tuple[EvidenceArtifact, ...]:
        """RAG 引用 → PDF 证据 artifact。

        只处理 ``source == "pdf"``、页码为正整数、片段非空且能映射到本地 PDF 文件名的
        引用：``analysis`` 等其他来源、缺失页码或缺失片段都无法构成可跳页的 PDF 证据，
        因此直接跳过而不是补造文件名或页码。已经删除但仍有分析产物的报告保留为
        ``missing_file`` 证据，提醒用户来源不可用。
        """
        history_pdfs = _history_pdf_filenames(analysis_dir, reports_dir)
        analysis_pdfs: dict[str, str] | None = None
        artifacts: list[EvidenceArtifact] = []
        for citation in citations:
            if not isinstance(citation, Mapping):
                continue
            if _text(citation.get("source")) != "pdf":
                continue
            page = _positive_page(citation.get("page"))
            if page is None:
                continue
            snippet = _text(citation.get("snippet"))
            if not snippet:
                continue
            report_id = _text(citation.get("report_id"))
            pdf_filename = history_pdfs.get(_report_scope_key(report_id), "")
            if not pdf_filename:
                if analysis_pdfs is None:
                    analysis_pdfs = _analysis_source_filenames(analysis_dir)
                pdf_filename = analysis_pdfs.get(report_id, "")
            if not pdf_filename:
                continue
            if _local_pdf_exists(reports_dir, pdf_filename):
                artifacts.append(EvidenceArtifact.pdf(
                    report_id,
                    pdf_filename,
                    page,
                    snippet,
                    pdf_url=pdf_page_url(pdf_filename, page, jump_version),
                ))
            else:
                artifacts.append(EvidenceArtifact.pdf(
                    report_id,
                    pdf_filename,
                    page,
                    snippet,
                    pdf_url=None,
                    availability="missing_file",
                ))
        return tuple(artifacts)

    def normalize_web_sources(
        self,
        rows: Sequence[Mapping[str, Any]],
        fetched_at: str,
    ) -> tuple[EvidenceArtifact, ...]:
        """网页搜索行 → 网页证据 artifact；只接受 http/https 且带抓取时间的行。

        无正文摘要的行用标题占位，避免丢掉模型已经使用的来源（artifact 契约要求
        ``snippet`` 非空），但绝不把工具/网页内容升级为 Fact。
        """
        fetched = _text(fetched_at)
        if not fetched:
            return ()
        artifacts: list[EvidenceArtifact] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            url = _text(row.get("url"))
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            title = _text(row.get("title")) or url
            snippet = _text(row.get("content")) or _text(row.get("snippet")) or title
            artifacts.append(EvidenceArtifact.web(
                url,
                title,
                snippet,
                published_at=_text(row.get("published_at")) or _text(row.get("published_date")),
                fetched_at=fetched,
            ))
        return tuple(artifacts)

    def normalize_tool_event(
        self,
        name: str,
        arguments: Any,
        summary: Any,
        *,
        provider: str,
        as_of: str,
        ok: bool,
    ) -> ToolArtifact:
        """工具事件 → 工具 artifact；参数经白名单/脱敏，返回体只留截断摘要。

        成功但缺少 ``as_of`` 的工具事件没有「数据截至」时间，构造 artifact 时直接失败，
        避免出现看似可核验的工具来源。
        """
        return ToolArtifact(
            provider=provider,
            tool_name=name,
            as_of=as_of,
            status="success" if ok else "failed",
            arguments_summary=_safe_arguments_summary(arguments),
            result_summary=_safe_result_summary(summary),
        )

    def facts_from_structured_tool_payload(
        self,
        payload: Any,
        artifact: ToolArtifact,
    ) -> tuple[Fact, ...]:
        """受控 JSON 工具结果 → ``reference`` Fact；未知文本/缺字段一律返回空元组。

        仅当 payload 同时提供 ``metric/value/unit/period/company_code/as_of`` 六项受控字段，
        且工具 artifact 自身带 ``as_of`` 时才创建 Fact；Fact 的 ``as_of`` 取工具记录的「数据
        截至」时间，两个时间的逐条对账留给 M2。自由文本、网页摘要与缺字段 JSON 一律只作为
        artifact 存在，不会被当作财报事实。
        """
        if not isinstance(artifact, ToolArtifact) or not _text(artifact.as_of):
            return ()
        data = self._structured_payload(payload)
        if data is None:
            return ()
        for field in STRUCTURED_FACT_FIELDS:
            if field not in data:
                return ()
        metric = _text(data.get("metric"))
        unit = _text(data.get("unit"))
        period = _text(data.get("period"))
        company_code = _text(data.get("company_code"))
        as_of = _text(data.get("as_of"))
        value = data.get("value")
        if not metric or not unit or not period or not company_code or not as_of:
            return ()
        if isinstance(value, bool) or not isinstance(value, Real):
            return ()
        return (
            Fact.from_tool(
                metric=metric,
                value=value,
                unit=unit,
                tool=artifact,
                period=period,
                company_code=company_code,
            ),
        )

    @staticmethod
    def _structured_payload(payload: Any) -> Mapping[str, Any] | None:
        if isinstance(payload, Mapping):
            return payload
        if not isinstance(payload, str) or not payload.strip():
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, Mapping) else None
