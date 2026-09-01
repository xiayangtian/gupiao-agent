"""可选的 PP-StructureV3 定向 OCR 增强。"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .models import (
    EntityScope,
    EvidenceRecord,
    SourceLocator,
    SourceType,
    VerificationState,
)


class OcrUnavailable(RuntimeError):
    """本地 OCR 依赖不可用。"""


@dataclass(frozen=True)
class OcrBlock:
    page_number: int
    label: str
    text: str
    confidence: float
    bbox: tuple[float, float, float, float] | None
    block_id: str

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("OcrBlock.page_number 必须从 1 开始")
        if not self.label.strip() or not self.block_id.strip():
            raise ValueError("OcrBlock.label 和 block_id 不能为空")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("OcrBlock.confidence 必须在 0 到 1 之间")
        if self.bbox is not None:
            if len(self.bbox) != 4:
                raise ValueError("OcrBlock.bbox 必须包含四个坐标")
            object.__setattr__(self, "bbox", tuple(float(value) for value in self.bbox))


class OcrBackend(Protocol):
    def extract_page(self, pdf_path: str, page_number: int) -> Sequence[OcrBlock]:
        """提取单个指定页面。"""


class OcrEngine(Protocol):
    def enrich(
        self,
        pdf_path: str,
        report_id: str,
        pages: Sequence[int],
    ) -> list[EvidenceRecord]:
        """把指定页面增强为统一证据。"""


def _plain_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    return list(value) if isinstance(value, Iterable) and not isinstance(value, (str, bytes)) else []


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    points = _plain_list(value)
    if len(points) == 4 and all(isinstance(item, (int, float)) for item in points):
        return tuple(float(item) for item in points)
    flattened: list[tuple[float, float]] = []
    for point in points:
        pair = _plain_list(point)
        if len(pair) >= 2:
            try:
                flattened.append((float(pair[0]), float(pair[1])))
            except (TypeError, ValueError):
                continue
    if not flattened:
        return None
    xs = [point[0] for point in flattened]
    ys = [point[1] for point in flattened]
    return min(xs), min(ys), max(xs), max(ys)


def _average_score(values: Any, default: float = 0.70) -> float:
    scores: list[float] = []
    for value in _plain_list(values):
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        if 0.0 <= score <= 1.0:
            scores.append(score)
    return sum(scores) / len(scores) if scores else default


def normalize_paddle_result(payload: Mapping[str, Any], page_number: int) -> list[OcrBlock]:
    """把 PP-StructureV3 ``result.json`` 归一化为稳定的页面块。"""
    raw = payload.get("res") if isinstance(payload.get("res"), Mapping) else payload
    overall = raw.get("overall_ocr_res") or {}
    page_confidence = _average_score(overall.get("rec_scores"))
    blocks: list[OcrBlock] = []

    table_results = raw.get("table_res_list") or []
    for table_index, table in enumerate(table_results):
        if not isinstance(table, Mapping):
            continue
        prediction = table.get("table_ocr_pred") or {}
        texts = _plain_list(prediction.get("rec_texts"))
        scores = _plain_list(prediction.get("rec_scores"))
        boxes = _plain_list(prediction.get("rec_boxes"))
        for cell_index, text in enumerate(texts):
            normalized_text = str(text).strip()
            if not normalized_text:
                continue
            confidence = _average_score(
                [scores[cell_index]] if cell_index < len(scores) else [],
                default=page_confidence,
            )
            blocks.append(OcrBlock(
                page_number=page_number,
                label="table_cell",
                text=normalized_text,
                confidence=confidence,
                bbox=_bbox(boxes[cell_index]) if cell_index < len(boxes) else None,
                block_id=f"table-{table_index}-cell-{cell_index}",
            ))

    has_table_cells = any(block.label == "table_cell" for block in blocks)
    for block_index, block in enumerate(raw.get("parsing_res_list") or []):
        if not isinstance(block, Mapping):
            continue
        label = str(block.get("block_label") or "text").strip().lower()
        if label == "table" and has_table_cells:
            continue
        text = str(block.get("block_content") or "").strip()
        if not text:
            continue
        blocks.append(OcrBlock(
            page_number=page_number,
            label=label,
            text=text,
            confidence=page_confidence,
            bbox=_bbox(block.get("block_bbox")),
            block_id=str(block.get("block_id", block_index)),
        ))
    return blocks


class PaddleStructureBackend:
    """懒加载 PP-StructureV3，并用单页临时 PDF 保证定向处理。"""

    def __init__(self, pipeline=None):
        self._pipeline = pipeline

    @property
    def pipeline(self):
        if self._pipeline is None:
            try:
                module = importlib.import_module("paddleocr")
            except (ImportError, ModuleNotFoundError) as exc:
                raise OcrUnavailable(
                    "本地 OCR 未安装，请执行 pip install -r requirements-ocr.txt"
                ) from exc
            self._pipeline = module.PPStructureV3(
                use_table_recognition=True,
                use_chart_recognition=True,
                use_formula_recognition=False,
            )
        return self._pipeline

    def extract_page(self, pdf_path: str, page_number: int) -> list[OcrBlock]:
        pipeline = self.pipeline
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(pdf_path)
        if page_number < 1 or page_number > len(reader.pages):
            raise ValueError(f"OCR 页码超出 PDF 范围: {page_number}")
        with tempfile.TemporaryDirectory(prefix="gp-agent-ocr-") as directory:
            page_path = Path(directory) / f"page-{page_number}.pdf"
            writer = PdfWriter()
            writer.add_page(reader.pages[page_number - 1])
            with page_path.open("wb") as handle:
                writer.write(handle)
            output = pipeline.predict(str(page_path))
            blocks: list[OcrBlock] = []
            for result in output:
                payload = result.json if hasattr(result, "json") else result
                if callable(payload):
                    payload = payload()
                if not isinstance(payload, Mapping):
                    raise RuntimeError("PP-StructureV3 返回了非对象结果")
                blocks.extend(normalize_paddle_result(payload, page_number))
            return blocks


def _numeric_value(text: str) -> tuple[Decimal | None, str | None]:
    normalized = text.strip().replace(",", "")
    match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)(%)?", normalized)
    if not match:
        return None, None
    try:
        value = Decimal(match.group(1))
    except InvalidOperation:
        return None, None
    return value, "percent" if match.group(2) else None


def _source_type(label: str) -> SourceType:
    normalized = label.lower()
    if normalized in {"table", "table_cell"}:
        return SourceType.OCR_TABLE
    if "chart" in normalized:
        return SourceType.CHART
    return SourceType.OCR_TEXT


def _block_hash(block: OcrBlock) -> str:
    payload = json.dumps(
        {
            "page": block.page_number,
            "label": block.label,
            "text": block.text,
            "confidence": block.confidence,
            "bbox": block.bbox,
            "block_id": block.block_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PaddleStructureEngine:
    parser_version = "pp-structure-v3"

    def __init__(self, backend: OcrBackend | None = None, min_confidence: float = 0.80):
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence 必须在 0 到 1 之间")
        self._backend = backend or PaddleStructureBackend()
        self._min_confidence = min_confidence

    def enrich(
        self,
        pdf_path: str,
        report_id: str,
        pages: Sequence[int],
    ) -> list[EvidenceRecord]:
        selected_pages: list[int] = []
        for raw_page in pages:
            page_number = int(raw_page)
            if page_number < 1:
                raise ValueError("OCR 页码必须从 1 开始")
            if page_number not in selected_pages:
                selected_pages.append(page_number)

        records: list[EvidenceRecord] = []
        for page_number in selected_pages:
            for block in self._backend.extract_page(pdf_path, page_number):
                source_type = _source_type(block.label)
                value: Decimal | None = None
                unit: str | None = None
                if source_type is SourceType.OCR_TABLE and block.confidence >= self._min_confidence:
                    value, unit = _numeric_value(block.text)
                fact_name = {
                    SourceType.OCR_TABLE: "ocr_table_cell",
                    SourceType.CHART: "ocr_chart",
                    SourceType.OCR_TEXT: "ocr_text",
                }[source_type]
                records.append(EvidenceRecord(
                    report_id=report_id,
                    entity_scope=EntityScope.UNKNOWN,
                    fact_name=fact_name,
                    value=value,
                    unit=unit,
                    currency=None,
                    period="unknown",
                    source_type=source_type,
                    source_locator=SourceLocator(
                        provider="paddleocr",
                        page=page_number,
                        section=block.label,
                        bbox=block.bbox,
                        record_id=block.block_id,
                    ),
                    extraction_confidence=block.confidence,
                    verification_state=VerificationState.UNKNOWN_SCOPE,
                    content_hash=_block_hash(block),
                    parser_version=self.parser_version,
                    text=block.text,
                ))
        return records
