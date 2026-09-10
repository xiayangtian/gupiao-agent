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
from financial_report_fetcher.evidence.document import (
    DocumentExtraction,
    DocumentPage,
    DocumentTextFragment,
)
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
    validate_visualization_payload,
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


class CellExtractor:
    """返回带定位表头的报表页，使管线只能从本期列产生单元格证据。"""

    def extract(self, pdf_path, report_id):
        return DocumentExtraction(
            report_id=report_id,
            pdf_hash="b" * 64,
            pages=(DocumentPage(
                1, "合并现金流量表", 9, 0, 0.5, 0.5, 0.9, False,
                fragments=(
                    DocumentTextFragment("项目", 80, 700),
                    DocumentTextFragment("2025年12月31日 本期", 300, 700),
                    DocumentTextFragment("上期", 330, 700),
                    DocumentTextFragment("经营活动现金流净额", 80, 680),
                    DocumentTextFragment("100", 300, 680),
                    DocumentTextFragment("90", 330, 680),
                    DocumentTextFragment("投资活动现金流净额", 80, 660),
                    DocumentTextFragment("50", 300, 660),
                    DocumentTextFragment("40", 330, 660),
                ),
            ),),
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


def _positioned_current_period_extraction(pdf_hash: str) -> DocumentExtraction:
    return DocumentExtraction(
        report_id="600900:2025-12-31:annual",
        pdf_hash=pdf_hash,
        pages=(DocumentPage(
            1, "合并现金流量表", 7, 0, 0.5, 0.5, 0.9, False,
            fragments=(
                DocumentTextFragment("项目", 80, 700),
                DocumentTextFragment("2025年12月31日 本期", 300, 700),
                DocumentTextFragment("2024年12月31日 上期", 330, 700),
                DocumentTextFragment("经营活动现金流净额", 80, 680),
                DocumentTextFragment("100", 300, 680),
                DocumentTextFragment("90", 330, 680),
                DocumentTextFragment("—", 330, 680),
            ),
        ),),
    )


def test_visualization_cell_evidence_comes_only_from_the_positioned_current_period_column():
    extraction = _positioned_current_period_extraction("d" * 64)

    cells = ProgressiveAnalysisPipeline.visualization_cell_records(extraction, "2025-12-31")

    assert len(cells) == 1
    assert "100" in cells[0].text
    assert "90" not in cells[0].text  # 上期列绝不进入本期单元格证据上下文。
    assert "—" not in cells[0].text
    assert "经营活动现金流净额" in cells[0].text
    assert cells[0].source_locator.bbox is not None


def test_page_level_pdf_evidence_cannot_back_structure_visualization_rows():
    """整页文本没有本期列坐标，绝不能作为结构图数据行的证据。"""
    extraction = _positioned_current_period_extraction("d" * 64)
    pages = ProgressiveAnalysisPipeline.pdf_records(extraction, "2025-12-31")

    assert [record.fact_name for record in pages] == ["pdf_page_1"]
    assert not [
        record for record in pages if record.raw_field_name == "current_period_pdf_cell"
    ]
    rejected = validate_visualization_payload(
        {"cards": [{
            "id": "cash_flow_structure", "topic_id": "cash", "title": "现金流结构",
            "kind": "cash_flow", "rows": [
                {"metric_id": "operating_cash_flow", "label": "经营活动现金流净额",
                 "value": 1, "unit": "亿元", "direction": "inflow",
                 "evidence_ids": [pages[0].stable_id]},
                {"metric_id": "investing_cash_flow", "label": "投资活动现金流净额",
                 "value": -1, "unit": "亿元", "direction": "outflow",
                 "evidence_ids": [pages[0].stable_id]},
            ],
        }]},
        period="2025-12-31", allowed_pdf_evidence={record.stable_id: record for record in pages},
    )
    assert rejected.cards[0].status == "unavailable"


def test_current_period_cells_never_cross_into_the_next_statement_table():
    """同页相邻报表的数据行只能归最近的上方表头，且表头行本身不是数据行。"""
    extraction = DocumentExtraction(
        report_id="600900:2025-12-31:annual",
        pdf_hash="7" * 64,
        pages=(DocumentPage(
            1, "合并现金流量表", 7, 0, 0.5, 0.5, 0.9, False,
            fragments=(
                DocumentTextFragment("项目", 80, 700),
                DocumentTextFragment("2025年12月31日 本期", 300, 700),
                DocumentTextFragment("上期", 330, 700),
                DocumentTextFragment("经营活动现金流净额", 80, 680),
                DocumentTextFragment("100", 300, 680),
                DocumentTextFragment("90", 330, 680),
                DocumentTextFragment("项目", 80, 600),
                DocumentTextFragment("2025年12月31日 本期", 300, 600),
                DocumentTextFragment("上期", 330, 600),
                DocumentTextFragment("投资活动现金流净额", 80, 580),
                DocumentTextFragment("555", 300, 580),
                DocumentTextFragment("444", 330, 580),
            ),
        ),),
    )

    cells = ProgressiveAnalysisPipeline.visualization_cell_records(extraction, "2025-12-31")

    assert len(cells) == 2
    operation = [cell for cell in cells if "经营活动现金流净额" in cell.text]
    investment = [cell for cell in cells if "投资活动现金流净额" in cell.text]
    assert len(operation) == 1 and len(investment) == 1
    assert "555" not in operation[0].text
    assert "100" in operation[0].text
    assert investment[0].source_locator.bbox[3] == 600


def test_row_label_column_left_edge_uses_the_known_table_column():
    """行名列左边界来自表格已知最左列，不能反射列宽把表格左侧标记并入行名。"""
    extraction = DocumentExtraction(
        report_id="600900:2025-12-31:annual",
        pdf_hash="8" * 64,
        pages=(DocumentPage(
            1, "合并利润表", 6, 0, 0.5, 0.5, 0.9, False,
            fragments=(
                DocumentTextFragment("项目", 80, 700),
                DocumentTextFragment("本期", 300, 700),
                DocumentTextFragment("上期", 330, 700),
                DocumentTextFragment("*", 20, 680),
                DocumentTextFragment("营业收入", 80, 680),
                DocumentTextFragment("100", 300, 680),
                DocumentTextFragment("90", 330, 680),
            ),
        ),),
    )

    cells = ProgressiveAnalysisPipeline.visualization_cell_records(extraction, "2025-12-31")

    assert len(cells) == 1
    assert "营业收入" in cells[0].text
    assert "100" in cells[0].text
    assert "*" not in cells[0].text


def test_current_period_header_matches_unpadded_chinese_dates():
    """表头写成“2025年6月30日”时也必须归一到报告期，不能静默丢失覆盖。"""
    extraction = DocumentExtraction(
        report_id="600900:2025-06-30:semi_annual",
        pdf_hash="9" * 64,
        pages=(DocumentPage(
            1, "合并利润表", 6, 0, 0.5, 0.5, 0.9, False,
            fragments=(
                DocumentTextFragment("项目", 80, 700),
                DocumentTextFragment("2025年6月30日", 300, 700),
                DocumentTextFragment("2024年6月30日", 330, 700),
                DocumentTextFragment("营业收入", 80, 680),
                DocumentTextFragment("100", 300, 680),
                DocumentTextFragment("90", 330, 680),
            ),
        ),),
    )

    cells = ProgressiveAnalysisPipeline.visualization_cell_records(extraction, "2025-06-30")

    assert len(cells) == 1
    assert cells[0].period == "2025-06-30"
    assert "100" in cells[0].text
    assert "90" not in cells[0].text


def test_current_period_evidence_excludes_prior_numeric_column_left_of_current_column():
    """行名必须来自表头识别的标签列，不能把左侧上期数值传给模型。"""
    extraction = DocumentExtraction(
        report_id="600900:2025-12-31:annual",
        pdf_hash="f" * 64,
        pages=(DocumentPage(
            1, "合并利润表", 6, 0, 0.5, 0.5, 0.9, False,
            fragments=(
                DocumentTextFragment("项目", 80, 700),
                DocumentTextFragment("上期", 220, 700),
                DocumentTextFragment("2025年12月31日 本期", 300, 700),
                DocumentTextFragment("附注", 380, 700),
                DocumentTextFragment("营业收入", 80, 680),
                DocumentTextFragment("90", 220, 680),
                DocumentTextFragment("100", 300, 680),
                DocumentTextFragment("—", 380, 680),
            ),
        ),),
    )

    cells = ProgressiveAnalysisPipeline.visualization_cell_records(extraction, "2025-12-31")

    assert len(cells) == 1
    assert "营业收入" in cells[0].text
    assert "100" in cells[0].text
    assert "90" not in cells[0].text
    assert "—" not in cells[0].text


def test_current_period_evidence_is_not_created_without_recognized_row_label_geometry():
    extraction = DocumentExtraction(
        report_id="600900:2025-12-31:annual",
        pdf_hash="1" * 64,
        pages=(DocumentPage(
            1, "合并利润表", 6, 0, 0.5, 0.5, 0.9, False,
            fragments=(
                DocumentTextFragment("序号", 80, 700),
                DocumentTextFragment("上期", 220, 700),
                DocumentTextFragment("2025年12月31日 本期", 300, 700),
                DocumentTextFragment("附注", 380, 700),
                DocumentTextFragment("营业收入", 80, 680),
                DocumentTextFragment("90", 220, 680),
                DocumentTextFragment("100", 300, 680),
            ),
        ),),
    )

    assert not ProgressiveAnalysisPipeline.visualization_cell_records(extraction, "2025-12-31")


def test_current_period_evidence_rejects_row_number_as_financial_label():
    """“行次”只标识行号，不能替代已验证的财务项目名称列。"""
    extraction = DocumentExtraction(
        report_id="600900:2025-12-31:annual",
        pdf_hash="2" * 64,
        pages=(DocumentPage(
            1, "合并利润表", 6, 0, 0.5, 0.5, 0.9, False,
            fragments=(
                DocumentTextFragment("行次", 80, 700),
                DocumentTextFragment("上期", 220, 700),
                DocumentTextFragment("2025年12月31日 本期", 300, 700),
                DocumentTextFragment("附注", 380, 700),
                DocumentTextFragment("1", 80, 680),
                DocumentTextFragment("90", 220, 680),
                DocumentTextFragment("100", 300, 680),
                DocumentTextFragment("—", 380, 680),
            ),
        ),),
    )

    assert not ProgressiveAnalysisPipeline.visualization_cell_records(extraction, "2025-12-31")


def test_pdf_records_without_positioned_current_period_header_do_not_create_visualization_evidence():
    extraction = DocumentExtraction(
        report_id="600900:2025-12-31:annual", pdf_hash="e" * 64,
        pages=(DocumentPage(1, "合并现金流量表\\n本期 100 上期 90", 20, 0, 0.5, 0.5, 0.9, False),),
    )
    assert not ProgressiveAnalysisPipeline.visualization_cell_records(extraction, "2025-12-31")


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
    def __init__(self, *, fail=False, result=None):
        self.fail = fail
        self.result = result
        self.calls = []

    def analyze(self, records, period, topic_ids):
        self.calls.append((records, period, topic_ids))
        if self.fail:
            raise RuntimeError("visualization failed")
        if self.result is not None:
            return self.result
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


class RecordingQuickAnalyzer(FakeQuickAnalyzer):
    def __init__(self):
        self.record_sets = []

    def analyze(self, records, interests):
        self.record_sets.append(list(records))
        return super().analyze(records, interests)


class RecordingInsightAnalyzer(FakeInsightAnalyzer):
    def __init__(self):
        super().__init__()
        self.record_sets = []

    def analyze(self, candidate, records, interests):
        self.record_sets.append(list(records))
        return super().analyze(candidate, records, interests)


class ReferencingStructureVisualizer:
    """只引用传入单元格证据的结构图实现，用于验证目录写入范围。"""

    def __init__(self, row_evidence_index=0):
        self.row_evidence_index = row_evidence_index
        self.calls = []

    def analyze(self, records, period, topic_ids):
        self.calls.append(list(records))
        evidence_id = records[self.row_evidence_index].stable_id
        return VisualizationBundle(version=1, cards=(VisualizationCard(
            id="cash_flow_structure",
            topic_id=topic_ids["cash_flow_structure"],
            title="现金流结构",
            kind="cash_flow",
            status="partial",
            unavailable_reason=None,
            rows=(
                VisualizationRow(
                    "operating_cash_flow", "经营活动现金流净额", 12.0, "亿元", "inflow",
                    (evidence_id,),
                ),
                VisualizationRow(
                    "investing_cash_flow", "投资活动现金流净额", -3.0, "亿元", "outflow",
                    (evidence_id,),
                ),
            ),
        ),))


def _pipeline(
    tmp_path,
    structure_visualizer=None,
    extractor=None,
    quick_analyzer=None,
    insight_analyzer=None,
):
    analyzer = insight_analyzer or FakeInsightAnalyzer()
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
        document_extractor=extractor or FakeExtractor(),
        ocr_engine=FakeOcr(),
        resolver=EvidenceResolver(),
        insight_planner=planner,
        insight_scorer=InsightScorer(),
        quick_analyzer=quick_analyzer or FakeQuickAnalyzer(),
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


def test_pipeline_does_not_upgrade_or_emit_for_unavailable_visualizations(tmp_path):
    unavailable = VisualizationBundle(version=1, cards=(VisualizationCard(
        id="cash_flow_structure", topic_id="cash-risk", title="现金流结构",
        kind="cash_flow", status="unavailable", unavailable_reason="披露不足", rows=(),
    ),))
    pipeline, _ = _pipeline(tmp_path, structure_visualizer=FakeStructureVisualizer(result=unavailable))
    events = []

    result = pipeline.run(_request(), lambda kind, data: events.append((kind, data)), Event())

    assert result.schema_version == 3
    assert result.visualizations is None
    assert not any(name == "visualizations.ready" for name, _payload in events)
    assert load_analysis_document(tmp_path / "analysis-1.json").schema_version == 3


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


def test_current_period_cells_are_sampled_only_by_the_structure_visualizer(tmp_path):
    """单元格证据只服务结构图：快速结论与主题 AI 的证据采样不得引入表格行。"""
    quick = RecordingQuickAnalyzer()
    analyzer = RecordingInsightAnalyzer()
    visualizer = ReferencingStructureVisualizer()
    pipeline, _ = _pipeline(
        tmp_path,
        structure_visualizer=visualizer,
        extractor=CellExtractor(),
        quick_analyzer=quick,
        insight_analyzer=analyzer,
    )

    pipeline.run(_request(), lambda kind, data: None, Event())

    assert quick.record_sets and analyzer.record_sets
    for records in [*quick.record_sets, *analyzer.record_sets]:
        assert not [
            record for record in records
            if record.raw_field_name == "current_period_pdf_cell"
        ]
    assert [
        record.raw_field_name for record in visualizer.calls[0]
    ] == ["current_period_pdf_cell", "current_period_pdf_cell"]


def test_evidence_catalog_only_gains_cells_referenced_by_validated_rows(tmp_path):
    """未被卡片行引用的单元格不得进入目录，避免无用的表格证据膨胀。"""
    visualizer = ReferencingStructureVisualizer(row_evidence_index=0)
    pipeline, _ = _pipeline(
        tmp_path, structure_visualizer=visualizer, extractor=CellExtractor()
    )

    result = pipeline.run(_request(), lambda kind, data: None, Event())

    cells = visualizer.calls[0]
    assert len(cells) == 2
    referenced, unreferenced = cells[0], cells[1]
    catalog = result.to_dict()["evidence_catalog"]
    cell_entries = [
        evidence_id for evidence_id, reference in catalog.items()
        if reference["fact_name"].startswith("pdf_current_period_cell")
    ]
    assert cell_entries == [referenced.stable_id]
    assert catalog[referenced.stable_id]["source_type"] == "pdf_text"
    assert catalog[referenced.stable_id]["source_locator"]["page"] == 1
    assert unreferenced.stable_id not in catalog


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
