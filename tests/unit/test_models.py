"""
models.py 单元测试

覆盖枚举、Pydantic 配置模型和运行时数据模型的各项行为。
"""

import pytest
from datetime import date

from financial_report_fetcher.models import (
    ReportType,
    DownloadStatus,
    CompanyConfig,
    AppConfig,
    DateRange,
    ReportMeta,
    DownloadSummary,
)


# ─────────────────────────────────────────────────────────────────────────────
# ReportType 枚举
# ─────────────────────────────────────────────────────────────────────────────

class TestReportType:
    def test_annual_value(self):
        assert ReportType.ANNUAL == "annual"

    def test_semi_annual_value(self):
        assert ReportType.SEMI_ANNUAL == "semi_annual"

    def test_quarterly_value(self):
        assert ReportType.QUARTERLY == "quarterly"

    def test_is_str_subclass(self):
        # ReportType 继承自 str，可直接用于字符串比较
        assert isinstance(ReportType.ANNUAL, str)


# ─────────────────────────────────────────────────────────────────────────────
# DownloadStatus 枚举
# ─────────────────────────────────────────────────────────────────────────────

class TestDownloadStatus:
    def test_success_value(self):
        assert DownloadStatus.SUCCESS == "success"

    def test_skipped_value(self):
        assert DownloadStatus.SKIPPED == "skipped"

    def test_failed_value(self):
        assert DownloadStatus.FAILED == "failed"


# ─────────────────────────────────────────────────────────────────────────────
# CompanyConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestCompanyConfig:
    def test_ticker_only(self):
        """仅提供 ticker 应当通过校验"""
        c = CompanyConfig(ticker="600519")
        assert c.ticker == "600519"
        assert c.name is None

    def test_name_only(self):
        """仅提供 name 应当通过校验"""
        c = CompanyConfig(name="贵州茅台")
        assert c.name == "贵州茅台"
        assert c.ticker is None

    def test_both_provided(self):
        """同时提供 ticker 和 name 应当通过校验"""
        c = CompanyConfig(ticker="600519", name="贵州茅台")
        assert c.ticker == "600519"
        assert c.name == "贵州茅台"

    def test_neither_raises(self):
        """ticker 和 name 均未提供时应抛出 ValueError"""
        with pytest.raises(Exception) as exc_info:
            CompanyConfig()
        assert "ticker" in str(exc_info.value) or "name" in str(exc_info.value)

    def test_error_message_content(self):
        """错误信息应包含可理解的提示"""
        with pytest.raises(Exception) as exc_info:
            CompanyConfig()
        assert "每个公司必须提供 ticker 或 name" in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────────────
# AppConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestAppConfigCompaniesCount:
    """validate_companies_count 校验器测试"""

    _company = CompanyConfig(ticker="600519")

    def test_empty_companies_raises(self):
        with pytest.raises(Exception) as exc_info:
            AppConfig(storage_dir="./r", companies=[])
        assert "companies" in str(exc_info.value) or "1 至 50" in str(exc_info.value)

    def test_one_company_ok(self):
        cfg = AppConfig(storage_dir="./r", companies=[self._company])
        assert len(cfg.companies) == 1

    def test_fifty_companies_ok(self):
        companies = [CompanyConfig(ticker=str(i)) for i in range(1, 51)]
        cfg = AppConfig(storage_dir="./r", companies=companies)
        assert len(cfg.companies) == 50

    def test_fifty_one_companies_raises(self):
        companies = [CompanyConfig(ticker=str(i)) for i in range(1, 52)]
        with pytest.raises(Exception) as exc_info:
            AppConfig(storage_dir="./r", companies=companies)
        assert "companies" in str(exc_info.value) or "50" in str(exc_info.value)


class TestAppConfigMaxCount:
    """validate_max_count 校验器测试"""

    _company = CompanyConfig(ticker="600519")

    def test_none_is_ok(self):
        cfg = AppConfig(storage_dir="./r", companies=[self._company], max_count=None)
        assert cfg.max_count is None

    def test_boundary_one(self):
        cfg = AppConfig(storage_dir="./r", companies=[self._company], max_count=1)
        assert cfg.max_count == 1

    def test_boundary_ten_thousand(self):
        cfg = AppConfig(storage_dir="./r", companies=[self._company], max_count=10000)
        assert cfg.max_count == 10000

    def test_zero_raises(self):
        with pytest.raises(Exception) as exc_info:
            AppConfig(storage_dir="./r", companies=[self._company], max_count=0)
        assert "max_count" in str(exc_info.value) or "10000" in str(exc_info.value)

    def test_ten_thousand_one_raises(self):
        with pytest.raises(Exception) as exc_info:
            AppConfig(storage_dir="./r", companies=[self._company], max_count=10001)
        assert "max_count" in str(exc_info.value) or "10000" in str(exc_info.value)

    def test_negative_raises(self):
        with pytest.raises(Exception):
            AppConfig(storage_dir="./r", companies=[self._company], max_count=-1)


