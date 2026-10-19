from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from webapp.search_usage_store import SearchUsageStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_reservation_commits_only_billable_success_and_enforces_monthly_limit(tmp_path):
    store = SearchUsageStore(str(tmp_path / "usage.sqlite3"), monthly_limit=2,
                             clock=lambda: datetime(2026, 10, 19, 12, tzinfo=SHANGHAI))

    first = store.reserve_deepseek_slot()
    second = store.reserve_deepseek_slot()
    assert first and second and store.reserve_deepseek_slot() is None
    assert store.usage_for_month("2026-10") == {"successful": 0, "in_flight": 2, "limit": 2}

    store.finish_deepseek_slot(first, billable_success=True)
    store.finish_deepseek_slot(second, billable_success=False)
    assert store.usage_for_month("2026-10") == {"successful": 1, "in_flight": 0, "limit": 2}
    assert store.reserve_deepseek_slot()


def test_usage_is_partitioned_by_beijing_calendar_month_and_keeps_reservation_month(tmp_path):
    now = [datetime(2026, 10, 31, 23, 59, tzinfo=SHANGHAI)]
    store = SearchUsageStore(str(tmp_path / "usage.sqlite3"), monthly_limit=2, clock=lambda: now[0])
    october = store.reserve_deepseek_slot()
    now[0] = datetime(2026, 11, 1, 0, 1, tzinfo=SHANGHAI)
    november = store.reserve_deepseek_slot()
    store.finish_deepseek_slot(october, billable_success=True)
    store.finish_deepseek_slot(november, billable_success=True)

    assert store.usage_for_month("2026-10") == {"successful": 1, "in_flight": 0, "limit": 2}
    assert store.usage_for_month("2026-11") == {"successful": 1, "in_flight": 0, "limit": 2}


def test_concurrent_reservations_never_exceed_remaining_limit(tmp_path):
    store = SearchUsageStore(str(tmp_path / "usage.sqlite3"), monthly_limit=12,
                             clock=lambda: datetime(2026, 10, 19, 12, tzinfo=SHANGHAI))
    with ThreadPoolExecutor(max_workers=24) as pool:
        reservations = list(pool.map(lambda _: store.reserve_deepseek_slot(), range(48)))

    reserved = [item for item in reservations if item]
    assert len(reserved) == 12
    assert store.usage_for_month("2026-10") == {"successful": 0, "in_flight": 12, "limit": 12}


def test_configured_limit_cannot_exceed_hard_cap(tmp_path):
    store = SearchUsageStore(str(tmp_path / "usage.sqlite3"), monthly_limit=5_000)
    assert store.usage_for_month("2026-10")["limit"] == 1_000


def test_database_failure_fails_closed_without_reserving_deepseek_slot(tmp_path):
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("file blocks directory creation")
    store = SearchUsageStore(str(not_a_directory / "usage.sqlite3"), monthly_limit=1,
                             clock=lambda: datetime(2026, 10, 19, 12, tzinfo=SHANGHAI))

    assert store.reserve_deepseek_slot() is None
