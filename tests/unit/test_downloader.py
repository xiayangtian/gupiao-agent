"""
tests/unit/test_downloader.py

ReportDownloader 单元测试
"""

from datetime import date

import pytest

from financial_report_fetcher.downloader import ReportDownloader
from financial_report_fetcher.models import ReportMeta, ReportType


def _make_report(
    company_id: str,
    report_type: ReportType,
    period: date,
    company_name: str = "",
) -> ReportMeta:
    """辅助函数：创建一个测试用的 ReportMeta 对象"""
    return ReportMeta(
        company_id=company_id,
        company_name=company_name,
        report_type=report_type,
        period=period,
        download_url="http://example.com/report.pdf",
        title="测试财报",
    )


class TestBuildFilename:
    """测试 ReportDownloader.build_filename() 方法"""

    # ── 三种财报类型的基本格式测试 ──────────────────────────────────────────

    def test_annual_report_filename(self):
        """年报文件名应包含中文"年报"，公司名称在最前，格式正确"""
        report = _make_report(
            "600519", ReportType.ANNUAL, date(2023, 12, 31), company_name="贵州茅台"
        )
        assert ReportDownloader.build_filename(report) == "贵州茅台_600519_年报_2023.pdf"

    def test_semi_annual_report_filename(self):
        """半年报文件名应包含中文"半年报"，格式正确"""
        report = _make_report(
            "000002", ReportType.SEMI_ANNUAL, date(2022, 6, 30), company_name="万科A"
        )
        assert ReportDownloader.build_filename(report) == "万科A_000002_半年报_2022.pdf"

    def test_quarterly_report_filename(self):
        """季报文件名应包含中文"季报"，格式正确"""
        report = _make_report(
            "BYD", ReportType.QUARTERLY, date(2021, 9, 30), company_name="比亚迪"
        )
        assert ReportDownloader.build_filename(report) == "比亚迪_BYD_季报_2021.pdf"

    # ── 文件名格式约束 ───────────────────────────────────────────────────────

    def test_filename_ends_with_pdf(self):
        """生成的文件名必须以 .pdf 结尾"""
        report = _make_report("600519", ReportType.ANNUAL, date(2023, 12, 31))
        assert ReportDownloader.build_filename(report).endswith(".pdf")

    def test_filename_has_three_parts_separated_by_underscore(self):
        """未提供公司名时，文件名去掉 .pdf 后缀后应由下划线分隔的三部分组成"""
        report = _make_report("600519", ReportType.ANNUAL, date(2023, 12, 31))
        filename = ReportDownloader.build_filename(report)
        name_without_ext = filename[:-4]  # 去掉 ".pdf"
        parts = name_without_ext.split("_")
        assert len(parts) == 3

    def test_company_name_at_front(self):
        """提供公司名时，公司名应位于文件名最前"""
        report = _make_report(
            "600900", ReportType.ANNUAL, date(2025, 12, 31), company_name="长江电力"
        )
        filename = ReportDownloader.build_filename(report)
        name_without_ext = filename[:-4]
        parts = name_without_ext.split("_")
        assert parts[0] == "长江电力"
        assert parts[1] == "600900"
        assert len(parts) == 4

    def test_filename_uses_year_not_full_date(self):
        """period 字段只取年份，不含月和日"""
        # 报告期为 6 月 30 日，文件名中只应出现年份 2022
        report = _make_report("000001", ReportType.SEMI_ANNUAL, date(2022, 6, 30))
        filename = ReportDownloader.build_filename(report)
        assert "2022" in filename
        assert "06" not in filename
        assert "30" not in filename

    def test_filename_contains_company_id(self):
        """文件名应包含公司标识"""
        report = _make_report("TSLA", ReportType.ANNUAL, date(2020, 12, 31))
        assert "TSLA" in ReportDownloader.build_filename(report)

    # ── 边界情况 ─────────────────────────────────────────────────────────────

    def test_filename_with_name_as_company_id(self):
        """公司标识为公司名称（而非 ticker）时，文件名应正确生成"""
        report = _make_report("贵州茅台", ReportType.ANNUAL, date(2023, 12, 31))
        assert ReportDownloader.build_filename(report) == "贵州茅台_年报_2023.pdf"

    def test_filename_format_matches_example_in_spec(self):
        """公司名 + ticker + 类型 + 年份：长江电力_600900_年报_2025.pdf"""
        report = _make_report(
            "600900", ReportType.ANNUAL, date(2025, 12, 31), company_name="长江电力"
        )
        assert ReportDownloader.build_filename(report) == "长江电力_600900_年报_2025.pdf"


