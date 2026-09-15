from datetime import date, datetime

from financial_report_fetcher.datasource import CNINFODatasource, _AnnouncementEntry
from financial_report_fetcher.models import ReportType


def test_cninfo_report_meta_preserves_announcement_date(monkeypatch):
    source = CNINFODatasource()
    source._stock_map = {"601288": ("org-1", "农业银行")}
    source._name_index = {"农业银行": "601288"}
    announcement = _AnnouncementEntry(
        stock_code="601288",
        org_id="org-1",
        title="2024年年度报告",
        announcement_time=datetime(2025, 3, 28, 9, 30),
        adjunct_url="finalpage/2025-03-28/report.pdf",
        adjunct_type="PDF",
    )
    monkeypatch.setattr(source, "_query_announcements", lambda **kwargs: [announcement])

    reports = source.fetch_reports(
        "601288", [ReportType.ANNUAL], date(2024, 1, 1), date(2024, 12, 31)
    )

    assert len(reports) == 1
    assert reports[0].disclosure_date == date(2025, 3, 28)