class TestAppConfigDateRange:
    """validate_date_range 校验器测试"""

    _company = CompanyConfig(ticker="600519")

    def test_both_none_is_ok(self):
        cfg = AppConfig(storage_dir="./r", companies=[self._company])
        assert cfg.start_date is None
        assert cfg.end_date is None

    def test_both_provided_ok(self):
        cfg = AppConfig(
            storage_dir="./r",
            companies=[self._company],
            start_date=date(2023, 1, 1),
            end_date=date(2023, 12, 31),
        )
        assert cfg.start_date == date(2023, 1, 1)
        assert cfg.end_date == date(2023, 12, 31)

    def test_start_equals_end_ok(self):
        """起止日期相同（单日区间）应合法"""
        cfg = AppConfig(
            storage_dir="./r",
            companies=[self._company],
            start_date=date(2023, 6, 30),
            end_date=date(2023, 6, 30),
        )
        assert cfg.start_date == cfg.end_date

    def test_only_start_raises(self):
        with pytest.raises(Exception) as exc_info:
            AppConfig(
                storage_dir="./r",
                companies=[self._company],
                start_date=date(2023, 1, 1),
            )
        assert "end_date" in str(exc_info.value)

    def test_only_end_raises(self):
        with pytest.raises(Exception) as exc_info:
            AppConfig(
                storage_dir="./r",
                companies=[self._company],
                end_date=date(2023, 12, 31),
            )
        assert "start_date" in str(exc_info.value)

    def test_start_after_end_raises(self):
        with pytest.raises(Exception) as exc_info:
            AppConfig(
                storage_dir="./r",
                companies=[self._company],
                start_date=date(2023, 12, 31),
                end_date=date(2023, 1, 1),
            )
        assert "start_date" in str(exc_info.value)

    def test_default_report_types_is_annual(self):
        """未指定 report_types 时默认应为 [ANNUAL]"""
        cfg = AppConfig(storage_dir="./r", companies=[self._company])
        assert cfg.report_types == [ReportType.ANNUAL]


# ─────────────────────────────────────────────────────────────────────────────
# DateRange
# ─────────────────────────────────────────────────────────────────────────────

class TestDateRange:
    _dr = DateRange(start=date(2023, 1, 1), end=date(2023, 12, 31))

    def test_contains_left_boundary(self):
        assert self._dr.contains(date(2023, 1, 1)) is True

    def test_contains_right_boundary(self):
        assert self._dr.contains(date(2023, 12, 31)) is True

    def test_contains_midpoint(self):
        assert self._dr.contains(date(2023, 6, 15)) is True

    def test_not_contains_before_start(self):
        assert self._dr.contains(date(2022, 12, 31)) is False

    def test_not_contains_after_end(self):
        assert self._dr.contains(date(2024, 1, 1)) is False

    def test_single_day_range(self):
        single = DateRange(start=date(2023, 6, 30), end=date(2023, 6, 30))
        assert single.contains(date(2023, 6, 30)) is True
        assert single.contains(date(2023, 6, 29)) is False
        assert single.contains(date(2023, 7, 1)) is False


# ─────────────────────────────────────────────────────────────────────────────
# ReportMeta
# ─────────────────────────────────────────────────────────────────────────────

class TestReportMeta:
    def test_create_with_all_fields(self):
        rm = ReportMeta(
            company_id="600519",
            report_type=ReportType.ANNUAL,
            period=date(2023, 12, 31),
            download_url="https://example.com/report.pdf",
            title="贵州茅台2023年年度报告",
        )
        assert rm.company_id == "600519"
        assert rm.report_type == ReportType.ANNUAL
        assert rm.period == date(2023, 12, 31)
        assert rm.download_url == "https://example.com/report.pdf"
        assert rm.title == "贵州茅台2023年年度报告"

    def test_company_id_can_be_name(self):
        """company_id 可以是公司名称（当 ticker 不存在时）"""
        rm = ReportMeta(
            company_id="比亚迪",
            report_type=ReportType.SEMI_ANNUAL,
            period=date(2023, 6, 30),
            download_url="https://example.com/byd.pdf",
            title="比亚迪2023年半年度报告",
        )
        assert rm.company_id == "比亚迪"


# ─────────────────────────────────────────────────────────────────────────────
# DownloadSummary
# ─────────────────────────────────────────────────────────────────────────────

class TestDownloadSummary:
    def test_default_all_zero(self):
        ds = DownloadSummary()
        assert ds.success == 0
        assert ds.skipped == 0
        assert ds.failed == 0
        assert ds.total == 0

    def test_total_sums_all_fields(self):
        ds = DownloadSummary(success=3, skipped=2, failed=1)
        assert ds.total == 6

    def test_total_only_success(self):
        ds = DownloadSummary(success=5)
        assert ds.total == 5

    def test_total_only_skipped(self):
        ds = DownloadSummary(skipped=4)
        assert ds.total == 4

    def test_total_only_failed(self):
        ds = DownloadSummary(failed=7)
        assert ds.total == 7

    def test_total_is_property_not_stored(self):
        """total 是只读属性，不应被直接赋值"""
        ds = DownloadSummary(success=1, skipped=1, failed=1)
        with pytest.raises(AttributeError):
            ds.total = 999
