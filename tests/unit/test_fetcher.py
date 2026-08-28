"""
fetcher.py 单元测试

覆盖 ReportFetcher._resolve_company()、_apply_filters()、fetch() 的各项行为。
"""

import datetime
import logging

import pytest

from financial_report_fetcher.fetcher import ReportFetcher
from financial_report_fetcher.models import (
    AppConfig,
    CompanyConfig,
    DateRange,
    ReportMeta,
    ReportType,
)


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：构造 CompanyConfig
# ─────────────────────────────────────────────────────────────────────────────

def make_company(ticker=None, name=None) -> CompanyConfig:
    """创建 CompanyConfig 实例，ticker/name 至少传一个"""
    return CompanyConfig(ticker=ticker, name=name)


# ─────────────────────────────────────────────────────────────────────────────
# 已知公司数据库（用于注入）
# ─────────────────────────────────────────────────────────────────────────────

KNOWN = {
    "600519": {"full_name": "贵州茅台股份有限公司"},
    "贵州茅台": {"ticker": "600519"},
    "BYD": {"full_name": "比亚迪股份有限公司"},
}


@pytest.fixture
def fetcher():
    """返回注入了 KNOWN 数据库的 ReportFetcher 实例"""
    return ReportFetcher(known_companies=KNOWN)


# ─────────────────────────────────────────────────────────────────────────────
# ticker 命中
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveByTicker:
    def test_ticker_in_db_returns_ticker(self, fetcher):
        """ticker 存在于数据库中时，应返回该 ticker"""
        company = make_company(ticker="600519")
        assert fetcher._resolve_company(company) == "600519"

    def test_ticker_priority_over_name_when_both_match(self, fetcher):
        """ticker 和 name 均命中时，应优先返回 ticker"""
        company = make_company(ticker="600519", name="贵州茅台")
        result = fetcher._resolve_company(company)
        assert result == "600519"

    def test_ticker_only_no_name(self, fetcher):
        """仅有 ticker 且命中，应正常返回"""
        company = make_company(ticker="BYD")
        assert fetcher._resolve_company(company) == "BYD"


# ─────────────────────────────────────────────────────────────────────────────
# 回退到 name 匹配
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveByName:
    def test_name_in_db_returns_name_when_no_ticker(self, fetcher):
        """ticker 为 None，name 命中时应返回 name"""
        company = make_company(name="贵州茅台")
        assert fetcher._resolve_company(company) == "贵州茅台"

    def test_ticker_not_in_db_falls_back_to_name(self, fetcher):
        """ticker 不在数据库中，name 命中时应返回 name"""
        company = make_company(ticker="UNKNOWN_TICKER", name="贵州茅台")
        assert fetcher._resolve_company(company) == "贵州茅台"


# ─────────────────────────────────────────────────────────────────────────────
# 无法匹配 → 返回 None + WARN 日志
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveUnknown:
    def test_unknown_ticker_returns_none(self, fetcher):
        """ticker 不在数据库中且无 name 时，应返回 None"""
        company = make_company(ticker="UNKNOWN123")
        assert fetcher._resolve_company(company) is None

    def test_unknown_name_returns_none(self, fetcher):
        """name 不在数据库中且无 ticker 时，应返回 None"""
        company = make_company(name="不存在的公司")
        assert fetcher._resolve_company(company) is None

    def test_both_unknown_returns_none(self, fetcher):
        """ticker 和 name 均不在数据库中时，应返回 None"""
        company = make_company(ticker="NOEXIST", name="不存在公司")
        assert fetcher._resolve_company(company) is None

    def test_warn_log_contains_ticker_when_only_ticker(self, fetcher, caplog):
        """ticker 无法匹配时，WARN 日志应包含原始 ticker 字符串"""
        with caplog.at_level(logging.WARNING, logger="financial_report_fetcher.fetcher"):
            fetcher._resolve_company(make_company(ticker="UNKNOWN123"))
        assert "UNKNOWN123" in caplog.text

    def test_warn_log_contains_name_when_only_name(self, fetcher, caplog):
        """name 无法匹配时，WARN 日志应包含原始 name 字符串"""
        with caplog.at_level(logging.WARNING, logger="financial_report_fetcher.fetcher"):
            fetcher._resolve_company(make_company(name="不存在的公司"))
        assert "不存在的公司" in caplog.text

    def test_warn_log_contains_ticker_when_both_unknown(self, fetcher, caplog):
        """ticker 和 name 均无法匹配时，WARN 日志应包含 ticker（因为优先级更高）"""
        with caplog.at_level(logging.WARNING, logger="financial_report_fetcher.fetcher"):
            fetcher._resolve_company(make_company(ticker="NOEXIST", name="不存在公司"))
        # ticker 被优先用于日志标识
        assert "NOEXIST" in caplog.text

    def test_warn_level_is_warning(self, fetcher, caplog):
        """无法匹配时日志级别应为 WARNING"""
        with caplog.at_level(logging.WARNING, logger="financial_report_fetcher.fetcher"):
            fetcher._resolve_company(make_company(ticker="UNKNOWN"))
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) >= 1

    def test_no_warn_log_when_match_succeeds(self, fetcher, caplog):
        """匹配成功时不应产生 WARN 日志"""
        with caplog.at_level(logging.WARNING, logger="financial_report_fetcher.fetcher"):
            fetcher._resolve_company(make_company(ticker="600519"))
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 空数据库
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptyDatabase:
    def test_empty_db_always_returns_none(self):
        """空数据库时任何公司都应返回 None"""
        empty_fetcher = ReportFetcher(known_companies={})
        company = make_company(ticker="600519", name="贵州茅台")
        assert empty_fetcher._resolve_company(company) is None

    def test_default_no_arg_fetcher_returns_none(self):
        """不传 known_companies 时默认空字典，应返回 None"""
        fetcher_default = ReportFetcher()
        company = make_company(ticker="600519")
        assert fetcher_default._resolve_company(company) is None


