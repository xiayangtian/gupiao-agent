import threading
import time

from webapp.task_store import TaskStore
from webapp.tasks import TaskManager


def _db_path(tmp_path):
    return str(tmp_path / "tasks.sqlite3")


def test_events_are_monotonic_and_replay_after_cursor(tmp_path):
    store = TaskStore(_db_path(tmp_path))
    store.create("t1")

    first = store.append_event("t1", "quick.ready", {"quick": {"conclusions": []}})
    second = store.append_event(
        "t1", "section.ready", {"section": {"section_id": "cash", "title": "现金流"}}
    )

    assert second.id > first.id
    assert [event.id for event in store.list_events("t1", after_id=first.id)] == [second.id]
    store.close()


def test_event_and_snapshot_are_updated_in_one_transaction(tmp_path):
    store = TaskStore(_db_path(tmp_path))
    store.create("t1")

    store.append_event("t1", "job.stage_changed", {"stage": "fast_ready"})
    store.append_event("t1", "quick.ready", {"quick": {"conclusions": [{"id": "q1"}]}})
    store.append_event("t1", "section.ready", {"section": {"section_id": "cash"}})
    store.append_event(
        "t1", "section.updated", {"section": {"section_id": "cash", "title": "已更新"}}
    )

    snapshot = store.get("t1")["result"]
    assert snapshot["stage"] == "fast_ready"
    assert snapshot["quick"]["conclusions"] == [{"id": "q1"}]
    assert snapshot["sections"] == [{"section_id": "cash", "title": "已更新"}]
    store.close()


def test_manager_eventful_submit_injects_emit_and_wakes_waiter(tmp_path):
    manager = TaskManager(db_path=_db_path(tmp_path))
    release = threading.Event()

    def fn(emit):
        emit("quick.ready", {"quick": {"conclusions": []}})
        release.wait(timeout=2)
        return {"stage": "completed"}

    task_id = manager.submit(fn, eventful=True)
    events = manager.wait_for_events(task_id, after_id=0, timeout=2.0)

    assert [event.event_type for event in events] == ["quick.ready"]
    assert manager.get(task_id)["result"]["quick"] == {"conclusions": []}
    release.set()
    manager.shutdown()


def test_eventful_task_serializes_domain_result_with_to_dict(tmp_path):
    manager = TaskManager(db_path=_db_path(tmp_path))

    class Result:
        def to_dict(self):
            return {"stage": "completed", "sections": []}

    task_id = manager.submit(lambda emit: Result(), eventful=True)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and manager.get(task_id)["status"] != "done":
        time.sleep(0.01)

    assert manager.get(task_id)["result"] == {"stage": "completed", "sections": []}
    manager.shutdown()


def test_wait_for_events_times_out_without_busy_polling(tmp_path):
    manager = TaskManager(db_path=_db_path(tmp_path))
    release = threading.Event()
    task_id = manager.submit(lambda: release.wait(timeout=2))

    started = time.monotonic()
    events = manager.wait_for_events(task_id, after_id=0, timeout=0.05)

    assert events == []
    assert time.monotonic() - started >= 0.04
    release.set()
    manager.shutdown()


def test_cleanup_removes_events_with_old_terminal_task(tmp_path):
    store = TaskStore(_db_path(tmp_path), history_limit=0)
    store.create("t1")
    store.append_event("t1", "job.completed", {"analysis": {"stage": "completed"}})
    store.update("t1", status="done")

    assert store.cleanup() == 1
    assert store.list_events("t1") == []
    store.close()
