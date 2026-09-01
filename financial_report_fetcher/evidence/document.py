"""PDF 逐页原生文本提取与定向 OCR 质量判定。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class DocumentPage:
    page_number: int
    text: str
    char_count: int
    abnormal_char_ratio: float
    table_alignment_score: float
    visual_area_ratio: float
    quality_score: float
    needs_ocr: bool

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number 必须从 1 开始")
        for name in (
            "abnormal_char_ratio",
            "table_alignment_score",
            "visual_area_ratio",
            "quality_score",
        ):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} 必须在 0 到 1 之间")


@dataclass(frozen=True)
class DocumentExtraction:
    report_id: str
    pdf_hash: str
    pages: tuple[DocumentPage, ...]
    parser_version: str = "pypdf-page-v1"

    def __post_init__(self) -> None:
        if not self.report_id.strip():
            raise ValueError("report_id 不能为空")
        if len(self.pdf_hash) != 64:
            raise ValueError("pdf_hash 必须是 SHA-256 十六进制摘要")


def _table_alignment_score(text: str) -> float:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return 0.0
    aligned = 0
    for line in lines:
        cells = [cell for cell in re.split(r"\t+|\s{2,}|\|", line) if cell.strip()]
        if len(cells) >= 2 and any(character.isdigit() for character in line):
            aligned += 1
    return min(1.0, aligned / len(lines))


def score_page(
    page_number: int,
    text: str,
    visual_area_ratio: float,
    min_chars: int,
) -> DocumentPage:
    """根据文字密度、异常字符和视觉占比生成确定性 OCR 判定。"""
    if min_chars <= 0:
        raise ValueError("min_chars 必须大于 0")
    normalized_text = text or ""
    char_count = len(normalized_text.strip())
    abnormal_count = sum(
        character == "\ufffd"
        or (unicodedata.category(character) == "Cc" and character not in "\n\r\t")
        for character in normalized_text
    )
    abnormal_ratio = abnormal_count / max(len(normalized_text), 1)
    visual_ratio = max(0.0, min(1.0, float(visual_area_ratio)))
    table_score = _table_alignment_score(normalized_text)
    quality = max(
        0.0,
        min(
            1.0,
            0.55 * min(char_count / min_chars, 1.0)
            + 0.30 * (1.0 - abnormal_ratio)
            + 0.15 * (1.0 - visual_ratio),
        ),
    )
    needs_ocr = (
        char_count < min_chars
        or abnormal_ratio > 0.08
        or visual_ratio > 0.70
    )
    return DocumentPage(
        page_number=page_number,
        text=normalized_text,
        char_count=char_count,
        abnormal_char_ratio=abnormal_ratio,
        table_alignment_score=table_score,
        visual_area_ratio=visual_ratio,
        quality_score=quality,
        needs_ocr=needs_ocr,
    )


def _default_visual_ratio(page: Any) -> float:
    """用嵌入图片数量做保守估算；扫描页仍主要由低文字量触发。"""
    try:
        image_count = len(page.images)
    except (AttributeError, TypeError, KeyError):
        image_count = 0
    return min(0.60, image_count * 0.15)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class DocumentExtractor:
    """保留整份 PDF 的逐页原生文本，并标记需要视觉增强的页面。"""

    parser_version = "pypdf-page-v1"

    def __init__(
        self,
        *,
        reader_factory: Callable[[str], Any] | None = None,
        visual_ratio_estimator: Callable[[Any], float] | None = None,
        min_chars: int = 120,
    ):
        if min_chars <= 0:
            raise ValueError("min_chars 必须大于 0")
        self._reader_factory = reader_factory
        self._visual_ratio_estimator = visual_ratio_estimator or _default_visual_ratio
        self._min_chars = min_chars

    def _reader(self, pdf_path: str):
        if self._reader_factory is not None:
            return self._reader_factory(pdf_path)
        from pypdf import PdfReader

        return PdfReader(pdf_path)

    def extract(self, pdf_path: str, report_id: str) -> DocumentExtraction:
        path = Path(pdf_path)
        reader = self._reader(str(path))
        pages = tuple(
            score_page(
                page_number=index,
                text=page.extract_text() or "",
                visual_area_ratio=self._visual_ratio_estimator(page),
                min_chars=self._min_chars,
            )
            for index, page in enumerate(reader.pages, start=1)
        )
        return DocumentExtraction(
            report_id=report_id,
            pdf_hash=_sha256_file(str(path)),
            pages=pages,
            parser_version=self.parser_version,
        )
