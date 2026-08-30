"""财务指标事实模型与输入校验。"""

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


METRIC_RULES: Dict[str, Tuple[str, Optional[float], Optional[float]]] = {
    "revenue": ("亿元", 0.0, None),
    "net_profit": ("亿元", None, None),
    "roe": ("%", -1000.0, 1000.0),
    "gross_margin": ("%", -1000.0, 1000.0),
    "debt_ratio": ("%", 0.0, 1000.0),
}


@dataclass
class Evidence:
    """事实证据；尚不能可靠定位时保持为 ``None``。"""

    page: Optional[int]
    quote: Optional[str]
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("Evidence.source 必须是非空字符串")


@dataclass
class FinancialFact:
    metric: str
    value: float
    unit: str
    period: str
    evidence: Optional[Evidence] = None
    validation_status: str = "passed"
    validation_messages: List["ValidationMessage"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "period": self.period,
            "evidence": None if self.evidence is None else {
                "page": self.evidence.page,
                "quote": self.evidence.quote,
                "source": self.evidence.source,
            },
            "validation_status": self.validation_status,
            "validation_messages": [message.to_dict() for message in self.validation_messages],
        }


@dataclass
class ValidationMessage:
    severity: str
    code: str
    message: str
    year: Optional[int] = None
    metric: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.year is not None:
            data["year"] = self.year
        if self.metric is not None:
            data["metric"] = self.metric
        return data


@dataclass
class ValidationSummary:
    status: str
    messages: List[ValidationMessage] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "messages": [message.to_dict() for message in self.messages],
        }


@dataclass
class MetricValidationResult:
    metrics: Optional[List[Dict[str, Any]]]
    facts: List[FinancialFact]
    validation: ValidationSummary


def failed_validation(message: str, code: str = "extraction_failed") -> ValidationSummary:
    """构造抽取层可复用的失败摘要。"""
    return ValidationSummary(
        status="failed",
        messages=[ValidationMessage(severity="error", code=code, message=message)],
    )


def _coerce_number(value: Any) -> Optional[float]:
    """接受模型常见数字字符串，拒绝空值和非有限值。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value.endswith("%"):
            value = value[:-1].strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _coerce_year(value: Any) -> Optional[int]:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def validate_metric_rows(rows: List[Dict[str, Any]], report_year: int) -> MetricValidationResult:
    """校验原始指标行并输出兼容 metrics、规范化 facts 与校验摘要。

    年份上限严格使用报告实际期次的 ``report_year``，不会受执行时系统年份影响。
    同一年同一指标的多个有效值以最后一个为准，并留下 warning。
    """
    messages: List[ValidationMessage] = []
    values_by_year: Dict[int, Dict[str, float]] = {}

    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            messages.append(ValidationMessage(
                severity="error", code="invalid_row", message=f"第 {row_index + 1} 行不是对象",
            ))
            continue
        year = _coerce_year(row.get("year"))
        if year is None or not 2000 <= year <= report_year:
            messages.append(ValidationMessage(
                severity="error",
                code="invalid_year",
                message=f"第 {row_index + 1} 行年份必须在 2000 至报告年份 {report_year} 之间",
            ))
            continue

        for metric, (_unit, minimum, maximum) in METRIC_RULES.items():
            raw_value = row.get(metric)
            if raw_value is None or raw_value == "":
                continue
            value = _coerce_number(raw_value)
            if value is None:
                messages.append(ValidationMessage(
                    severity="error", code="invalid_number",
                    message=f"{year} 年 {metric} 必须是有限数值", year=year, metric=metric,
                ))
                continue
            if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
                messages.append(ValidationMessage(
                    severity="error", code="out_of_range",
                    message=f"{year} 年 {metric} 超出允许范围", year=year, metric=metric,
                ))
                continue

            year_values = values_by_year.setdefault(year, {})
            if metric in year_values:
                messages.append(ValidationMessage(
                    severity="warning", code="duplicate_metric",
                    message=f"{year} 年 {metric} 重复，保留最后一个有效值", year=year, metric=metric,
                ))
            year_values[metric] = value

    if not values_by_year:
        messages.append(ValidationMessage(
            severity="error", code="no_valid_facts", message="未发现有效财务事实",
        ))
        return MetricValidationResult(
            metrics=None,
            facts=[],
            validation=ValidationSummary(status="failed", messages=messages),
        )

    metrics: List[Dict[str, Any]] = []
    facts: List[FinancialFact] = []
    for year in sorted(values_by_year):
        values = values_by_year[year]
        metrics.append({"year": year, **{metric: values.get(metric) for metric in METRIC_RULES}})
        facts.extend(
            FinancialFact(metric=metric, value=values[metric], unit=METRIC_RULES[metric][0], period=str(year))
            for metric in METRIC_RULES
            if metric in values
        )

    return MetricValidationResult(
        metrics=metrics,
        facts=facts,
        validation=ValidationSummary(status="warning" if messages else "passed", messages=messages),
    )
