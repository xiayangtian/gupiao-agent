"""黄金报告清单和质量指标计算测试。"""

import json
import os
from pathlib import Path

import pytest

from scripts.evaluate_analysis_quality import evaluate_cases


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "analysis"
MANIFEST = ROOT / "tests" / "quality" / "golden_manifest.json"


def test_offline_cases_measure_accuracy_conflicts_filtering_and_latency():
    cases = [
        json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        for name in ("verified_report.json", "conflicting_sources.json", "scanned_pages.json")
    ]

    metrics = evaluate_cases(cases)

    assert metrics.numeric_fact_accuracy == 1.0
    assert metrics.entity_scope_accuracy == 1.0
    assert metrics.unsupported_deterministic_claims == 0
    assert metrics.unresolved_conflicts_auto_selected == 0
    assert metrics.filtered_low_evidence_topics >= 1
    assert metrics.quick_latency_seconds < metrics.completion_latency_seconds


def test_golden_manifest_covers_nine_report_risk_classes():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    categories = {item["category"] for item in manifest["cases"]}
    assert categories == {
        "industrial", "financial", "scanned", "cross_page_table", "chart",
        "multi_entity", "missing_source", "source_conflict", "legacy",
    }


def test_real_report_evaluation_is_explicitly_opt_in():
    if not os.environ.get("GOLDEN_REPORT_DIR"):
        pytest.skip("设置 GOLDEN_REPORT_DIR 后才运行本地真实年报质量评估")
