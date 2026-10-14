from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from webapp.investment_news_scheduler import InvestmentNewsScheduler
from webapp.investment_news_store import InvestmentNewsStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


class RecordingScheduleService:
    def __init__(self, collection_status="succeeded"):
        self.collection_status = collection_status
        self.calls = []
        self.sequence = []

    def submit_collection(self, trigger):
        self.calls.append(("collect", trigger))
        self.sequence.append("collect")
        return {"run_id": "run-1", "status": "queued"}

    def wait_for_run(self, run_id, timeout=None):
        self.calls.append(("wait", run_id))
        self.sequence.append("collection_complete")
        return {"status": self.collection_status, "result": {"status": self.collection_status}}

    def submit_digest(self, trigger, force=False):
        self.calls.append(("digest", trigger, force))
        self.sequence.append("digest")
        return {"run_id": "digest-1", "status": "queued"}


def _scheduler(tmp_path, now, service):
    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    return InvestmentNewsScheduler(
        service=service,
        store=store,
        clock=lambda: now,
        wait=lambda event, seconds: event.wait(seconds),
    ), store


def test_startup_catchup_does_not_generate_automatic_digest(tmp_path):
    service = RecordingScheduleService()
    scheduler, store = _scheduler(
        tmp_path, datetime.fromisoformat("2026-10-14T11:20:00+08:00"), service,
    )

    scheduler.run_once(scheduler.clock(), startup=True)

    assert service.calls == [("collect", "startup_catchup")]
    assert not store.has_schedule_slot("2026-10-14T11:00+08:00")


@pytest.mark.parametrize(("hour", "expect_digest"), [(hour, hour in {9, 16}) for hour in range(9, 17)])
def test_scheduled_slots_collect_and_only_09_16_digest_after_completion(tmp_path, hour, expect_digest):
    service = RecordingScheduleService()
    now = datetime(2026, 10, 14, hour, 0, tzinfo=SHANGHAI)
    scheduler, store = _scheduler(tmp_path, now, service)

    scheduler.run_once(now)

    assert service.sequence[:2] == ["collect", "collection_complete"]
    assert ("digest", "scheduled", False) in service.calls if expect_digest else not any(
        call[0] == "digest" for call in service.calls
    )
    assert store.has_schedule_slot(f"2026-10-14T{hour:02d}:00+08:00")
    if expect_digest:
        assert service.sequence == ["collect", "collection_complete", "digest"]


def test_duplicate_slot_is_not_dispatched_twice(tmp_path):
    service = RecordingScheduleService()
    now = datetime(2026, 10, 14, 9, 0, tzinfo=SHANGHAI)
    scheduler, store = _scheduler(tmp_path, now, service)

    scheduler.run_once(now)
    first_count = len(service.calls)
    scheduler.run_once(now)

    assert first_count == 3
    assert len(service.calls) == first_count


def test_startup_on_exact_summary_hour_uses_scheduled_slot_once(tmp_path):
    service = RecordingScheduleService()
    now = datetime(2026, 10, 14, 9, 0, tzinfo=SHANGHAI)
    scheduler, store = _scheduler(tmp_path, now, service)

    scheduler.run_once(now, startup=True)

    assert service.sequence == ["collect", "collection_complete", "digest"]
    assert store.has_schedule_slot("2026-10-14T09:00+08:00")


@pytest.mark.parametrize("hour", [8, 17, 23])
def test_startup_outside_collection_window_does_not_catch_up(tmp_path, hour):
    service = RecordingScheduleService()
    now = datetime(2026, 10, 14, hour, 20, tzinfo=SHANGHAI)
    scheduler, _ = _scheduler(tmp_path, now, service)

    scheduler.run_once(now, startup=True)

    assert service.calls == []


def test_failed_scheduled_collection_does_not_create_digest(tmp_path):
    service = RecordingScheduleService(collection_status="failed")
    now = datetime(2026, 10, 14, 16, 0, tzinfo=SHANGHAI)
    scheduler, _ = _scheduler(tmp_path, now, service)

    scheduler.run_once(now)

    assert service.sequence == ["collect", "collection_complete"]
    assert not any(call[0] == "digest" for call in service.calls)
