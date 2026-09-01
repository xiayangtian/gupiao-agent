"""渐进式财报分析配置的行为测试。"""

from decimal import Decimal

from financial_report_fetcher.analysis_config import AnalysisConfig


def test_analysis_config_loads_overrides_and_keeps_spec_defaults(tmp_path):
    """局部配置不能抹掉评分、数量和并发的安全默认值。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        "analysis:\n"
        "  detail_score_threshold: 80\n"
        "  structured_providers: [akshare]\n"
        "  numeric_relative_tolerance: 0.0025\n",
        encoding="utf-8",
    )

    config = AnalysisConfig.load(str(path))

    assert config.detail_score_threshold == 80
    assert config.observation_score_threshold == 60
    assert config.max_detailed_sections == 6
    assert config.max_quick_conclusions == 3
    assert config.structured_providers == ("akshare",)
    assert config.numeric_relative_tolerance == Decimal("0.0025")
    assert (config.quick_io_workers, config.deep_workers, config.ocr_workers) == (2, 2, 1)


def test_analysis_config_missing_file_returns_safe_defaults(tmp_path):
    """没有本机私有配置时，证据基础层仍应以安全默认值启动。"""
    config = AnalysisConfig.load(str(tmp_path / "missing.yaml"))

    assert config.detail_score_threshold == 75
    assert config.observation_score_threshold == 60
    assert config.ocr_enabled is True
    assert config.ocr_min_chars == 120
    assert config.evidence_cache_dir == "data/evidence_cache"
    assert config.structured_providers == ("tushare", "akshare")


def test_analysis_config_rejects_inverted_score_thresholds(tmp_path):
    """观察门槛高于详细门槛会破坏评分分流，必须在加载时拒绝。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        "analysis:\n"
        "  detail_score_threshold: 50\n"
        "  observation_score_threshold: 60\n",
        encoding="utf-8",
    )

    try:
        AnalysisConfig.load(str(path))
    except ValueError as exc:
        assert "observation_score_threshold" in str(exc)
    else:
        raise AssertionError("阈值倒置时应拒绝配置")
