"""快速结论先行、主题逐步可见的证据化财报分析管线。"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event
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
from .insights import (
    InsightCandidate,
    InsightScorer,
    RankedInsights,
    filter_and_rank,
    sanitize_section,
)


EventEmitter = Callable[[str, dict[str, Any]], None]


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
    ):
        self.structured_gateway = structured_gateway
        self.document_extractor = document_extractor
        self.ocr_engine = ocr_engine
        self.resolver = resolver
        self.insight_planner = insight_planner
        self.insight_scorer = insight_scorer
        self.quick_analyzer = quick_analyzer
        self.insight_analyzer = insight_analyzer
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
        safe_id = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in request.analysis_id
        ).strip("_")
        if not safe_id:
            raise ValueError("analysis_id 不能生成空文件名")
        return self.output_dir / f"{safe_id}.json", self.output_dir / f"{safe_id}.md"

    def _save(self, document: AnalysisDocument, request: AnalysisPipelineRequest) -> None:
        document.updated_at = _now()
        save_analysis_document(document, *self._paths(request))

    @staticmethod
    def pdf_records(extracted, period: str) -> list[EvidenceRecord]:
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
                entity_scope=EntityScope.UNKNOWN,
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

    @staticmethod
    def _catalog(records: Sequence[EvidenceRecord]) -> dict[str, EvidenceReference]:
        return {
            record.stable_id: EvidenceReference(
                record.entity_scope,
                record.period,
                None if record.value is None else str(record.value),
                record.unit,
                record.source_type,
                record.source_locator,
                record.verification_state,
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
        emit("job.stage_changed", {"stage": "fast_processing"})
        structured_future = self.quick_executor.submit(
            self.structured_gateway.fetch,
            request.company_code,
            request.period,
            request.report_id,
        )
        document_future = self.quick_executor.submit(
            self.document_extractor.extract, request.pdf_path, request.report_id
        )
        try:
            structured = structured_future.result()
            extracted = document_future.result()
            resolved = self.resolver.resolve([
                *structured.records, *self.pdf_records(extracted, request.period)
            ])
            quick = self.quick_analyzer.analyze(resolved.records, request.interests)
        except Exception as exc:
            emit("job.failed", {"stage": "fast_processing", "error": str(exc)})
            raise

        document = self._new_document(request, quick, resolved.records)
        self._save(document, request)
        emit("job.stage_changed", {"stage": "fast_ready"})
        emit("quick.ready", {
            "quick": quick.to_dict(),
            "evidence_catalog": document.to_dict()["evidence_catalog"],
        })
        if stop_event.is_set():
            return self._finish_cancelled(document, request, emit)

        document.stage = "deep_processing"
        self._save(document, request)
        emit("job.stage_changed", {"stage": "deep_processing"})
        ocr_pages = [page.page_number for page in extracted.pages if page.needs_ocr]
        ocr_future = self.ocr_executor.submit(
            self._enrich_ocr, request, ocr_pages, emit, stop_event
        )

        evidence = {record.stable_id: record for record in resolved.records}
        candidates = self.insight_planner.plan(evidence, request.interests)
        ranked, scores = self._rank(candidates, resolved.records, request.interests)
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
            self._save(document, request)
            section_payload = next(
                item for item in document.to_dict()["sections"]
                if item["section_id"] == section.section_id
            )
            emit("section.ready", {"section": section_payload})

        ocr_records = ocr_future.result()
        if stop_event.is_set():
            return self._finish_cancelled(document, request, emit)
        if ocr_records:
            merged = self.resolver.resolve([*resolved.records, *ocr_records])
            document.evidence_catalog = self._catalog(merged.records)
            document.evidence_summary = self._summary(merged.records)
            correct = getattr(self.quick_analyzer, "correct", None)
            if callable(correct) and document.quick is not None:
                corrections = list(correct(document.quick, merged.records) or ())
                for correction in corrections:
                    document.quick.corrections.append(correction)
                    self._save(document, request)
                    emit("quick.corrected", {"correction": correction.to_dict()})

        document.stage = "partial" if document.errors else "completed"
        self._save(document, request)
        emit(f"job.{document.stage}", {"analysis": document.to_dict()})
        return document