# ─────────────────────────────────────────────────────────────────────────────
# download_one 单元测试
# ─────────────────────────────────────────────────────────────────────────────

import os
import requests
from unittest.mock import patch, MagicMock
from tenacity import wait_none

from financial_report_fetcher.models import DownloadStatus


def _make_mock_response(content: bytes, status_code: int = 200) -> MagicMock:
    """创建模拟的 requests.Response 对象"""
    mock_resp = MagicMock()
    mock_resp.content = content
    mock_resp.status_code = status_code
    mock_resp.raise_for_status.return_value = None
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP Error {status_code}"
        )
    return mock_resp


class TestDownloadOne:
    """测试 ReportDownloader.download_one() 方法"""

    def _report(self):
        return _make_report("600519", ReportType.ANNUAL, date(2023, 12, 31))

    # ── 正常下载 ──────────────────────────────────────────────────────────────

    def test_download_success(self, tmp_path):
        """正常下载：返回 SUCCESS，文件被写入磁盘"""
        report = self._report()
        fake_content = b"%PDF-1.4 fake content"

        with patch(
            "financial_report_fetcher.downloader._do_download",
            return_value=fake_content,
        ):
            downloader = ReportDownloader()
            status = downloader.download_one(report, str(tmp_path))

        assert status == DownloadStatus.SUCCESS
        expected_file = tmp_path / "600519_年报_2023.pdf"
        assert expected_file.exists()
        assert expected_file.read_bytes() == fake_content

    # ── 目录自动创建（需求 2.3）───────────────────────────────────────────────

    def test_creates_target_directory_if_not_exists(self, tmp_path):
        """目标目录不存在时应自动创建（需求 2.3）"""
        report = self._report()
        nested_dir = str(tmp_path / "deep" / "nested" / "dir")

        with patch(
            "financial_report_fetcher.downloader._do_download",
            return_value=b"%PDF-1.4 fake",
        ):
            downloader = ReportDownloader()
            status = downloader.download_one(report, nested_dir)

        assert status == DownloadStatus.SUCCESS
        assert os.path.isdir(nested_dir)

    # ── 文件已存在返回 SKIPPED（需求 2.4）────────────────────────────────────

    def test_skip_if_file_already_exists(self, tmp_path):
        """已存在的有效 PDF 不重新下载，返回 SKIPPED（需求 2.4）"""
        report = self._report()
        existing_content = b"%PDF-1.4 original content"
        # 预先写入同名文件
        existing_file = tmp_path / "600519_年报_2023.pdf"
        existing_file.write_bytes(existing_content)

        with patch(
            "financial_report_fetcher.downloader._do_download",
        ) as mock_dl:
            downloader = ReportDownloader()
            status = downloader.download_one(report, str(tmp_path))
            # _do_download 不应被调用
            mock_dl.assert_not_called()

        assert status == DownloadStatus.SKIPPED
        # 文件内容不应被修改
        assert existing_file.read_bytes() == existing_content

    def test_existing_invalid_pdf_is_replaced(self, tmp_path):
        """已存在的部分/损坏文件应重新下载并原子替换。"""
        report = self._report()
        target = tmp_path / "600519_年报_2023.pdf"
        target.write_bytes(b"partial download")
        valid_pdf = b"%PDF-1.7\ncomplete"

        with patch(
            "financial_report_fetcher.downloader._do_download",
            return_value=valid_pdf,
        ) as mock_dl:
            status = ReportDownloader().download_one(report, str(tmp_path))

        assert status == DownloadStatus.SUCCESS
        mock_dl.assert_called_once()
        assert target.read_bytes() == valid_pdf

    def test_invalid_download_leaves_no_partial_files(self, tmp_path):
        """非 PDF 响应返回 FAILED，不留最终文件或临时文件。"""
        report = self._report()

        with patch(
            "financial_report_fetcher.downloader._do_download",
            return_value=b"<html>upstream error</html>",
        ):
            status = ReportDownloader().download_one(report, str(tmp_path))

        assert status == DownloadStatus.FAILED
        assert not (tmp_path / "600519_年报_2023.pdf").exists()
        assert list(tmp_path.glob("*.tmp")) == []

    # ── 超时场景 ──────────────────────────────────────────────────────────────

    def test_timeout_returns_failed(self, tmp_path):
        """请求超时时返回 FAILED"""
        report = self._report()

        with patch(
            "financial_report_fetcher.downloader._do_download",
            side_effect=requests.Timeout("连接超时"),
        ):
            downloader = ReportDownloader()
            status = downloader.download_one(report, str(tmp_path))

        assert status == DownloadStatus.FAILED
        # 超时后不应产生残留文件
        assert not (tmp_path / "600519_年报_2023.pdf").exists()

    # ── 空文件场景（需求 2.8）─────────────────────────────────────────────────

    def test_empty_content_returns_failed(self, tmp_path):
        """下载内容为 0 字节时返回 FAILED（需求 2.8）"""
        report = self._report()

        with patch(
            "financial_report_fetcher.downloader._do_download",
            return_value=b"",
        ):
            downloader = ReportDownloader()
            status = downloader.download_one(report, str(tmp_path))

        assert status == DownloadStatus.FAILED
        # 零字节文件不应被保留在磁盘上
        assert not (tmp_path / "600519_年报_2023.pdf").exists()

    # ── HTTP 错误场景 ─────────────────────────────────────────────────────────

    def test_http_error_returns_failed(self, tmp_path):
        """HTTP 4xx/5xx 错误时返回 FAILED"""
        report = self._report()

        with patch(
            "financial_report_fetcher.downloader._do_download",
            side_effect=requests.HTTPError("404 Not Found"),
        ):
            downloader = ReportDownloader()
            status = downloader.download_one(report, str(tmp_path))

        assert status == DownloadStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# download_all 单元测试
