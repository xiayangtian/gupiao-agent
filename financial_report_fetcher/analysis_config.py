"""证据化财报分析配置。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import yaml


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"无法把 {value!r} 解析为布尔值")


@dataclass(frozen=True)
class AnalysisConfig:
    """渐进式分析的可校准参数及安全默认值。"""

    detail_score_threshold: int = 75
    observation_score_threshold: int = 60
    max_detailed_sections: int = 6
    max_quick_conclusions: int = 3
    ocr_enabled: bool = True
    ocr_min_chars: int = 120
    numeric_relative_tolerance: Decimal = Decimal("0.001")
    structured_providers: tuple[str, ...] = ("tushare", "akshare")
    evidence_cache_dir: str = "data/evidence_cache"
    quick_io_workers: int = 2
    deep_workers: int = 2
    ocr_workers: int = 1

    def __post_init__(self) -> None:
        for name in ("detail_score_threshold", "observation_score_threshold"):
            value = getattr(self, name)
            if not 0 <= value <= 100:
                raise ValueError(f"{name} 必须在 0 到 100 之间")
        if self.observation_score_threshold > self.detail_score_threshold:
            raise ValueError("observation_score_threshold 不能高于 detail_score_threshold")
        for name in (
            "max_detailed_sections",
            "max_quick_conclusions",
            "ocr_min_chars",
            "quick_io_workers",
            "deep_workers",
            "ocr_workers",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if self.numeric_relative_tolerance < 0:
            raise ValueError("numeric_relative_tolerance 不能为负数")
        if not self.structured_providers or any(not item.strip() for item in self.structured_providers):
            raise ValueError("structured_providers 必须包含至少一个非空提供方")
        if not self.evidence_cache_dir.strip():
            raise ValueError("evidence_cache_dir 不能为空")

    @classmethod
    def load(cls, path: str = "config.yaml") -> "AnalysisConfig":
        """从 YAML 的 ``analysis`` 段加载配置；文件不存在时使用安全默认值。"""
        config_path = Path(path)
        try:
            with config_path.open(encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
        except OSError:
            return cls()

        if loaded is None:
            return cls()
        if not isinstance(loaded, Mapping):
            raise ValueError("配置文件顶层必须是映射")
        raw = loaded.get("analysis") or {}
        if not isinstance(raw, Mapping):
            raise ValueError("analysis 配置必须是映射")

        defaults = cls()
        providers_raw = raw.get("structured_providers", defaults.structured_providers)
        if not isinstance(providers_raw, (list, tuple)):
            raise ValueError("structured_providers 必须是列表")
        try:
            tolerance = Decimal(str(raw.get(
                "numeric_relative_tolerance", defaults.numeric_relative_tolerance
            )))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("numeric_relative_tolerance 必须是十进制数") from exc

        return cls(
            detail_score_threshold=int(raw.get(
                "detail_score_threshold", defaults.detail_score_threshold
            )),
            observation_score_threshold=int(raw.get(
                "observation_score_threshold", defaults.observation_score_threshold
            )),
            max_detailed_sections=int(raw.get(
                "max_detailed_sections", defaults.max_detailed_sections
            )),
            max_quick_conclusions=int(raw.get(
                "max_quick_conclusions", defaults.max_quick_conclusions
            )),
            ocr_enabled=_as_bool(raw.get("ocr_enabled"), defaults.ocr_enabled),
            ocr_min_chars=int(raw.get("ocr_min_chars", defaults.ocr_min_chars)),
            numeric_relative_tolerance=tolerance,
            structured_providers=tuple(str(item).strip() for item in providers_raw),
            evidence_cache_dir=str(raw.get(
                "evidence_cache_dir", defaults.evidence_cache_dir
            )),
            quick_io_workers=int(raw.get("quick_io_workers", defaults.quick_io_workers)),
            deep_workers=int(raw.get("deep_workers", defaults.deep_workers)),
            ocr_workers=int(raw.get("ocr_workers", defaults.ocr_workers)),
        )
