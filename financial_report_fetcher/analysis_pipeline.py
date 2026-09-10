"""快速结论先行、主题逐步可见的证据化财报分析管线。"""

from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event
from time import perf_counter
from typing import Any, Callable, Sequence

from .analysis_config import AnalysisConfig
from .analysis_result import (
    AnalysisDocument,
    AnalysisError,
    EvidenceReference,
    EvidenceSummary,
    FilteredTopic,
    QuickResult,
    save_analysis_document,
)
from .evidence.models import (
    EntityScope,
    EvidenceRecord,
    SourceLocator,
    SourceType,
    VerificationState,
)
from .visualizations import VisualizationBundle
from .insights import (
    InsightCandidate,
    InsightScorer,
    RankedInsights,
    filter_and_rank,
    sanitize_section,
)


EventEmitter = Callable[[str, dict[str, Any]], None]


_PARENT_SHEET_PATTERN = re.compile(
    r"母公司\s*(资产负债表|利润表|现金流量表|所有者权益变动表)"
)
_CURRENT_PERIOD_CELL_FIELD = "current_period_pdf_cell"
_NUMERIC_CELL_PATTERN = re.compile(r"[-−]?\d[\d,，]*(?:\.\d+)?")
_PERIOD_NUMBER_PATTERN = re.compile(r"\d+")
# 表头与数据行的最大垂直距离，避免跨页/跨大段空白误配。
_MAX_HEADER_ROW_DISTANCE = 500
# 只有这些明确的报表表头才可定义行名列。没有可识别的标签列时宁可不生成
# 结构图证据，也不能把本期列左侧的上期/附注列误传给模型。
_ROW_LABEL_HEADERS = frozenset({"项目", "项目名称", "科目"})

_TOPIC_STRONG_KEYWORDS = {
    "profit_structure": ("营业收入", "营业成本", "净利润", "利润总额", "毛利"),
    "balance_sheet_structure": ("资产负债", "发放贷款", "客户存款"),
    "cash_flow_structure": ("现金流", "经营活动", "投资活动", "筹资活动"),
}
_TOPIC_KEYWORDS = {
    "profit_structure": ("收入", "营收", "盈利", "利润", "成本", "毛利"),
    "balance_sheet_structure": ("资产负债", "资产", "负债", "权益", "资本", "贷款", "存款"),
    "cash_flow_structure": ("现金流", "经营活动", "投资活动", "筹资活动"),
}


def _page_entity_scope(text: str) -> EntityScope:
    """从页面标题推断财报页主体：母公司报表页归母公司，其余归合并。"""
    if _PARENT_SHEET_PATTERN.search(text):
        return EntityScope.PARENT
    return EntityScope.CONSOLIDATED


def _normalized_period_digits(text: str) -> str:
    """把 2025年6月30日 / 2025-06-30 / 2025/6/30 归一到可比较的数字串。"""
    groups = _PERIOD_NUMBER_PATTERN.findall(text or "")
    if not groups:
        return ""
    parts = [groups[0].zfill(4)]
    parts.extend(group.zfill(2) for group in groups[1:3])
    return "".join(parts)


def _nearest_header_row_above(row: int, header_rows: set[int]) -> int | None:
    """PDF 纵坐标向上增长：最近的上方表头行是大于该行的最小值。"""
    candidates = [header_row for header_row in header_rows if header_row > row]
    return min(candidates) if candidates else None


@dataclass(frozen=True)
class AnalysisPipelineRequest:
    analysis_id: str
    report_id: str
    company_code: str
    company_name: str
    period: str
    pdf_path: str
    interests: tuple[str, ...] = ()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def analysis_output_stem(analysis_id: str) -> str:
    """把 analysis_id 转为可用的输出文件名（不含扩展名）。"""
    safe_id = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in analysis_id
    ).strip("_")
    if not safe_id:
        raise ValueError("analysis_id 不能生成空文件名")
    return safe_id


def _timed_call(call, *args):
    started = perf_counter()
    return call(*args), round((perf_counter() - started) * 1000, 3)


