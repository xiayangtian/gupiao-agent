"""经 PDF 页码证据校验的财务结构可视化 v4 数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from typing import Any, Mapping, Sequence

from .evidence.models import EntityScope, EvidenceRecord, SourceType


CARD_SPECS = {
    "profit_structure": {
        "kind": "profit",
        "core": frozenset({"revenue", "operating_cost", "total_profit", "net_profit"}),
        "optional": frozenset({
            "taxes_and_surcharges",
            "selling_expense",
            "administrative_expense",
            "research_and_development_expense",
            "financial_expense",
        }),
    },
    "balance_sheet_structure": {
        "kind": "balance_sheet",
        "core": frozenset({"total_assets", "total_liabilities", "total_equity"}),
        "optional": frozenset({"loans_and_advances", "customer_deposits"}),
    },
    "cash_flow_structure": {
        "kind": "cash_flow",
        "core": frozenset({
            "operating_cash_flow", "investing_cash_flow", "financing_cash_flow",
        }),
        "optional": frozenset({"cash_and_equivalents_net_increase"}),
    },
}

CARD_TITLES = {
    "profit_structure": "利润结构",
    "balance_sheet_structure": "资产负债结构",
    "cash_flow_structure": "现金流结构",
}

_VALID_DIRECTIONS = frozenset({"inflow", "outflow", "neutral"})


@dataclass(frozen=True)
class VisualizationRow:
    """一个可由 PDF 原文页码定位的金额披露项。"""

    metric_id: str
    label: str
    value: float
    unit: str
    direction: str
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "direction": self.direction,
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualizationRow":
        value = _finite_number(data.get("value"))
        if value is None:
            raise ValueError("visualization row value 必须是有限数值")
        metric_id = _non_empty_string(data.get("metric_id"), "metric_id")
        label = _non_empty_string(data.get("label"), "label")
        unit = _non_empty_string(data.get("unit"), "unit")
        direction = _non_empty_string(data.get("direction"), "direction")
        evidence_ids = _evidence_ids(data.get("evidence_ids"))
        return cls(metric_id, label, value, unit, direction, evidence_ids)


@dataclass(frozen=True)
class VisualizationCard:
    """单个主题 Tab 顶部的财务结构卡片。"""

    id: str
    topic_id: str
    title: str
    kind: str
    status: str
    unavailable_reason: str | None
    rows: tuple[VisualizationRow, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic_id": self.topic_id,
            "title": self.title,
            "kind": self.kind,
            "status": self.status,
            "unavailable_reason": self.unavailable_reason,
            "rows": [row.to_dict() for row in self.rows],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualizationCard":
        rows = data.get("rows", ())
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ValueError("visualization card rows 必须是数组")
        unavailable_reason = data.get("unavailable_reason")
        if unavailable_reason is not None:
            unavailable_reason = _non_empty_string(unavailable_reason, "unavailable_reason")
        return cls(
            id=_non_empty_string(data.get("id"), "id"),
            topic_id=_non_empty_string(data.get("topic_id"), "topic_id"),
            title=_non_empty_string(data.get("title"), "title"),
            kind=_non_empty_string(data.get("kind"), "kind"),
            status=_non_empty_string(data.get("status"), "status"),
            unavailable_reason=unavailable_reason,
            rows=tuple(VisualizationRow.from_dict(row) for row in rows),
        )


@dataclass(frozen=True)
class VisualizationBundle:
    """v4 报告持久化的可视化数据；空 cards 表示无可用结构图。"""

    version: int
    cards: tuple[VisualizationCard, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "cards": [card.to_dict() for card in self.cards]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualizationBundle":
        version = data.get("version")
        if version != 1:
            raise ValueError("visualizations.version 必须为 1")
        cards = data.get("cards", ())
        if not isinstance(cards, Sequence) or isinstance(cards, (str, bytes)):
            raise ValueError("visualizations.cards 必须是数组")
        return cls(version=1, cards=tuple(VisualizationCard.from_dict(card) for card in cards))


def validate_visualization_payload(
    payload: Mapping[str, Any],
    *,
    period: str,
    allowed_pdf_evidence: Mapping[str, EvidenceRecord],
) -> VisualizationBundle:
    """丢弃不可定位或口径不安全的行，绝不让它们进入图表契约。"""
    raw_cards = payload.get("cards", ()) if isinstance(payload, Mapping) else ()
    if not isinstance(raw_cards, Sequence) or isinstance(raw_cards, (str, bytes)):
        return VisualizationBundle(version=1, cards=())

    cards: list[VisualizationCard] = []
    seen_card_ids: set[str] = set()
    for raw_card in raw_cards:
        if not isinstance(raw_card, Mapping):
            continue
        card_id = raw_card.get("id")
        spec = CARD_SPECS.get(card_id)
        if not spec or card_id in seen_card_ids:
            continue
        kind = raw_card.get("kind")
        topic_id = raw_card.get("topic_id")
        if kind != spec["kind"] or not isinstance(topic_id, str) or not topic_id.strip():
            continue
        seen_card_ids.add(card_id)
        rows, reasons = _validated_rows(
            raw_card.get("rows", ()),
            spec=spec,
            period=period,
            allowed_pdf_evidence=allowed_pdf_evidence,
        )
        core_count = sum(row.metric_id in spec["core"] for row in rows)
        if core_count == len(spec["core"]):
            status = "complete"
            unavailable_reason = None
        elif core_count >= 2:
            status = "partial"
            unavailable_reason = None
        else:
            status = "unavailable"
            rows = []
            unavailable_reason = _unavailable_reason(reasons, core_count)
        title = raw_card.get("title")
        cards.append(VisualizationCard(
            id=card_id,
            topic_id=topic_id.strip(),
            title=title.strip() if isinstance(title, str) and title.strip() else CARD_TITLES[card_id],
            kind=kind,
            status=status,
            unavailable_reason=unavailable_reason,
            rows=tuple(rows),
        ))
    return VisualizationBundle(version=1, cards=tuple(cards))


def _validated_rows(
    raw_rows: Any,
    *,
    spec: Mapping[str, Any],
    period: str,
    allowed_pdf_evidence: Mapping[str, EvidenceRecord],
) -> tuple[list[VisualizationRow], list[str]]:
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        return [], ["未提供有效披露项"]
    rows: list[VisualizationRow] = []
    reasons: list[str] = []
    seen_metrics: set[str] = set()
    allowed_metrics = spec["core"] | spec["optional"]
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            reasons.append("存在非对象披露项")
            continue
        metric_id = raw_row.get("metric_id")
        if not isinstance(metric_id, str) or metric_id not in allowed_metrics:
            reasons.append("存在不在白名单内的指标")
            continue
        if metric_id in seen_metrics:
            reasons.append(f"指标 {metric_id} 重复")
            continue
        value = _finite_number(raw_row.get("value"))
        if value is None:
            reasons.append(f"指标 {metric_id} 的金额不是有限数值")
            continue
        if raw_row.get("unit") != "亿元":
            reasons.append(f"指标 {metric_id} 未统一为亿元")
            continue
        label = raw_row.get("label")
        if not isinstance(label, str) or not label.strip():
            reasons.append(f"指标 {metric_id} 缺少显示名称")
            continue
        direction = raw_row.get("direction", "neutral")
        if not _valid_direction(direction, spec["kind"], value):
            reasons.append(f"指标 {metric_id} 的方向不合法")
            continue
        evidence_ids = _valid_evidence_ids(
            raw_row.get("evidence_ids"), period, allowed_pdf_evidence
        )
        if not evidence_ids:
            reasons.append(f"指标 {metric_id} 缺少可定位的 PDF 页码证据")
            continue
        seen_metrics.add(metric_id)
        rows.append(VisualizationRow(
            metric_id=metric_id,
            label=label.strip(),
            value=value,
            unit="亿元",
            direction=direction,
            evidence_ids=evidence_ids,
        ))
    return rows, reasons


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if isfinite(result) else None


def _valid_direction(direction: Any, kind: str, value: float) -> bool:
    if direction not in _VALID_DIRECTIONS:
        return False
    if kind != "cash_flow":
        return direction == "neutral"
    if value > 0:
        return direction == "inflow"
    if value < 0:
        return direction == "outflow"
    return direction == "neutral"


def _valid_evidence_ids(
    value: Any,
    period: str,
    allowed_pdf_evidence: Mapping[str, EvidenceRecord],
) -> tuple[str, ...]:
    try:
        evidence_ids = _evidence_ids(value)
    except ValueError:
        return ()
    valid_ids = tuple(
        evidence_id for evidence_id in evidence_ids
        if _is_valid_pdf_evidence(allowed_pdf_evidence.get(evidence_id), period)
    )
    return valid_ids


def _is_valid_pdf_evidence(record: EvidenceRecord | None, period: str) -> bool:
    return bool(
        record
        and record.source_type is SourceType.PDF_TEXT
        and record.entity_scope is EntityScope.CONSOLIDATED
        and record.period == period
        and isinstance(record.source_locator.page, int)
        and not isinstance(record.source_locator.page, bool)
        and record.source_locator.page > 0
    )


def _evidence_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("evidence_ids 必须是数组")
    ids = tuple(item for item in value if isinstance(item, str) and item.strip())
    if not ids:
        raise ValueError("evidence_ids 至少包含一个证据 ID")
    return tuple(dict.fromkeys(ids))


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _unavailable_reason(reasons: Sequence[str], core_count: int) -> str:
    if core_count < 2:
        return "可定位的已核验核心指标不足两个，无法安全绘图"
    if reasons:
        return "；".join(dict.fromkeys(reasons))
    return "未提供可安全绘制的已核验披露项"