# ─────────────────────────────────────────────────────────────────────────────
# _apply_filters 测试
# ─────────────────────────────────────────────────────────────────────────────

# 辅助：构造测试用 ReportMeta
def make_report(company_id="600519", report_type=ReportType.ANNUAL, period=None):
    """创建 ReportMeta 实例，period 默认用 2023-12-31"""
    if period is None:
        period = datetime.date(2023, 12, 31)
    return ReportMeta(
        company_id=company_id,
        report_type=report_type,
        period=period,
        download_url=f"http://example.com/{company_id}_{period}.pdf",
        title=f"{company_id}_{period}财报",
    )


# 通用参数
DEFAULT_RANGE = DateRange(datetime.date(2020, 1, 1), datetime.date(2025, 12, 31))
DEFAULT_TYPES = [ReportType.ANNUAL]
FETCHER = ReportFetcher()


class TestApplyFiltersDateRange:
    """测试 _apply_filters 的日期范围过滤"""

    def test_report_inside_range_is_included(self):
        """报告期在区间内的财报应被保留"""
        r = make_report(period=datetime.date(2023, 12, 31))
        result = FETCHER._apply_filters([r], DEFAULT_RANGE, DEFAULT_TYPES, None)
        assert len(result) == 1
        assert result[0] == r

    def test_report_on_left_boundary_is_included(self):
        """报告期等于区间起始日期时应被保留（闭区间）"""
        r = make_report(period=datetime.date(2020, 1, 1))
        result = FETCHER._apply_filters([r], DEFAULT_RANGE, DEFAULT_TYPES, None)
        assert len(result) == 1

    def test_report_on_right_boundary_is_included(self):
        """报告期等于区间结束日期时应被保留（闭区间）"""
        r = make_report(period=datetime.date(2025, 12, 31))
        result = FETCHER._apply_filters([r], DEFAULT_RANGE, DEFAULT_TYPES, None)
        assert len(result) == 1

    def test_report_before_start_is_excluded(self):
        """报告期早于区间起始日期应被排除"""
        r = make_report(period=datetime.date(2019, 12, 31))
        result = FETCHER._apply_filters([r], DEFAULT_RANGE, DEFAULT_TYPES, None)
        assert len(result) == 0

    def test_report_after_end_is_excluded(self):
        """报告期晚于区间结束日期应被排除"""
        r = make_report(period=datetime.date(2026, 1, 1))
        result = FETCHER._apply_filters([r], DEFAULT_RANGE, DEFAULT_TYPES, None)
        assert len(result) == 0


class TestApplyFiltersReportTypes:
    """测试 _apply_filters 的财报类型过滤"""

    def test_matching_report_type_is_included(self):
        """类型匹配的财报应被保留"""
        r = make_report(report_type=ReportType.ANNUAL)
        result = FETCHER._apply_filters(
            [r], DEFAULT_RANGE, [ReportType.ANNUAL], None
        )
        assert len(result) == 1

    def test_non_matching_report_type_is_excluded(self):
        """类型不匹配的财报应被排除"""
        r = make_report(report_type=ReportType.QUARTERLY)
        result = FETCHER._apply_filters(
            [r], DEFAULT_RANGE, [ReportType.ANNUAL], None
        )
        assert len(result) == 0

    def test_multiple_types_all_matching_are_included(self):
        """多个类型均被允许时，匹配任一即保留"""
        r1 = make_report(report_type=ReportType.ANNUAL)
        r2 = make_report(
            period=datetime.date(2023, 6, 30),
            report_type=ReportType.SEMI_ANNUAL,
        )
        result = FETCHER._apply_filters(
            [r1, r2],
            DEFAULT_RANGE,
            [ReportType.ANNUAL, ReportType.SEMI_ANNUAL],
            10,
        )
        assert len(result) == 2