class ProgressiveAnalysisPipeline:
    def __init__(
        self,
        *,
        structured_gateway,
        document_extractor,
        ocr_engine,
        resolver,
        insight_planner,
        insight_scorer: InsightScorer,
        quick_analyzer,
        insight_analyzer,
        output_dir: str,
        config: AnalysisConfig | None = None,
        structure_visualizer=None,
    ):
        self.structured_gateway = structured_gateway
        self.document_extractor = document_extractor
        self.ocr_engine = ocr_engine
        self.resolver = resolver
        self.insight_planner = insight_planner
        self.insight_scorer = insight_scorer
        self.quick_analyzer = quick_analyzer
        self.insight_analyzer = insight_analyzer
        self.structure_visualizer = structure_visualizer
        self.output_dir = Path(output_dir)
        self.config = config or AnalysisConfig()
        self.quick_executor = ThreadPoolExecutor(
            max_workers=self.config.quick_io_workers, thread_name_prefix="analysis-quick"
        )
        self.deep_executor = ThreadPoolExecutor(
            max_workers=self.config.deep_workers, thread_name_prefix="analysis-deep"
        )
        self.ocr_executor = ThreadPoolExecutor(
            max_workers=self.config.ocr_workers, thread_name_prefix="analysis-ocr"
        )

    def _paths(self, request: AnalysisPipelineRequest) -> tuple[Path, Path]:
        safe_id = analysis_output_stem(request.analysis_id)
        return self.output_dir / f"{safe_id}.json", self.output_dir / f"{safe_id}.md"

    def _save(self, document: AnalysisDocument, request: AnalysisPipelineRequest) -> None:
        document.updated_at = _now()
        save_analysis_document(document, *self._paths(request))

    @staticmethod
    def pdf_records(extracted, period: str) -> list[EvidenceRecord]:
        """页面级 PDF 文本证据；快速结论与主题 AI 只采样这里的内容。"""
        records: list[EvidenceRecord] = []
        for page in extracted.pages:
            text = page.text.strip()
            if not text:
                continue
            digest = hashlib.sha256(
                f"{extracted.pdf_hash}:{page.page_number}:{text}".encode("utf-8")
            ).hexdigest()
            records.append(EvidenceRecord(
                report_id=extracted.report_id,
                entity_scope=_page_entity_scope(text),
                fact_name=f"pdf_page_{page.page_number}",
                value=None,
                unit=None,
                currency=None,
                period=period,
                source_type=SourceType.PDF_TEXT,
                source_locator=SourceLocator(
                    provider="pdf", page=page.page_number, record_id=f"page-{page.page_number}"
                ),
                extraction_confidence=page.quality_score,
                verification_state=VerificationState.UNKNOWN_SCOPE,
                content_hash=digest,
                parser_version=extracted.parser_version,
                text=text,
            ))
        return records

    @classmethod
    def visualization_cell_records(cls, extracted, period: str) -> list[EvidenceRecord]:
        """结构图专用的本期单元格证据，只传给结构抽取器。

        表格行证据既不适合作为通用分析证据，也会稀释快速/主题 AI 的
        文本采样，因此与页面级证据分开产出。
        """
        unique: dict[str, EvidenceRecord] = {}
        for page in extracted.pages:
            text = page.text.strip()
            if not text or _page_entity_scope(text) is not EntityScope.CONSOLIDATED:
                continue
            for record in cls._current_period_cell_records(extracted, page, period):
                unique.setdefault(record.stable_id, record)
        return list(unique.values())

    @staticmethod
    def _current_period_cell_records(extracted, page, period: str) -> list[EvidenceRecord]:
        """把有坐标表头明确标记为本期的同列数值转为独立 PDF 证据。"""
        fragments = tuple(getattr(page, "fragments", ()) or ())
        if not fragments:
            return []
        target = _normalized_period_digits(period)
        lines: dict[int, list[Any]] = {}
        for fragment in fragments:
            text = getattr(fragment, "text", "")
            if not isinstance(text, str) or not text.strip():
                continue
            lines.setdefault(round(float(getattr(fragment, "y", 0.0)) / 2), []).append(fragment)
        headers = [
            (fragment, sorted(line, key=lambda item: float(item.x)))
            for line in lines.values() for fragment in line
            if "本期" in fragment.text
            or (target and _normalized_period_digits(fragment.text) == target)
        ]
        if not headers:
            return []
        # 表头行本身不是数据行；同一页存在多张报表时，数据行只能归
        # 最近的上方表头，不能跨越中间表头行绑定到上一张报表。
        header_rows = {round(float(header.y) / 2) for header, _line in headers}
        nearest_header_row = {
            row: _nearest_header_row_above(row, header_rows) for row in lines
        }
        records: list[EvidenceRecord] = []
        for header, header_line in headers:
            header_row = round(float(header.y) / 2)
            # 两个相邻表头的中点才是可审计的列边界。没有左右边界（例如
            # PDF 只提取到一个日期表头）或表头坐标重叠时，绝不猜测列宽。
            try:
                header_index = next(
                    index for index, item in enumerate(header_line) if item is header
                )
            except StopIteration:
                continue
            if header_index == 0 or header_index == len(header_line) - 1:
                continue
            left_header = header_line[header_index - 1]
            right_header = header_line[header_index + 1]
            left_x = float(left_header.x)
            header_x = float(header.x)
            right_x = float(right_header.x)
            if not left_x < header_x < right_x:
                continue
            left_boundary = (left_x + header_x) / 2
            right_boundary = (header_x + right_x) / 2
            if not left_boundary < header_x < right_boundary:
                continue
            header_y = float(header.y)
            for line_y, line in lines.items():
                if line_y in header_rows or nearest_header_row[line_y] != header_row:
                    continue
                # PDF 坐标向上增长：表头下方的数据行 y 必须更小，且限制在合理距离内。
                y = line_y * 2
                if not 0 < header_y - y <= _MAX_HEADER_ROW_DISTANCE:
                    continue
                cells = [
                    fragment for fragment in line
                    if left_boundary < float(fragment.x) < right_boundary
                ]
                if not any(_NUMERIC_CELL_PATTERN.search(fragment.text) for fragment in cells):
                    continue
                # 用同一表头行中明确的“项目/科目”等标签表头定义行名列，而不是
                # 把本期列左侧全部片段当作行名：相邻的上期数值列也可能位于左侧。
                label_headers = [
                    (index, item) for index, item in enumerate(header_line)
                    if item.text.strip() in _ROW_LABEL_HEADERS and float(item.x) < header_x
                ]
                if len(label_headers) != 1:
                    continue
                label_index, label_header = label_headers[0]
                if label_index == len(header_line) - 1:
                    continue
                label_x = float(label_header.x)
                next_label_x = float(header_line[label_index + 1].x)
                if not label_x < next_label_x:
                    continue
                # 行名列左边界取表格已知最左列（或与左侧已知列的中点），
                # 不能用反射列宽猜测，否则表格左侧的标记会被并入行名。
                label_left_boundary = (
                    label_x if label_index == 0
                    else (float(header_line[label_index - 1].x) + label_x) / 2
                )
                label_right_boundary = (label_x + next_label_x) / 2
                if not label_left_boundary <= label_x < label_right_boundary:
                    continue

                label_cells = [
                    fragment for fragment in line
                    if label_left_boundary <= float(fragment.x) < label_right_boundary
                ]
                current_cells = [
                    fragment for fragment in line
                    if left_boundary < float(fragment.x) < right_boundary
                ]
                if not label_cells:
                    continue
                # 证据摘录只能由明确的标签列和本期列构成；上期/附注列的数值、
                # 破折号或任何文本都不能进入模型上下文。
                line_text = " ".join(
                    fragment.text for fragment in sorted(
                        [*label_cells, *current_cells], key=lambda item: item.x
                    )
                )
                evidence_text = f"本期表头：{header.text}\n数据行：{line_text}"
                digest = hashlib.sha256(
                    f"{extracted.pdf_hash}:{page.page_number}:{header.x}:{header.y}:{line_text}".encode("utf-8")
                ).hexdigest()
                cell_index = len(records) + 1
                records.append(EvidenceRecord(
                    report_id=extracted.report_id,
                    entity_scope=EntityScope.CONSOLIDATED,
                    fact_name=f"pdf_current_period_cell_{page.page_number}_{cell_index}",
                    value=None,
                    unit=None,
                    currency=None,
                    period=period,
                    source_type=SourceType.PDF_TEXT,
                    source_locator=SourceLocator(
                        provider="pdf", page=page.page_number,
                        bbox=(left_boundary, float(y), right_boundary, float(header_y)),
                        record_id=f"current-period-cell-{page.page_number}-{cell_index}",
                    ),
                    extraction_confidence=page.quality_score,
                    verification_state=VerificationState.UNKNOWN_SCOPE,
                    content_hash=digest,
                    parser_version=f"{extracted.parser_version}-current-period-cell-v1",
                    text=evidence_text,
                    raw_field_name=_CURRENT_PERIOD_CELL_FIELD,
                ))
        return records

    @staticmethod
    def _referenced_cell_evidence(
        visualizations: VisualizationBundle,
        cell_records: Sequence[EvidenceRecord],
    ) -> list[EvidenceRecord]:
        """返回卡片行真正引用的单元格证据，保持产出顺序稳定。"""
        referenced = {
            evidence_id
            for card in visualizations.cards
            for row in card.rows
            for evidence_id in row.evidence_ids
        }
        return [record for record in cell_records if record.stable_id in referenced]

    @staticmethod
    def _catalog_label(record: EvidenceRecord) -> str:
        if record.source_type in {
            SourceType.PDF_TEXT, SourceType.OCR_TEXT, SourceType.OCR_TABLE, SourceType.CHART,
        } and record.source_locator.page:
            return f"PDF · 第 {record.source_locator.page} 页"
        return record.fact_name

    @staticmethod
    def _catalog_excerpt(record: EvidenceRecord) -> str:
        return re.sub(r"\s+", " ", record.text or "").strip()[:240]

    @classmethod
    def _catalog(cls, records: Sequence[EvidenceRecord]) -> dict[str, EvidenceReference]:
        return {
            record.stable_id: EvidenceReference(
                record.entity_scope,
                record.period,
                None if record.value is None else str(record.value),
                record.unit,
                record.source_type,
                record.source_locator,
                record.verification_state,
                fact_name=record.fact_name,
                label=cls._catalog_label(record),
                excerpt=cls._catalog_excerpt(record),
            )
            for record in records
        }

    @staticmethod
    def _summary(records: Sequence[EvidenceRecord]) -> EvidenceSummary:
        states = [record.verification_state for record in records]
        return EvidenceSummary(
            total=len(records),
            verified=states.count(VerificationState.VERIFIED),
            single_source=states.count(VerificationState.SINGLE_SOURCE),
            conflicts=states.count(VerificationState.CONFLICT),
            unknown_scope=states.count(VerificationState.UNKNOWN_SCOPE),
        )

    def _new_document(
        self,
        request: AnalysisPipelineRequest,
        quick: QuickResult,
        records: Sequence[EvidenceRecord],
    ) -> AnalysisDocument:
        timestamp = _now()
        return AnalysisDocument(
            schema_version=3,
            analysis_id=request.analysis_id,
            report_id=request.report_id,
            interests=list(request.interests),
            stage="fast_ready",
            quick=quick,
            sections=[],
            observations=[],
            filtered_topics=[],
            evidence_catalog=self._catalog(records),
            evidence_summary=self._summary(records),
            errors=[],
            created_at=timestamp,
            updated_at=timestamp,
            company_code=request.company_code,
            company_name=request.company_name,
            period=request.period,
            source_file=request.pdf_path,
        )

    @staticmethod
    def visualization_topic_ids(
        candidates: Sequence[InsightCandidate],
    ) -> dict[str, str]:
        """将动态主题按披露语义映射到结构卡片，不依赖候选 ID 或完整标题。"""
        topic_ids: dict[str, str] = {}
        for candidate in candidates:
            text = "\n".join((
                candidate.title,
                candidate.summary,
                *candidate.interest_tags,
            )).casefold()
            strong_matches = {
                card_id for card_id, keywords in _TOPIC_STRONG_KEYWORDS.items()
                if any(keyword.casefold() in text for keyword in keywords)
            }
            matches = strong_matches or {
                card_id for card_id, keywords in _TOPIC_KEYWORDS.items()
                if any(keyword.casefold() in text for keyword in keywords)
            }
            if len(matches) != 1:
                continue
            card_id = next(iter(matches))
            topic_ids.setdefault(card_id, candidate.candidate_id)
        return topic_ids

    def _rank(
        self,
        candidates: Sequence[InsightCandidate],
        records: Sequence[EvidenceRecord],
        interests: Sequence[str],
    ) -> tuple[RankedInsights, dict[str, Any]]:
        evidence = {record.stable_id: record for record in records}
        scored = [
            (candidate, self.insight_scorer.score(candidate, evidence, interests))
            for candidate in candidates
        ]
        ranked = filter_and_rank(
            scored,
            detail_threshold=self.config.detail_score_threshold,
            observation_threshold=self.config.observation_score_threshold,
            limit=self.config.max_detailed_sections,
        )
        return ranked, {candidate.candidate_id: score for candidate, score in scored}

    def _enrich_ocr(
        self,
        request: AnalysisPipelineRequest,
        pages: Sequence[int],
        emit: EventEmitter,
        stop_event: Event,
    ) -> list[EvidenceRecord]:
        records: list[EvidenceRecord] = []
        for page in pages:
            if stop_event.is_set():
                break
            emit("extraction.page_started", {"phase": "ocr", "page": page})
            try:
                records.extend(self.ocr_engine.enrich(
                    request.pdf_path, request.report_id, [page]
                ))
            except Exception as exc:
                emit("extraction.page_completed", {
                    "phase": "ocr", "page": page, "status": "failed", "error": str(exc)
                })
                continue
            emit("extraction.page_completed", {"phase": "ocr", "page": page, "status": "completed"})
        return records

    def _finish_cancelled(
        self, document: AnalysisDocument, request: AnalysisPipelineRequest, emit: EventEmitter
    ) -> AnalysisDocument:
        document.stage = "cancelled"
        self._save(document, request)
        emit("job.cancelled", {"analysis": document.to_dict()})
        return document

    def run(
        self,
        request: AnalysisPipelineRequest,
        emit: EventEmitter,
        stop_event: Event,
    ) -> AnalysisDocument:
        started_at = perf_counter()
        emit("job.stage_changed", {"stage": "fast_processing"})
        structured_future = self.quick_executor.submit(
            _timed_call,
            self.structured_gateway.fetch,
            request.company_code,
            request.period,
            request.report_id,
        )
        document_future = self.quick_executor.submit(
            _timed_call, self.document_extractor.extract, request.pdf_path, request.report_id
        )
        try:
            structured, structured_fetch_ms = structured_future.result()
            extracted, pdf_extract_ms = document_future.result()
            resolved = self.resolver.resolve([
                *structured.records, *self.pdf_records(extracted, request.period)
            ])
            cell_records = self.visualization_cell_records(extracted, request.period)
            quick = self.quick_analyzer.analyze(resolved.records, request.interests)
        except Exception as exc:
            emit("job.failed", {"stage": "fast_processing", "error": str(exc)})
            raise

        document = self._new_document(request, quick, resolved.records)
        document.performance = {
            "structured_fetch_ms": structured_fetch_ms,
            "pdf_extract_ms": pdf_extract_ms,
            "quick_ready_ms": round((perf_counter() - started_at) * 1000, 3),
            "ocr_ms": 0.0,
            "section_ready_ms": [],
            "total_ms": 0.0,
            "cache_hit": bool(
                getattr(structured, "cache_hit", False)
                or getattr(extracted, "cache_hit", False)
            ),
            "interest_count": len(request.interests),
        }
        self._save(document, request)
        emit("job.stage_changed", {"stage": "fast_ready"})
        emit("quick.ready", {
            "quick": quick.to_dict(),
            "evidence_catalog": document.to_dict()["evidence_catalog"],
        })
        if stop_event.is_set():
            document.performance["total_ms"] = round((perf_counter() - started_at) * 1000, 3)
            return self._finish_cancelled(document, request, emit)

        document.stage = "deep_processing"
        self._save(document, request)
        emit("job.stage_changed", {"stage": "deep_processing"})
        ocr_pages = [
            page.page_number for page in extracted.pages
            if self.config.ocr_enabled and page.needs_ocr
        ]
        ocr_future = self.ocr_executor.submit(
            _timed_call, self._enrich_ocr, request, ocr_pages, emit, stop_event
        )

        evidence = {record.stable_id: record for record in resolved.records}
        candidates = self.insight_planner.plan(evidence, request.interests)
        ranked, scores = self._rank(candidates, resolved.records, request.interests)
        visualization_future = None
        topic_ids = self.visualization_topic_ids(ranked.details)
        if self.structure_visualizer is not None and topic_ids:
            visualization_future = self.deep_executor.submit(
                self.structure_visualizer.analyze,
                cell_records,
                request.period,
                topic_ids,
            )
        document.observations = list(ranked.observations)
        for candidate in ranked.filtered:
            score = scores[candidate.candidate_id]
            reason = "；".join(score.disqualifiers) or "证据或重要性评分不足"
            document.filtered_topics.append(FilteredTopic(candidate.candidate_id, candidate.title, reason))
            emit("theme.filtered", {"candidate_id": candidate.candidate_id, "reason": reason})

        futures = {}
        order = {candidate.candidate_id: index for index, candidate in enumerate(ranked.details)}
        for candidate in ranked.details:
            if stop_event.is_set():
                break
            emit("theme.started", {"candidate_id": candidate.candidate_id})
            futures[self.deep_executor.submit(
                self.insight_analyzer.analyze,
                candidate,
                resolved.records,
                request.interests,
            )] = candidate
        for future in as_completed(futures):
            candidate = futures[future]
            if stop_event.is_set():
                break
            try:
                section = sanitize_section(future.result())
            except Exception as exc:
                document.errors.append(AnalysisError(
                    "deep_processing", candidate.candidate_id, "theme_failed", str(exc), True
                ))
                continue
            if not section.is_detailed_eligible:
                document.observations.append(candidate)
                emit("theme.filtered", {
                    "candidate_id": candidate.candidate_id,
                    "reason": "有效披露不足 3 条",
                })
                continue
            document.sections.append(section)
            document.sections.sort(key=lambda item: order[item.section_id])
            document.performance["section_ready_ms"].append(
                round((perf_counter() - started_at) * 1000, 3)
            )
            self._save(document, request)
            section_payload = next(
                item for item in document.to_dict()["sections"]
                if item["section_id"] == section.section_id
            )
            emit("section.ready", {"section": section_payload})

        visualization_evidence: list[EvidenceRecord] = []
        if visualization_future is not None:
            try:
                visualizations = visualization_future.result()
            except Exception as exc:
                document.errors.append(AnalysisError(
                    "deep_processing", None, "visualization_failed", str(exc), True
                ))
            else:
                if any(card.status in {"complete", "partial"} for card in visualizations.cards):
                    # 只有卡片行真正引用的单元格才进入证据目录，未使用的表格行
                    # 既不参与通用分析证据，也不扩大持久化体积。
                    visualization_evidence = self._referenced_cell_evidence(
                        visualizations, cell_records
                    )
                    catalog_records = [*resolved.records, *visualization_evidence]
                    document.evidence_catalog = self._catalog(catalog_records)
                    document.evidence_summary = self._summary(catalog_records)
                    document.schema_version = 4
                    document.visualizations = visualizations
                    self._save(document, request)
                    emit("visualizations.ready", {
                        "visualizations": visualizations.to_dict(),
                    })

        ocr_records, ocr_ms = ocr_future.result()
        document.performance["ocr_ms"] = ocr_ms
        if stop_event.is_set():
            document.performance["total_ms"] = round((perf_counter() - started_at) * 1000, 3)
            return self._finish_cancelled(document, request, emit)
        if ocr_records:
            merged = self.resolver.resolve([*resolved.records, *ocr_records])
            catalog_records = [*merged.records, *visualization_evidence]
            document.evidence_catalog = self._catalog(catalog_records)
            document.evidence_summary = self._summary(catalog_records)
            correct = getattr(self.quick_analyzer, "correct", None)
            if callable(correct) and document.quick is not None:
                corrections = list(correct(document.quick, merged.records) or ())
                for correction in corrections:
                    document.quick.corrections.append(correction)
                    self._save(document, request)
                    emit("quick.corrected", {"correction": correction.to_dict()})

        document.stage = "partial" if document.errors else "completed"
        document.performance["total_ms"] = round((perf_counter() - started_at) * 1000, 3)
        self._save(document, request)
        emit(f"job.{document.stage}", {"analysis": document.to_dict()})
        return document
