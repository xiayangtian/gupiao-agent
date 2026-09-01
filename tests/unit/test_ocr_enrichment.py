"""可选定向 OCR 的离线契约测试。"""

from decimal import Decimal

import pytest

from financial_report_fetcher.evidence.models import (
    EntityScope,
    SourceType,
    VerificationState,
)
from financial_report_fetcher.evidence.ocr import (
    OcrBlock,
    OcrUnavailable,
    PaddleStructureBackend,
    PaddleStructureEngine,
    normalize_paddle_result,
)


class FakeBackend:
    def __init__(self, results_by_page):
        self.results_by_page = results_by_page
        self.page_calls = []

    def extract_page(self, pdf_path, page_number):
        self.page_calls.append(page_number)
        return list(self.results_by_page.get(page_number, []))


def test_ocr_enricher_calls_backend_only_for_requested_pages():
    """定向 OCR 只能处理显式页码，且重复页不能重复推理。"""
    backend = FakeBackend({
        3: [OcrBlock(3, "table_cell", "1,234", 0.96, (1, 2, 30, 40), "cell-1")],
        8: [OcrBlock(8, "chart", "收入同比增长 12%", 0.88, (4, 5, 60, 70), "chart-1")],
    })
    engine = PaddleStructureEngine(backend=backend, min_confidence=0.80)

    records = engine.enrich("report.pdf", "r1", pages=[3, 8, 3])

    assert backend.page_calls == [3, 8]
    assert {record.source_locator.page for record in records} == {3, 8}
    assert next(record for record in records if record.source_locator.page == 3).value == Decimal("1234")
    assert next(record for record in records if record.source_locator.page == 8).source_type is SourceType.CHART


def test_low_confidence_numeric_cell_is_retained_as_text_not_fact():
    """低置信数字只能作为待核验文本，不能进入可计算数值事实。"""
    backend = FakeBackend({
        3: [OcrBlock(3, "table_cell", "1,234", 0.42, (1, 2, 30, 40), "cell-1")],
    })

    records = PaddleStructureEngine(backend=backend, min_confidence=0.80).enrich(
        "report.pdf", "r1", [3]
    )

    assert len(records) == 1
    assert records[0].value is None
    assert records[0].text == "1,234"
    assert records[0].entity_scope is EntityScope.UNKNOWN
    assert records[0].verification_state is VerificationState.UNKNOWN_SCOPE


def test_official_paddle_result_shape_yields_table_cells_chart_and_text():
    """官方 result.json 的表格、图表和正文结构都应被保留。"""
    payload = {
        "parsing_res_list": [
            {"block_id": 1, "block_label": "text", "block_content": "经营稳健", "block_bbox": [1, 2, 20, 30]},
            {"block_id": 2, "block_label": "table", "block_content": "table", "block_bbox": [1, 40, 80, 90]},
            {"block_id": 3, "block_label": "chart", "block_content": "销量上升", "block_bbox": [5, 100, 90, 180]},
        ],
        "overall_ocr_res": {"rec_scores": [0.91, 0.87]},
        "table_res_list": [{
            "table_ocr_pred": {
                "rec_texts": ["营业收入", "100"],
                "rec_scores": [0.98, 0.96],
                "rec_boxes": [[1, 40, 20, 50], [30, 40, 50, 50]],
            }
        }],
    }

    blocks = normalize_paddle_result(payload, page_number=5)

    assert [(block.label, block.text) for block in blocks] == [
        ("table_cell", "营业收入"),
        ("table_cell", "100"),
        ("text", "经营稳健"),
        ("chart", "销量上升"),
    ]
    assert blocks[1].confidence == 0.96
    assert blocks[2].confidence == pytest.approx(0.89)


def test_default_backend_is_lazy_and_explains_optional_install(monkeypatch):
    """未安装重依赖不能影响导入，只有真正调用 OCR 时才给出安装说明。"""
    import financial_report_fetcher.evidence.ocr as ocr_module

    def missing_paddle(name):
        assert name == "paddleocr"
        raise ModuleNotFoundError("paddleocr")

    monkeypatch.setattr(ocr_module.importlib, "import_module", missing_paddle)
    engine = PaddleStructureEngine(backend=PaddleStructureBackend())

    with pytest.raises(OcrUnavailable, match="requirements-ocr.txt"):
        engine.enrich("report.pdf", "r1", [1])


@pytest.mark.parametrize("pages", [[0], [-1]])
def test_ocr_rejects_non_positive_page_numbers(pages):
    """页码必须使用从 1 开始的 PDF 语义。"""
    with pytest.raises(ValueError, match="页码"):
        PaddleStructureEngine(backend=FakeBackend({})).enrich("report.pdf", "r1", pages)