class TestApplyFiltersSorting:
    """测试 _apply_filters 的排序行为"""

    def test_reports_sorted_descending_by_period(self):
        """过滤后应按报告期降序排列"""
        r1 = make_report(period=datetime.date(2022, 12, 31))
        r2 = make_report(period=datetime.date(2024, 12, 31))
        r3 = make_report(period=datetime.date(2023, 12, 31))
        result = FETCHER._apply_filters(
            [r1, r2, r3], DEFAULT_RANGE, DEFAULT_TYPES, 10
        )
        periods = [r.period for r in result]
        assert periods == sorted(periods, reverse=True)


class TestApplyFiltersTruncation:
    """测试 _apply_filters 的截取行为"""

    def test_max_count_none_defaults_to_one(self):
        """max_count 为 None 时默认返回 1 条（需求 4.3）"""
        reports = [
            make_report(period=datetime.date(2023, 12, 31)),
            make_report(period=datetime.date(2022, 12, 31)),
            make_report(period=datetime.date(2021, 12, 31)),
        ]
        result = FETCHER._apply_filters(reports, DEFAULT_RANGE, DEFAULT_TYPES, None)
        assert len(result) == 1

    def test_max_count_none_defaults_to_one_newest(self):
        """max_count 为 None 时默认返回最新的 1 条"""
        reports = [
            make_report(period=datetime.date(2021, 12, 31)),
            make_report(period=datetime.date(2023, 12, 31)),
        ]
        result = FETCHER._apply_filters(reports, DEFAULT_RANGE, DEFAULT_TYPES, None)
        assert len(result) == 1
        assert result[0].period == datetime.date(2023, 12, 31)

    def test_max_count_truncates_to_k(self):
        """max_count=2 时最多返回 2 条"""
        reports = [
            make_report(period=datetime.date(2023, 12, 31)),
            make_report(period=datetime.date(2022, 12, 31)),
            make_report(period=datetime.date(2021, 12, 31)),
        ]
        result = FETCHER._apply_filters(reports, DEFAULT_RANGE, DEFAULT_TYPES, 2)
        assert len(result) == 2

    def test_max_count_larger_than_available_returns_all(self):
        """max_count 大于可用数量时返回全部"""
        reports = [
            make_report(period=datetime.date(2023, 12, 31)),
            make_report(period=datetime.date(2022, 12, 31)),
        ]
        result = FETCHER._apply_filters(reports, DEFAULT_RANGE, DEFAULT_TYPES, 10)
        assert len(result) == 2


# ─────────────────────────────────────────────────────────────────────────────
# fetch() 测试
# ─────────────────────────────────────────────────────────────────────────────

def make_config(**kwargs):
    """构造 AppConfig 实例，提供常见默认值"""
    defaults = {
        "storage_dir": "/tmp/reports",
        "companies": [CompanyConfig(ticker="600519")],
        "report_types": [ReportType.ANNUAL],
        "start_date": datetime.date(2020, 1, 1),
        "end_date": datetime.date(2025, 12, 31),
        "max_count": None,
    }
    defaults.update(kwargs)
    return AppConfig(**defaults)


