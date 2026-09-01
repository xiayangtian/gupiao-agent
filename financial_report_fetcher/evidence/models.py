"""跨结构化数据、PDF 与 OCR 的统一证据契约。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping


class EntityScope(str, Enum):
    CONSOLIDATED = "consolidated"
    PARENT = "parent"
    UNKNOWN = "unknown"


class SourceType(str, Enum):
    STRUCTURED = "structured"
    PDF_TEXT = "pdf_text"
    OCR_TEXT = "ocr_text"
    OCR_TABLE = "ocr_table"
    CHART = "chart"
    NARRATIVE = "narrative"


class VerificationState(str, Enum):
    VERIFIED = "verified"
    SINGLE_SOURCE = "single_source"
    CONFLICT = "conflict"
    UNKNOWN_SCOPE = "unknown_scope"


@dataclass(frozen=True)
class SourceLocator:
    provider: str | None = None
    page: int | None = None
    section: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    record_id: str | None = None

    def __post_init__(self) -> None:
        if self.page is not None and self.page < 1:
            raise ValueError("SourceLocator.page 必须从 1 开始")
        if self.bbox is not None:
            if len(self.bbox) != 4:
                raise ValueError("SourceLocator.bbox 必须包含四个坐标")
            object.__setattr__(self, "bbox", tuple(float(value) for value in self.bbox))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.bbox is not None:
            data["bbox"] = list(self.bbox)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceLocator":
        bbox = data.get("bbox")
        return cls(
            provider=data.get("provider"),
            page=data.get("page"),
            section=data.get("section"),
            bbox=None if bbox is None else tuple(bbox),
            record_id=data.get("record_id"),
        )


@dataclass(frozen=True)
class EvidenceRecord:
    report_id: str
    entity_scope: EntityScope
    fact_name: str
    value: Decimal | None
    unit: str | None
    currency: str | None
    period: str
    source_type: SourceType
    source_locator: SourceLocator
    extraction_confidence: float
    verification_state: VerificationState
    content_hash: str
    parser_version: str
    text: str | None = None
    adjustment_state: str | None = None
    source_timestamp: str | None = None
    raw_field_name: str | None = None

    def __post_init__(self) -> None:
        for name in ("report_id", "fact_name", "period", "content_hash", "parser_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空字符串")
        if not 0.0 <= self.extraction_confidence <= 1.0:
            raise ValueError("extraction_confidence 必须在 0 到 1 之间")
        if self.value is not None and not isinstance(self.value, Decimal):
            try:
                object.__setattr__(self, "value", Decimal(str(self.value)))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("value 必须是十进制数或 None") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "entity_scope": self.entity_scope.value,
            "fact_name": self.fact_name,
            "value": None if self.value is None else str(self.value),
            "unit": self.unit,
            "currency": self.currency,
            "period": self.period,
            "source_type": self.source_type.value,
            "source_locator": self.source_locator.to_dict(),
            "extraction_confidence": self.extraction_confidence,
            "verification_state": self.verification_state.value,
            "content_hash": self.content_hash,
            "parser_version": self.parser_version,
            "text": self.text,
            "adjustment_state": self.adjustment_state,
            "source_timestamp": self.source_timestamp,
            "raw_field_name": self.raw_field_name,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceRecord":
        value = data.get("value")
        return cls(
            report_id=str(data["report_id"]),
            entity_scope=EntityScope(data["entity_scope"]),
            fact_name=str(data["fact_name"]),
            value=None if value is None else Decimal(str(value)),
            unit=data.get("unit"),
            currency=data.get("currency"),
            period=str(data["period"]),
            source_type=SourceType(data["source_type"]),
            source_locator=SourceLocator.from_dict(data["source_locator"]),
            extraction_confidence=float(data["extraction_confidence"]),
            verification_state=VerificationState(data["verification_state"]),
            content_hash=str(data["content_hash"]),
            parser_version=str(data["parser_version"]),
            text=data.get("text"),
            adjustment_state=data.get("adjustment_state"),
            source_timestamp=data.get("source_timestamp"),
            raw_field_name=data.get("raw_field_name"),
        )

    @property
    def stable_id(self) -> str:
        payload = self.to_dict()
        payload.pop("verification_state", None)
        payload.pop("source_timestamp", None)
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
