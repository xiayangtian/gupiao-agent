"""PDF 逐页提取与定向 OCR 触发评分测试。"""

import hashlib

import pytest

from financial_report_fetcher.evidence.document import DocumentExtractor, score_page


@pytest.mark.parametrize(
    ("text", "visual_ratio", "expected"),
    [
        ("\ufffd" * 80, 0.1, True),
        ("年度报告" * 100, 0.05, False),
        ("12 34 56", 0.85, True),
    ],
)
def test_page_quality_controls_targeted_ocr(text, visual_ratio, expected):
    """低文字量、乱码或高视觉占比页面才进入 OCR 候选。"""
    page = score_page(1, text, visual_area_ratio=visual_ratio, min_chars=120)

    assert page.needs_ocr is expected
    assert 0.0 <= page.quality_score <= 1.0


def test_page_quality_does_not_count_normal_line_breaks_as_garbled_text():
    """换行和制表符是正常布局字符，不能被当成乱码。"""
    page = score_page(
        2,
        ("营业收入\t100\n净利润\t20\n" * 30),
        visual_area_ratio=0.05,
        min_chars=120,
    )

    assert page.abnormal_char_ratio == 0.0
    assert page.needs_ocr is False
    assert page.table_alignment_score > 0.0


class FakePage:
    def __init__(self, text, image_count=0):
        self._text = text
        self.images = [object()] * image_count

    def extract_text(self):
        return self._text


class FakeReader:
    def __init__(self, pages):
        self.pages = pages


def test_document_extractor_keeps_full_pages_numbers_and_pdf_hash(tmp_path):
    """逐页结果不得沿用旧分析器的 15000 字符截断。"""
    pdf = tmp_path / "report.pdf"
    raw_pdf = b"fake-pdf-content"
    pdf.write_bytes(raw_pdf)
    long_text = "经营情况" * 5000
    reader = FakeReader([FakePage(long_text), FakePage(None, image_count=1)])
    extractor = DocumentExtractor(reader_factory=lambda _path: reader, min_chars=120)

    result = extractor.extract(str(pdf), "600900:2025-12-31:annual")

    assert result.report_id == "600900:2025-12-31:annual"
    assert result.pdf_hash == hashlib.sha256(raw_pdf).hexdigest()
    assert [page.page_number for page in result.pages] == [1, 2]
    assert result.pages[0].text == long_text
    assert len(result.pages[0].text) > 15000
    assert result.pages[1].text == ""
    assert result.pages[1].needs_ocr is True


def test_document_extractor_uses_injected_visual_estimator(tmp_path):
    """布局层可替换估算器必须真正影响 OCR 判定。"""
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"pdf")
    page = FakePage("年度报告" * 100)
    extractor = DocumentExtractor(
        reader_factory=lambda _path: FakeReader([page]),
        visual_ratio_estimator=lambda _page: 0.9,
        min_chars=120,
    )

    result = extractor.extract(str(pdf), "r1")

    assert result.pages[0].visual_area_ratio == 0.9
    assert result.pages[0].needs_ocr is True
