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
from financial_report_fetcher.visualizations import (
    VisualizationBundle,
    VisualizationCard,
    VisualizationRow,
)
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
    def __init__(self):
        self.calls = []

    def enrich(self, pdf_path, report_id, pages):
        self.calls.append(tuple(pages))
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


def test_pdf_records_assign_entity_scope_from_statement_page_title():
    """PDF 正文页归属报告主体；母公司报表页识别为母公司口径。"""
    extraction = DocumentExtraction(
        report_id="600900:2025-12-31:annual",
        pdf_hash="b" * 64,
        pages=(
            DocumentPage(1, "合并资产负债表\n单位：元", 12, 0, 0.5, 0.5, 0.9, False),
            DocumentPage(2, "母公司资产负债表\n单位：元", 12, 0, 0.5, 0.5, 0.9, False),
            DocumentPage(3, "第三节 管理层讨论与分析\n公司营业收入增长。", 30, 0, 0.4, 0.4, 0.9, False),
        ),
    )

    records = ProgressiveAnalysisPipeline.pdf_records(extraction, "2025-12-31")

    assert [record.entity_scope for record in records] == [
        EntityScope.CONSOLIDATED,
        EntityScope.PARENT,
        EntityScope.CONSOLIDATED,
    ]


def test_pdf_records_resolve_to_single_source_when_scope_is_known():
    """主体已知的 PDF 页不再被降级为 unknown，可参与单源引用。"""
    extraction = DocumentExtraction(
        report_id="600900:2025-12-31:annual",
        pdf_hash="c" * 64,
        pages=(
            DocumentPage(1, "合并利润表\n营业收入增长。", 14, 0, 0.5, 0.5, 0.9, False),
        ),
    )

    records = ProgressiveAnalysisPipeline.pdf_records(extraction, "2025-12-31")
    resolved = EvidenceResolver().resolve(records)

    assert resolved.records
    assert all(
        record.verification_state is VerificationState.SINGLE_SOURCE
        for record in resolved.records
    )
    assert VerificationState.UNKNOWN_SCOPE not in {
        record.verification_state for record in resolved.records
    }


def test_document_catalog_persists_stable_evidence_display_metadata(tmp_path):
    """前端统一证据区必须从真实 document payload 得到可读标签和摘录。"""
    pipeline, _ = _pipeline(tmp_path)
    record = _record("pdf-12", source_type=SourceType.PDF_TEXT, page=12)

    document = pipeline._new_document(_request(), QuickResult(), [record]).to_dict()
    reference = document["evidence_catalog"][record.stable_id]

    assert reference["fact_name"] == "revenue"
    assert reference["label"] == "PDF · 第 12 页"
    assert reference["excerpt"] == "营业收入 100 元"


class FakeStructureVisualizer:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def analyze(self, records, period, topic_ids):
        self.calls.append((records, period, topic_ids))
        if self.fail:
            raise RuntimeError("visualization failed")
        topic_id = topic_ids["cash_flow_structure"]
        return VisualizationBundle(version=1, cards=(VisualizationCard(
            id="cash_flow_structure",
            topic_id=topic_id,
            title="现金流结构",
            kind="cash_flow",
            status="partial",
            unavailable_reason=None,
            rows=(
                VisualizationRow(
                    "operating_cash_flow", "经营活动现金流净额", 12.0, "亿元", "inflow", ("pdf",),
                ),
                VisualizationRow(
                    "investing_cash_flow", "投资活动现金流净额", -3.0, "亿元", "outflow", ("pdf",),
                ),
            ),
        ),))


def _pipeline(tmp_path, structure_visualizer=None):
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
        structure_visualizer=structure_visualizer,
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


def test_pipeline_persists_visualizations_and_emits_ready_event(tmp_path):
    visualizer = FakeStructureVisualizer()
    pipeline, _ = _pipeline(tmp_path, structure_visualizer=visualizer)
    events = []

    result = pipeline.run(_request(), lambda kind, data: events.append((kind, data)), Event())

    assert result.schema_version == 4
    assert result.visualizations.cards[0].id == "cash_flow_structure"
    assert any(name == "visualizations.ready" for name, _payload in events)
    assert visualizer.calls[0][2]["cash_flow_structure"] in {
        section.section_id for section in result.sections
    }
    assert load_analysis_document(tmp_path / "analysis-1.json").schema_version == 4


def test_visualization_failure_does_not_interrupt_analysis(tmp_path):
    pipeline, _ = _pipeline(tmp_path, structure_visualizer=FakeStructureVisualizer(fail=True))
    events = []

    result = pipeline.run(_request(), lambda kind, data: events.append((kind, data)), Event())

    assert result.stage == "partial"
    assert result.visualizations is None
    assert any(error.code == "visualization_failed" for error in result.errors)
    assert not any(name == "visualizations.ready" for name, _payload in events)
    assert result.sections


def test_visualization_topic_mapping_uses_dynamic_first_matching_detail(tmp_path):
    pipeline, _ = _pipeline(tmp_path)
    candidates = [
        InsightCandidate("dynamic-first", "现金流质量", "经营活动现金流改善", (), (), 0, 0),
        InsightCandidate("dynamic-second", "现金流风险", "筹资活动减少", (), (), 0, 0),
        InsightCandidate("dynamic-profit", "盈利能力", "营业收入提升", (), (), 0, 0),
        InsightCandidate("unmatched", "治理", "公司治理", (), (), 0, 0),
    ]

    assert pipeline.visualization_topic_ids(candidates) == {
        "cash_flow_structure": "dynamic-first",
        "profit_structure": "dynamic-profit",
    }


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
    assert result.performance["interest_count"] == 1
    assert result.performance["quick_ready_ms"] >= 0
    assert result.performance["total_ms"] >= result.performance["quick_ready_ms"]
    assert len(result.performance["section_ready_ms"]) == 2


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


def test_ocr_can_be_disabled_without_delaying_quick_or_deep_results(tmp_path):
    pipeline, _ = _pipeline(tmp_path)
    pipeline.config = AnalysisConfig(detail_score_threshold=70, ocr_enabled=False)
    events = []

    result = pipeline.run(_request(), lambda kind, data: events.append((kind, data)), Event())

    assert pipeline.ocr_engine.calls == []
    assert not any(kind == "extraction.page_started" for kind, _ in events)
    assert result.stage == "completed"


def test_legacy_analysis_document_loads_without_rewriting(tmp_path):
    path = tmp_path / "legacy.json"
    raw = {"meta": {"company": "旧报告"}, "dimensions": [{"id": "risk"}]}
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    loaded = load_analysis_document(path)

    assert loaded.legacy is True
    assert loaded.to_dict() == raw
    assert json.loads(path.read_text(encoding="utf-8")) == raw
