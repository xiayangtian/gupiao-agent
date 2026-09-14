from datetime import date
from pathlib import Path

import pytest

from financial_report_fetcher.downloader import ReportDownloader
from financial_report_fetcher.models import DownloadStatus, ReportMeta, ReportType
from webapp.chat_models import Scope
from webapp.chat_supplement import (
    SupplementCandidate,
    SupplementCandidateResolver,
    SupplementExecutor,
    SupplementRegistry,
    SupplementRequest,
)


def _company_scope():
    return Scope.company_only(
        "601288", "农业银行", ["601288:2025-12-31:annual"]
    )


def _candidates(count=5):
    return [
        SupplementCandidate(
            id=f"c{index}",
            report_id=f"601288:202{index}-12-31:annual",
            code="601288",
            company="农业银行",
            period=f"202{index}-12-31",
            report_type="annual",
            source="巨潮资讯",
        )
        for index in range(1, count + 1)
    ]


def _registry_with_request():
    registry = SupplementRegistry(
        clock=lambda: "2026-09-14T10:00:00",
        id_factory=lambda: "r1",
    )
    registry.create(
        session_id="s1",
        question="营收变化",
        scope=_company_scope(),
        candidates=_candidates(2),
    )
    return registry


def test_approve_rejects_cross_session_replay_and_more_than_five_candidates():
    registry = SupplementRegistry(
        clock=lambda: "2026-09-14T10:00:00",
        id_factory=lambda: "r1",
    )
    request = registry.create(
        session_id="s1",
        question="营收变化",
        scope=_company_scope(),
        candidates=_candidates(5),
    )

    with pytest.raises(PermissionError):
        registry.approve(request.id, session_id="other", candidate_ids=["c1"])
    with pytest.raises(ValueError, match="最多 5"):
        registry.approve(
            request.id,
            session_id="s1",
            candidate_ids=["c1", "c2", "c3", "c4", "c5", "c6"],
        )


def test_approved_request_cannot_be_consumed_twice():
    registry = _registry_with_request()
    request = registry.approve("r1", "s1", ["c1"])
    assert request.status == "approved"
    assert request.consumed_at == "2026-09-14T10:00:00"

    registry.mark_consumed("r1")
    with pytest.raises(ValueError, match="已消费"):
        registry.approve("r1", "s1", ["c1"])


def test_request_round_trip_omits_url_and_path_fields():
    request = _registry_with_request().get("r1")
    assert request is not None

    encoded = request.to_dict()
    assert set(encoded["candidates"][0]) == {
        "id", "report_id", "code", "company", "period", "report_type", "source",
    }
    restored = SupplementRequest.from_dict(encoded)
    assert restored == request


def test_create_requires_unique_candidates_within_the_five_report_budget():
    registry = SupplementRegistry(clock=lambda: "2026-09-14T10:00:00")
    duplicate = _candidates(2)
    duplicate[1] = duplicate[0]

    with pytest.raises(ValueError, match="候选 ID 必须唯一"):
        registry.create("s1", "问题", _company_scope(), duplicate)
    with pytest.raises(ValueError, match="最多 5"):
        registry.create("s1", "问题", _company_scope(), _candidates(6))


def test_transition_cannot_bypass_approval_or_decline_and_allows_operational_flow():
    registry = _registry_with_request()

    with pytest.raises(ValueError, match="approve|decline"):
        registry.transition("r1", "approved")
    with pytest.raises(ValueError, match="approve|decline"):
        registry.transition("r1", "declined")

    registry.approve("r1", "s1", ["c1"])
    assert registry.transition("r1", "downloading").status == "downloading"
    assert registry.transition("r1", "ingesting").status == "ingesting"
    assert registry.transition("r1", "resuming").status == "resuming"
    assert registry.transition("r1", "completed").status == "completed"


def test_declined_or_expired_request_cannot_be_approved():
    registry = _registry_with_request()
    declined = registry.decline("r1", "s1")
    assert declined.status == "declined"
    with pytest.raises(ValueError, match="不是待授权状态"):
        registry.approve("r1", "s1", ["c1"])

    expired_registry = _registry_with_request()
    expired_registry.transition("r1", "expired")
    with pytest.raises(ValueError, match="已过期"):
        expired_registry.approve("r1", "s1", ["c1"])


