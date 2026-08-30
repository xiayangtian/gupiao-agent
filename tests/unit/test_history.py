"""webapp.history 单元测试"""

import json

from webapp.history import analyzed_periods_for_code, build_flat_history, parse_pdf_filename


def _write_analysis(
    analysis_dir, fname, period=None, code="600900", year=2025, source_file=None
):
    """写入一份 Web 格式分析 JSON；period=None 模拟旧文件缺失 meta.period"""
    meta = {"company": "长江电力（600900）"}
    if period is not None:
        meta["period"] = period
    if source_file is not None:
        meta["source_file"] = source_file
    (analysis_dir / fname).write_text(
        json.dumps({"meta": meta, "dimensions": []}, ensure_ascii=False),
        encoding="utf-8",
    )


class TestAnalyzedPeriodsForCode:
    def test_returns_exact_period_when_meta_present(self, tmp_path):
        _write_analysis(tmp_path, "长江电力_600900_2025_分析报告.json", period="2025-12-31")
        assert analyzed_periods_for_code(str(tmp_path), "600900") == {"2025-12-31"}

    def test_falls_back_to_year_annual_when_period_missing(self, tmp_path):
        """旧分析文件 meta.period 缺失 → 回退为 {year}-12-31"""
        _write_analysis(tmp_path, "长江电力_600900_2024_分析报告.json", period=None, year=2024)
        assert analyzed_periods_for_code(str(tmp_path), "600900") == {"2024-12-31"}

    def test_ignores_other_codes(self, tmp_path):
        _write_analysis(tmp_path, "长江电力_600900_2025_分析报告.json", period="2025-12-31")
        _write_analysis(tmp_path, "贵州茅台_600519_2025_分析报告.json", period="2025-12-31")
        assert analyzed_periods_for_code(str(tmp_path), "600900") == {"2025-12-31"}

    def test_ignores_unparseable_filename(self, tmp_path):
        _write_analysis(tmp_path, "乱七八糟.json", period="2025-12-31")
        assert analyzed_periods_for_code(str(tmp_path), "600900") == set()

    def test_missing_dir_returns_empty(self, tmp_path):
        assert analyzed_periods_for_code(str(tmp_path / "nope"), "600900") == set()


def test_history_scans_q1_and_q3_as_distinct_reports(tmp_path):
    """历史扫描必须保留完整季度期次，并与同季分析产物逐一合并。"""
    reports_dir = tmp_path / "reports"
    analysis_dir = reports_dir / "analysis"
    analysis_dir.mkdir(parents=True)
    reports_dir.mkdir(exist_ok=True)
    for period in ("2025-03-31", "2025-09-30"):
        (reports_dir / f"长江电力_600900_季报_{period}.pdf").write_bytes(b"%PDF")
        _write_analysis(
            analysis_dir,
            f"长江电力_600900_{period}_分析报告.json",
            period=period,
        )

    items = build_flat_history(str(analysis_dir), str(reports_dir))
    quarterly = [item for item in items if item["type"] == "季报"]
    assert {(item["period"], item["pdf_filename"], item["has_analysis"]) for item in quarterly} == {
        ("2025-03-31", "长江电力_600900_季报_2025-03-31.pdf", True),
        ("2025-09-30", "长江电力_600900_季报_2025-09-30.pdf", True),
    }


def test_history_refuses_legacy_year_only_quarterly_filename():
    """历史层不得把无精确期次的旧季报文件猜测为 Q1。"""
    assert parse_pdf_filename("长江电力_600900_季报_2025.pdf") is None


def test_history_rejects_ambiguous_legacy_quarter_analysis_without_hiding_annual(
    tmp_path,
):
    """旧季报 JSON 不得借年份回退冒充同年真实年报。"""
    annual = "A_600900_2025_分析报告.json"
    legacy_quarter = "Z_600900_2025_分析报告.json"
    _write_analysis(
        tmp_path,
        annual,
        source_file="reports/长江电力_600900_年报_2025.pdf",
    )
    _write_analysis(
        tmp_path,
        legacy_quarter,
        source_file="reports/长江电力_600900_季报_2025.pdf",
    )

    items = build_flat_history(str(tmp_path), str(tmp_path / "reports"))

    assert [(item["period"], item["analysis_filename"]) for item in items] == [
        ("2025-12-31", annual),
    ]