# ─────────────────────────────────────────────────────────────────────────────


class TestDownloadAll:
    """测试 ReportDownloader.download_all() 方法"""

    def _reports(self):
        """生成 3 份不同公司的财报用于测试"""
        return [
            _make_report("600519", ReportType.ANNUAL, date(2023, 12, 31)),
            _make_report("000002", ReportType.SEMI_ANNUAL, date(2022, 6, 30)),
            _make_report("BYD", ReportType.QUARTERLY, date(2021, 9, 30)),
        ]

    def test_all_success(self, tmp_path):
        """全部下载成功时汇总计数正确"""
        reports = self._reports()

        with patch(
            "financial_report_fetcher.downloader._do_download",
            return_value=b"%PDF-1.4 fake content",
        ):
            downloader = ReportDownloader()
            summary = downloader.download_all(iter(reports), str(tmp_path))

        assert summary.success == 3
        assert summary.skipped == 0
        assert summary.failed == 0
        assert summary.total == 3

    def test_single_failure_does_not_interrupt_flow(self, tmp_path):
        """单个文件失败不应中断整体下载流程（需求 2.6）"""
        reports = self._reports()  # 3 份财报

        # 第 2 次调用抛出异常，第 1、3 次正常返回
        side_effects = [
            b"%PDF-1.4 first",
            requests.ConnectionError("网络错误"),
            b"%PDF-1.4 third",
        ]

        with patch(
            "financial_report_fetcher.downloader._do_download",
            side_effect=side_effects,
        ):
            downloader = ReportDownloader()
            summary = downloader.download_all(iter(reports), str(tmp_path))

        assert summary.success == 2
        assert summary.failed == 1
        assert summary.total == 3

    def test_all_skipped_when_files_exist(self, tmp_path):
        """所有文件已存在时，全部返回 SKIPPED，汇总正确"""
        reports = self._reports()

        # 预先写入所有文件
        downloader = ReportDownloader()
        for r in reports:
            (tmp_path / downloader.build_filename(r)).write_bytes(b"%PDF-1.4 existing")

        with patch("financial_report_fetcher.downloader._do_download") as mock_dl:
            summary = downloader.download_all(iter(reports), str(tmp_path))
            mock_dl.assert_not_called()

        assert summary.skipped == 3
        assert summary.success == 0
        assert summary.failed == 0
        assert summary.total == 3

    def test_mixed_results(self, tmp_path):
        """混合场景：成功 + 跳过 + 失败，汇总计数正确"""
        reports = self._reports()
        downloader = ReportDownloader()

        # 预先写入第一份文件（触发 SKIPPED）
        (tmp_path / downloader.build_filename(reports[0])).write_bytes(b"%PDF-1.4 existing")

        # 第二份正常下载（SUCCESS），第三份失败（FAILED）
        side_effects = [
            b"%PDF-1.4 second",
            requests.Timeout("超时"),
        ]

        with patch(
            "financial_report_fetcher.downloader._do_download",
            side_effect=side_effects,
        ):
            summary = downloader.download_all(iter(reports), str(tmp_path))

        assert summary.skipped == 1
        assert summary.success == 1
        assert summary.failed == 1
        assert summary.total == 3

    def test_empty_reports_returns_zero_summary(self, tmp_path):
        """空报告列表时，返回全零汇总"""
        downloader = ReportDownloader()
        summary = downloader.download_all(iter([]), str(tmp_path))

        assert summary.success == 0
        assert summary.skipped == 0
        assert summary.failed == 0
        assert summary.total == 0