def _meta(code, period, report_type, *, url="https://cninfo.example/report.pdf", company="农业银行"):
    return ReportMeta(
        company_id=code,
        company_name=company,
        period=date.fromisoformat(period),
        report_type=ReportType(report_type),
        download_url=url,
        title=f"{company}{period}{report_type}",
    )


def test_resolver_limits_to_scope_company_filters_local_and_sorts_requested_period_first():
    resolver = SupplementCandidateResolver(id_factory=lambda index: f"candidate-{index}")
    reports = [
        _meta("601288", "2025-12-31", "annual"),
        _meta("601288", "2025-06-30", "semi_annual"),
        _meta("601288", "2025-09-30", "quarterly"),
        _meta("601288", "2024-12-31", "annual"),
        _meta("601288", "2024-06-30", "semi_annual"),
        _meta("601288", "2024-09-30", "quarterly"),
        _meta("601288", "2023-12-31", "annual", url=""),
        _meta("600000", "2025-06-30", "semi_annual", company="浦发银行"),
        _meta("601288", "2025-12-31", "annual"),  # 重复 report_id
    ]
    candidates = resolver.resolve(
        _company_scope(),
        [{"period": "2025-06-30", "report_type": "semi_annual"}],
        reports,
        local_pdf_exists=lambda report: report.period == date(2024, 6, 30),
        indexed_report_ids=lambda: {"601288:2025-09-30:quarterly"},
    )

    assert [candidate.report_id for candidate in candidates] == [
        "601288:2025-06-30:semi_annual",
        "601288:2025-12-31:annual",
        "601288:2024-12-31:annual",
        "601288:2024-09-30:quarterly",
    ]
    assert len(candidates) <= 5
    assert all(candidate.code == "601288" for candidate in candidates)
    assert all("url" not in candidate.to_dict() for candidate in candidates)


def test_resolver_rejects_non_company_only_scope():
    resolver = SupplementCandidateResolver()
    with pytest.raises(ValueError, match="company_only"):
        resolver.resolve(
            Scope.whole_corpus(), [], [], lambda report: False, lambda: set()
        )


def test_executor_never_downloads_without_approved_request_and_only_resumes_ingested_ids(tmp_path):
    class FakeDownloader:
        def __init__(self):
            self.calls = []

        def download_one(self, report, storage_dir):
            self.calls.append(report.company_id)
            path = Path(storage_dir) / ReportDownloader.build_filename(report)
            if report.company_id == "601288":
                path.write_bytes(b"%PDF-1.7 valid")
                return DownloadStatus.SUCCESS
            return DownloadStatus.FAILED

    class FakeIngestion:
        def __init__(self):
            self.paths = []

        def auto_ingest_pdf(self, path):
            self.paths.append(path)
            return True

    candidates = (
        SupplementCandidate("candidate-1", "601288:2025-06-30:semi_annual", "601288", "农业银行", "2025-06-30", "semi_annual", "巨潮资讯"),
        SupplementCandidate("candidate-2", "600000:2025-06-30:semi_annual", "600000", "浦发银行", "2025-06-30", "semi_annual", "巨潮资讯"),
    )
    registry = SupplementRegistry(clock=lambda: "2026-09-14T10:00:00", id_factory=lambda: "r2")
    request = registry.create("s1", "营收变化", _company_scope(), candidates)
    reports = {
        "candidate-1": _meta("601288", "2025-06-30", "semi_annual"),
        "candidate-2": _meta("600000", "2025-06-30", "semi_annual", company="浦发银行"),
    }
    downloader = FakeDownloader()
    ingestion = FakeIngestion()
    executor = SupplementExecutor(downloader, ingestion, str(tmp_path), reports)

    with pytest.raises(PermissionError, match="已批准"):
        executor.run(request)
    assert downloader.calls == []

    approved = registry.approve("r2", "s1", ["candidate-1", "candidate-2"])
    outcome = executor.run(approved)

    assert outcome.downloaded_report_ids == ("601288:2025-06-30:semi_annual",)
    assert outcome.ingested_report_ids == ("601288:2025-06-30:semi_annual",)
    assert outcome.failed == ("candidate-2",)
    assert len(ingestion.paths) == 1
    assert "https://" not in str(outcome.to_dict())


