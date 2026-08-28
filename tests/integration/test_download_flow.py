"""Integration tests for the report download flow."""

from datetime import date
import logging

from financial_report_fetcher.downloader import ReportDownloader
from financial_report_fetcher.models import DownloadStatus, ReportMeta, ReportType


class _MockResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


def _make_report(
    company_id: str,
    report_type: ReportType,
    period: date,
    download_url: str,
    title: str,
) -> ReportMeta:
    return ReportMeta(
        company_id=company_id,
        report_type=report_type,
        period=period,
        download_url=download_url,
        title=title,
    )


def test_download_all_creates_missing_directory_and_writes_file(monkeypatch, tmp_path):
    downloader = ReportDownloader()
    storage_dir = tmp_path / "reports"
    report = _make_report(
        company_id="600519",
        report_type=ReportType.ANNUAL,
        period=date(2023, 12, 31),
        download_url="https://example.test/annual.pdf",
        title="annual report",
    )
    payload = b"%PDF-1.4\nintegration-test\n"

    def fake_get(url: str, timeout: int):
        assert url == report.download_url
        assert timeout == downloader.TIMEOUT_SECONDS
        return _MockResponse(payload)

    monkeypatch.setattr("financial_report_fetcher.downloader.requests.get", fake_get)

    summary = downloader.download_all([report], str(storage_dir))

    expected_path = storage_dir / downloader.build_filename(report)
    assert storage_dir.is_dir()
    assert expected_path.read_bytes() == payload
    assert summary.success == 1
    assert summary.skipped == 0
    assert summary.failed == 0
    assert summary.total == 1
    assert DownloadStatus.SUCCESS.value == "success"


def test_download_all_logs_summary_for_success_and_skipped(monkeypatch, tmp_path, caplog):
    downloader = ReportDownloader()
    storage_dir = tmp_path / "reports"
    storage_dir.mkdir()

    success_report = _make_report(
        company_id="600000",
        report_type=ReportType.SEMI_ANNUAL,
        period=date(2024, 6, 30),
        download_url="https://example.test/half-year.pdf",
        title="half year report",
    )
    skipped_report = _make_report(
        company_id="600001",
        report_type=ReportType.QUARTERLY,
        period=date(2024, 3, 31),
        download_url="https://example.test/quarterly.pdf",
        title="quarterly report",
    )

    downloaded_payload = b"%PDF-1.4 downloaded report contents\n"
    skipped_path = storage_dir / downloader.build_filename(skipped_report)
    skipped_path.write_bytes(b"%PDF-1.4 already on disk\n")

    def fake_get(url: str, timeout: int):
        assert url == success_report.download_url
        assert timeout == downloader.TIMEOUT_SECONDS
        return _MockResponse(downloaded_payload)

    monkeypatch.setattr("financial_report_fetcher.downloader.requests.get", fake_get)
    caplog.set_level(logging.INFO, logger="financial_report_fetcher.downloader")

    summary = downloader.download_all([success_report, skipped_report], str(storage_dir))

    success_path = storage_dir / downloader.build_filename(success_report)
    assert success_path.read_bytes() == downloaded_payload
    assert skipped_path.read_bytes() == b"%PDF-1.4 already on disk\n"
    assert summary.success == 1
    assert summary.skipped == 1
    assert summary.failed == 0
    assert summary.total == 2

    summary_messages = [
        record.message
        for record in caplog.records
        if record.name == "financial_report_fetcher.downloader"
        and record.message.startswith("下载完成：")
    ]
    assert summary_messages == ["下载完成：成功 1 份，跳过 1 份，失败 0 份，共 2 份"]