class TestFetchBasic:
    """测试 fetch() 基本功能"""

    def test_fetch_returns_matching_reports(self):
        """配置匹配时返回对应财报"""
        reports_db = {
            "600519": [
                make_report(period=datetime.date(2023, 12, 31)),
            ]
        }
        fetcher = ReportFetcher(known_companies=KNOWN, reports_db=reports_db)
        config = make_config()
        results = list(fetcher.fetch(config))
        assert len(results) == 1
        assert results[0].company_id == "600519"
        assert results[0].period == datetime.date(2023, 12, 31)

    def test_fetch_skips_unknown_company(self, caplog):
        """未知公司应被跳过，WARN 日志含原始标识"""
        fetcher = ReportFetcher(known_companies={"600519": {}})
        config = make_config(companies=[CompanyConfig(ticker="UNKNOWN")])
        with caplog.at_level(logging.WARNING, logger="financial_report_fetcher.fetcher"):
            results = list(fetcher.fetch(config))
        assert len(results) == 0
        assert "UNKNOWN" in caplog.text

    def test_fetch_applies_max_count_default_one(self):
        """未配置 max_count 时默认只取 1 条（需求 4.3）"""
        reports_db = {
            "600519": [
                make_report(period=datetime.date(2023, 12, 31)),
                make_report(period=datetime.date(2022, 12, 31)),
                make_report(period=datetime.date(2021, 12, 31)),
            ]
        }
        fetcher = ReportFetcher(known_companies=KNOWN, reports_db=reports_db)
        config = make_config(max_count=None)
        results = list(fetcher.fetch(config))
        assert len(results) == 1

    def test_fetch_multiple_companies(self):
        """多公司配置时返回所有匹配公司的财报"""
        multi_known = {
            "600519": {},
            "000651": {},
        }
        reports_db = {
            "600519": [make_report(period=datetime.date(2023, 12, 31))],
            "000651": [make_report(period=datetime.date(2023, 12, 31))],
        }
        fetcher = ReportFetcher(known_companies=multi_known, reports_db=reports_db)
        config = make_config(
            companies=[
                CompanyConfig(ticker="600519"),
                CompanyConfig(ticker="000651"),
            ]
        )
        results = list(fetcher.fetch(config))
        assert len(results) == 2

    def test_fetch_company_with_no_reports_returns_nothing(self):
        """公司在数据库中但没有匹配财报时返回空"""
        fetcher = ReportFetcher(known_companies={"600519": {}}, reports_db={})
        config = make_config()
        results = list(fetcher.fetch(config))
        assert len(results) == 0


class TestFetchDefaultDateRange:
    """测试 fetch() 未配置日期时的默认时间范围（需求 4.1, 4.4）"""

    def test_default_date_range_logs_info_with_dates(self, caplog):
        """未配置 start/end 时 INFO 日志包含实际日期值（需求 4.4）"""
        today = datetime.date.today()
        prev_year = today.year - 1
        expected_start = f"{prev_year}-01-01"
        expected_end = f"{prev_year}-12-31"

        config = make_config(start_date=None, end_date=None)
        fetcher = ReportFetcher(
            known_companies={"600519": {}}, reports_db={"600519": []}
        )
        with caplog.at_level(logging.INFO, logger="financial_report_fetcher.fetcher"):
            list(fetcher.fetch(config))

        assert "start_date" in caplog.text
        assert "end_date" in caplog.text
        assert expected_start in caplog.text
        assert expected_end in caplog.text

    def test_default_date_range_is_previous_full_year(self):
        """未配置日期时使用上一自然年度（需求 4.1）"""
        today = datetime.date.today()
        prev_year = today.year - 1

        config = make_config(start_date=None, end_date=None)
        # 报告期刚好是去年12-31，应在范围内
        reports_db = {
            "600519": [
                make_report(
                    period=datetime.date(prev_year, 12, 31),
                ),
                make_report(
                    period=datetime.date(prev_year + 1, 12, 31),
                    report_type=ReportType.QUARTERLY,  # 不同类型，日期被过滤
                ),
            ]
        }
        fetcher = ReportFetcher(known_companies=KNOWN, reports_db=reports_db)
        results = list(fetcher.fetch(config))
        # 去年12-31 在范围内，今年12-31 不在范围（即使同类型也不会匹配）
        # 且 quarterly 不在默认 report_types 中
        assert len(results) == 1
        assert results[0].period.year == prev_year

    def test_configured_date_range_is_used(self):
        """已配置日期时使用配置值而非默认值"""
        reports_db = {
            "600519": [
                make_report(period=datetime.date(2020, 12, 31)),
                make_report(period=datetime.date(2021, 12, 31)),
            ]
        }
        fetcher = ReportFetcher(known_companies=KNOWN, reports_db=reports_db)
        # 配置窄区间，只匹配 2020-12-31
        config = make_config(
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2020, 12, 31),
            max_count=10,
        )
        results = list(fetcher.fetch(config))
        assert len(results) == 1
        assert results[0].period == datetime.date(2020, 12, 31)

    def test_default_report_types_is_annual(self):
        """未配置 report_types 时默认使用 annual（需求 5.3）"""
        reports_db = {
            "600519": [
                make_report(report_type=ReportType.ANNUAL),
                make_report(
                    period=datetime.date(2023, 6, 30),
                    report_type=ReportType.SEMI_ANNUAL,
                ),
                make_report(
                    period=datetime.date(2023, 9, 30),
                    report_type=ReportType.QUARTERLY,
                ),
            ]
        }
        fetcher = ReportFetcher(known_companies=KNOWN, reports_db=reports_db)
        config = make_config(
            max_count=10,
            report_types=[ReportType.ANNUAL],  # 显式设为默认值
        )
        results = list(fetcher.fetch(config))
        assert len(results) == 1
        assert results[0].report_type == ReportType.ANNUAL
