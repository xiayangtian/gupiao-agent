"""webapp.history 单元测试"""

import json

from webapp.history import analyzed_periods_for_code


def _write_analysis(analysis_dir, fname, period=None, code="600900", year=2025):
    """写入一份 Web 格式分析 JSON；period=None 模拟旧文件缺失 meta.period"""
    meta = {"company": "长江电力（600900）"}
    if period is not None:
        meta["period"] = period
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
