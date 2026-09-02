#!/usr/bin/env python3
"""计算证据化财报分析的离线质量指标，可选检查本地黄金 PDF 集。"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


@dataclass(frozen=True)
class QualityMetrics:
    numeric_fact_accuracy: float
    entity_scope_accuracy: float
    unsupported_deterministic_claims: int
    unresolved_conflicts_auto_selected: int
    filtered_low_evidence_topics: int
    quick_latency_seconds: float
    completion_latency_seconds: float


def _ratio(matches: int, total: int) -> float:
    return round(matches / total, 6) if total else 1.0


def evaluate_cases(cases: Iterable[dict[str, Any]]) -> QualityMetrics:
    """从可重复的 expected/actual fixture 计算准确性与时延汇总。"""
    numeric_total = numeric_matches = 0
    scope_total = scope_matches = 0
    unsupported = auto_selected_conflicts = filtered = 0
    quick_latencies: list[float] = []
    completion_latencies: list[float] = []

    for case in cases:
        for fact in case.get("facts", []):
            if fact.get("expected_value") is not None:
                numeric_total += 1
                numeric_matches += fact.get("actual_value") == fact.get("expected_value")
            if fact.get("expected_scope") is not None:
                scope_total += 1
                scope_matches += fact.get("actual_scope") == fact.get("expected_scope")
        unsupported += sum(
            1 for claim in case.get("claims", [])
            if claim.get("deterministic") and not claim.get("supported")
        )
        auto_selected_conflicts += sum(
            1 for conflict in case.get("conflicts", [])
            if not conflict.get("resolved") and conflict.get("auto_selected")
        )
        filtered += sum(
            1 for topic in case.get("topics", [])
            if topic.get("low_evidence") and topic.get("filtered")
        )
        performance = case.get("performance") or {}
        if performance.get("quick_latency_seconds") is not None:
            quick_latencies.append(float(performance["quick_latency_seconds"]))
        if performance.get("completion_latency_seconds") is not None:
            completion_latencies.append(float(performance["completion_latency_seconds"]))

    return QualityMetrics(
        numeric_fact_accuracy=_ratio(numeric_matches, numeric_total),
        entity_scope_accuracy=_ratio(scope_matches, scope_total),
        unsupported_deterministic_claims=unsupported,
        unresolved_conflicts_auto_selected=auto_selected_conflicts,
        filtered_low_evidence_topics=filtered,
        quick_latency_seconds=round(mean(quick_latencies), 6) if quick_latencies else 0.0,
        completion_latency_seconds=(
            round(mean(completion_latencies), 6) if completion_latencies else 0.0
        ),
    )


def _load_cases(manifest_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parents[2]
    fixture_cases = []
    real_root = os.environ.get("GOLDEN_REPORT_DIR")
    real_reports = {"enabled": bool(real_root), "found": 0, "missing": []}
    for item in manifest.get("cases", []):
        fixture = item.get("fixture")
        if fixture:
            fixture_cases.append(json.loads((root / fixture).read_text(encoding="utf-8")))
        if real_root and item.get("pdf"):
            pdf = Path(real_root) / item["pdf"]
            if pdf.is_file():
                real_reports["found"] += 1
            else:
                real_reports["missing"].append(item["id"])
    return fixture_cases, real_reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    cases, real_reports = _load_cases(args.manifest)
    payload = {"metrics": asdict(evaluate_cases(cases)), "real_reports": real_reports}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