def test_resolver_ranks_disclosure_date_and_keeps_exact_ordered_top_five():
    resolver = SupplementCandidateResolver(id_factory=lambda index: f"candidate-{index}")
    reports = [
        _meta("601288", "2025-06-30", "semi_annual"),
        _meta("601288", "2025-12-31", "annual"),
        _meta("601288", "2025-09-30", "quarterly"),
        _meta("601288", "2024-12-31", "annual"),
        _meta("601288", "2024-06-30", "semi_annual"),
        _meta("601288", "2024-09-30", "quarterly"),
        _meta("601288", "2023-12-31", "annual"),
    ]
    reports[1].disclosure_date = date(2026, 3, 31)
    reports[3].disclosure_date = date(2025, 3, 31)
    reports[6].disclosure_date = date(2024, 3, 31)
    candidates = resolver.resolve(
        _company_scope(),
        [{"period": "2025-06-30", "report_type": "semi_annual"}],
        reports,
        lambda report: False,
        lambda: set(),
    )
    assert [candidate.report_id for candidate in candidates] == [
        "601288:2025-06-30:semi_annual",
        "601288:2025-12-31:annual",
        "601288:2024-12-31:annual",
        "601288:2023-12-31:annual",
        "601288:2024-06-30:semi_annual",
    ]


def test_executor_handles_skipped_download_and_safe_failure_reasons(tmp_path):
    class FakeDownloader:
        def download_one(self, report, storage_dir):
            path = Path(storage_dir) / ReportDownloader.build_filename(report)
            if report.period == date(2025, 6, 30):
                path.write_bytes(b"%PDF-1.7 already-present")
                return DownloadStatus.SKIPPED
            return DownloadStatus.FAILED

    class FakeIngestion:
        def __init__(self):
            self.paths = []

        def auto_ingest_pdf(self, path):
            self.paths.append(path)
            return True

    candidates = (
        SupplementCandidate("candidate-1", "601288:2025-06-30:semi_annual", "601288", "农业银行", "2025-06-30", "semi_annual", "巨潮资讯"),
        SupplementCandidate("candidate-2", "601288:2025-12-31:annual", "601288", "农业银行", "2025-12-31", "annual", "巨潮资讯"),
    )
    registry = SupplementRegistry(clock=lambda: "2026-09-14T10:00:00", id_factory=lambda: "r3")
    registry.create("s1", "营收变化", _company_scope(), candidates)
    request = registry.approve("r3", "s1", ["candidate-1", "candidate-2"])
    outcome = SupplementExecutor(
        FakeDownloader(), FakeIngestion(), str(tmp_path),
        {"candidate-1": _meta("601288", "2025-06-30", "semi_annual"),
         "candidate-2": _meta("601288", "2025-12-31", "annual")},
    ).run(request)
    assert outcome.skipped_report_ids == ("601288:2025-06-30:semi_annual",)
    assert outcome.ingested_report_ids == ("601288:2025-06-30:semi_annual",)
    assert outcome.failed == ("candidate-2",)
    assert outcome.failure_reasons == (("candidate-2", "download_failed"),)
    assert "https://" not in str(outcome.to_dict())


def test_executor_does_not_resume_when_ingestion_fails(tmp_path):
    class FakeDownloader:
        def download_one(self, report, storage_dir):
            (Path(storage_dir) / ReportDownloader.build_filename(report)).write_bytes(b"%PDF-1.7 valid")
            return DownloadStatus.SUCCESS

    class FailedIngestion:
        def auto_ingest_pdf(self, path):
            return False

    candidate = SupplementCandidate("candidate-1", "601288:2025-06-30:semi_annual", "601288", "农业银行", "2025-06-30", "semi_annual", "巨潮资讯")
    registry = SupplementRegistry(clock=lambda: "2026-09-14T10:00:00", id_factory=lambda: "r4")
    registry.create("s1", "营收变化", _company_scope(), [candidate])
    request = registry.approve("r4", "s1", ["candidate-1"])
    outcome = SupplementExecutor(
        FakeDownloader(), FailedIngestion(), str(tmp_path),
        {"candidate-1": _meta("601288", "2025-06-30", "semi_annual")},
    ).run(request)
    assert outcome.downloaded_report_ids == ("601288:2025-06-30:semi_annual",)
    assert outcome.ingested_report_ids == ()
    assert outcome.failure_reasons == (("candidate-1", "ingest_failed"),)
