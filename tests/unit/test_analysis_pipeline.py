import json
from decimal import Decimal
from threading import Event

from financial_report_fetcher.analysis_config import AnalysisConfig
from financial_report_fetcher.analysis_pipeline import (
    AnalysisPipelineRequest,
    ProgressiveAnalysisPipeline,
)
from financial_report_fetcher.analysis_result import (
    QuickConclusion,
    QuickCorrection,
    QuickResult,
    load_analysis_document,
)
from financial_report_fetcher.evidence.document import DocumentExtraction, DocumentPage
from financial_report_fetcher.evidence.models import (
    EntityScope,
    EvidenceRecord,
    SourceLocator,
    SourceType,
    VerificationState,
)
from financial_report_fetcher.evidence.resolver import EvidenceResolver
from financial_report_fetcher.evidence.structured import StructuredFetchResult
from financial_report_fetcher.insights import (
    InsightCandidate,
    InsightFinding,
    InsightPlanner,
    InsightScore,
    InsightScorer,
    InsightSection,
)


def _record(record_id="e1", *, source_type=SourceType.STRUCTURED, page=None):
    return EvidenceRecord(
        report_id="600900:2025-12-31:annual",
        entity_scope=EntityScope.CONSOLIDATED,
        fact_name="revenue",
        value=Decimal("100"),
        unit="元",
        currency="CNY",
        period="2025-12-31",
        source_type=source_type,
        source_locator=SourceLocator(provider="fake", page=page, record_id=record_id),
        extraction_confidence=0.95,
        verification_state=VerificationState.SINGLE_SOURCE,
        content_hash=record_id,
        parser_version="fake-v1",
        text="营业收入 100 元",
    )


class FakeGateway:
    def fetch(self, company_code, period, report_id):
        return StructuredFetchResult([_record()], ["fake"], {})


class FakeExtractor:
    def extract(self, pdf_path, report_id):
        return DocumentExtraction(
            report_id=report_id,
            pdf_hash="a" * 64,
            pages=(DocumentPage(1, "原生文本", 4, 0, 0, 0.8, 0.4, True),),
        )


class FakeOcr:
    def enrich(self, pdf_path, report_id, pages):
        return [_record("ocr-1", source_type=SourceType.OCR_TEXT, page=pages[0])]


class FakeQuickAnalyzer:
    def analyze(self, records, interests):
        evidence_id = records[0].stable_id
        return QuickResult(conclusions=[QuickConclusion(
            conclusion_id="q1",
            claim="收入保持增长",
            key_data="100 元",
            significance="经营稳定",
            evidence_ids=(evidence_id,),
            verification_state=records[0].verification_state,
        )])

    def correct(self, quick, records):
        return [QuickCorrection("q1", "100 元", "101 元", "OCR 补充", (records[-1].stable_id,))]


class FakeInsightAnalyzer:
    def __init__(self):
        self.fail_ids = set()

    def analyze(self, candidate, records, interests):
        if candidate.candidate_id in self.fail_ids:
            raise RuntimeError("theme failed")
        score = InsightScore(30, 25, candidate.materiality_score, candidate.clarity_score, 10)
        finding = InsightFinding("有披露内容", "对投资判断重要", candidate.evidence_ids, ("重要",))
        return InsightSection(
            candidate.candidate_id,
            candidate.title,
            candidate.summary,
            (finding, finding, finding),
            score,
            VerificationState.VERIFIED,
        )


def _pipeline(tmp_path):
    analyzer = FakeInsightAnalyzer()
    planner = InsightPlanner(lambda records, interests: [
        {
            "candidate_id": item,
            "title": item,
            "summary": "摘要",
            "interest_tags": ["现金流"],
            "evidence_ids": [next(iter(records))],
            "materiality_score": 20,
            "clarity_score": 15,
        }
        for item in ("growth-quality", "cash-risk")
    ])
    pipeline = ProgressiveAnalysisPipeline(
        structured_gateway=FakeGateway(),
        document_extractor=FakeExtractor(),
        ocr_engine=FakeOcr(),
        resolver=EvidenceResolver(),
        insight_planner=planner,
        insight_scorer=InsightScorer(),
        quick_analyzer=FakeQuickAnalyzer(),
        insight_analyzer=analyzer,
        output_dir=str(tmp_path),
        config=AnalysisConfig(detail_score_threshold=70),
    )
    return pipeline, analyzer


def _request():
    return AnalysisPipelineRequest(
        analysis_id="analysis-1",
        report_id="600900:2025-12-31:annual",
        company_code="600900",
        company_name="长江电力",
        period="2025-12-31",
        pdf_path="report.pdf",
        interests=("现金流",),
    )


def test_quick_result_is_persisted_and_emitted_before_ocr_and_deep_sections(tmp_path):
    pipeline, _ = _pipeline(tmp_path)
    events = []

    result = pipeline.run(_request(), lambda kind, data: events.append((kind, data)), Event())

    quick_index = next(i for i, event in enumerate(events) if event[0] == "quick.ready")
    ocr_index = next(i for i, event in enumerate(events) if event[0] == "extraction.page_started")
    section_index = next(i for i, event in enumerate(events) if event[0] == "section.ready")
    assert quick_index < ocr_index
    assert quick_index < section_index
    assert events[quick_index][1]["evidence_catalog"]
    assert result.quick.conclusions
    saved = load_analysis_document(tmp_path / "analysis-1.json")
    assert saved.stage == "completed"
    assert json.loads((tmp_path / "analysis-1.json").read_text())["schema_version"] == 3


def test_one_theme_failure_yields_partial_and_keeps_ready_section(tmp_path):
    pipeline, analyzer = _pipeline(tmp_path)
    analyzer.fail_ids.add("cash-risk")
    events = []

    result = pipeline.run(_request(), lambda kind, data: events.append((kind, data)), Event())

    assert result.stage == "partial"
    assert [section.section_id for section in result.sections] == ["growth-quality"]
    assert any(error.item_id == "cash-risk" for error in result.errors)
    assert events[-1][0] == "job.partial"


def test_cancel_preserves_quick_result_and_emits_cancelled(tmp_path):
    pipeline, _ = _pipeline(tmp_path)
    stop = Event()
    events = []

    def emit(kind, payload):
        events.append((kind, payload))
        if kind == "quick.ready":
            stop.set()

    result = pipeline.run(_request(), emit, stop)

    assert result.stage == "cancelled"
    assert result.quick is not None
    assert events[-1][0] == "job.cancelled"


def test_legacy_analysis_document_loads_without_rewriting(tmp_path):
    path = tmp_path / "legacy.json"
    raw = {"meta": {"company": "旧报告"}, "dimensions": [{"id": "risk"}]}
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    loaded = load_analysis_document(path)

    assert loaded.legacy is True
    assert loaded.to_dict() == raw
    assert json.loads(path.read_text(encoding="utf-8")) == raw
