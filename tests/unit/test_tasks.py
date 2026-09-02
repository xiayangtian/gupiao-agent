"""TaskManager 取消、持久化与生命周期单元测试。"""

import sqlite3
import threading
import time

import pytest

from financial_report_fetcher.exceptions import AnalysisCancelledError
from webapp.tasks import TaskManager


def _wait_status(tm, task_id, statuses, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = tm.get(task_id)
        if t and t["status"] in statuses:
            return t
        time.sleep(0.01)
    return tm.get(task_id)


def _db_path(tmp_path):
    return str(tmp_path / "tasks.sqlite3")


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_cancel_running_task_sets_cancelled(tmp_path):
    """运行中的任务 cancel 后置 stop_event，fn 感知后抛 AnalysisCancelledError → cancelled"""
    tm = TaskManager(db_path=_db_path(tmp_path))
    stop = threading.Event()

    def fn():
        stop.wait(timeout=5.0)
        if stop.is_set():
            raise AnalysisCancelledError("已停止")
        return "ok"

    task_id = tm.submit(fn, stop_event=stop)
    _wait_status(tm, task_id, ("running",))
    assert tm.cancel(task_id) is True
    t = _wait_status(tm, task_id, ("done", "failed", "cancelled"))
    assert t["status"] == "cancelled"
    tm.shutdown()


def test_cancel_unknown_task_returns_false(tmp_path):
    tm = TaskManager(db_path=_db_path(tmp_path))
    assert tm.cancel("nope") is False
    tm.shutdown()


def test_cancel_done_task_returns_false(tmp_path):
    tm = TaskManager(db_path=_db_path(tmp_path))
    task_id = tm.submit(lambda: "ok")
    _wait_status(tm, task_id, ("done", "failed", "cancelled"))
    assert tm.cancel(task_id) is False
    assert tm.get(task_id)["status"] == "done"
    tm.shutdown()


def test_normal_task_unaffected_by_cancel_api(tmp_path):
    """未调用 cancel 的任务正常 done"""
    tm = TaskManager(db_path=_db_path(tmp_path))
    task_id = tm.submit(lambda: "ok")
    t = _wait_status(tm, task_id, ("done", "failed", "cancelled"))
    assert t["status"] == "done"
    assert t["result"] == "ok"
    tm.shutdown()


def test_progressive_task_persists_intermediate_progress(tmp_path):
    """渐进任务应让轮询端在任务完成前读到最新进度。"""
    tm = TaskManager(db_path=_db_path(tmp_path))
    reported = threading.Event()
    release = threading.Event()

    def fn(report_progress):
        report_progress(0.42)
        reported.set()
        release.wait(timeout=5.0)
        return "ok"

    task_id = tm.submit(fn, progressive=True)
    assert reported.wait(timeout=3.0)
    task = tm.get(task_id)
    assert task["status"] == "running"
    assert task["progress"] == pytest.approx(0.42)

    release.set()
    assert _wait_status(tm, task_id, ("done",))["result"] == "ok"
    tm.shutdown()


def test_task_failure_keeps_failed_status(tmp_path):
    """fn 抛普通异常仍标记 failed（不影响既有行为）"""
    tm = TaskManager(db_path=_db_path(tmp_path))

    def fn():
        raise ValueError("boom")

    task_id = tm.submit(fn)
    t = _wait_status(tm, task_id, ("done", "failed", "cancelled"))
    assert t["status"] == "failed"
    assert "boom" in t["error"]
    tm.shutdown()


def test_tasks_run_concurrently(tmp_path):
    """多个任务可同时进入 running（线程池并发执行）"""
    tm = TaskManager(max_workers=3, db_path=_db_path(tmp_path))
    entered = []
    lock = threading.Lock()
    gate1, gate2 = threading.Event(), threading.Event()

    def fn(i, gate):
        with lock:
            entered.append(i)
        gate.wait(timeout=5.0)
        return i

    t1 = tm.submit(lambda: fn(1, gate1))
    t2 = tm.submit(lambda: fn(2, gate2))
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and len(entered) < 2:
        time.sleep(0.01)
    assert len(entered) == 2, f"两个任务应并发进入 running：{entered}"
    gate1.set()
    gate2.set()
    assert _wait_status(tm, t1, ("done", "failed", "cancelled"))["status"] == "done"
    assert _wait_status(tm, t2, ("done", "failed", "cancelled"))["status"] == "done"
    tm.shutdown()


def test_submit_queues_when_workers_full(tmp_path):
    """worker 满时新任务排队（pending）而非拒绝，放行后全部完成"""
    tm = TaskManager(max_workers=1, db_path=_db_path(tmp_path))
    gate = threading.Event()
    released = []

    def blocking():
        gate.wait(timeout=5.0)
        released.append("first")
        return "first"

    t1 = tm.submit(blocking)
    _wait_status(tm, t1, ("running",))
    # worker 被占用：新任务进入 pending 排队
    t2 = tm.submit(lambda: (released.append("second") or "second"))
    t = tm.get(t2)
    assert t == {
        "status": "pending",
        "progress": None,
        "result": None,
        "error": None,
    }
    gate.set()
    assert _wait_status(tm, t1, ("done", "failed", "cancelled"))["status"] == "done"
    assert _wait_status(tm, t2, ("done", "failed", "cancelled"))["status"] == "done"
    assert released == ["first", "second"]
    tm.shutdown()


def test_completed_task_survives_manager_restart(tmp_path):
    db = _db_path(tmp_path)
    first = TaskManager(db_path=db)
    task_id = first.submit(lambda: {"answer": 42})
    assert _wait_status(first, task_id, ("done",))["result"] == {"answer": 42}
    first.shutdown()

    second = TaskManager(db_path=db)
    assert second.get(task_id)["result"] == {"answer": 42}
    second.shutdown()


def test_running_task_becomes_retryable_failure_after_restart(tmp_path):
    from webapp.task_store import TaskStore

    db = _db_path(tmp_path)
    store = TaskStore(db)
    store.create("interrupted")
    store.update("interrupted", status="running")
    store.close()

    manager = TaskManager(db_path=db)
    task = manager.get("interrupted")
    assert task["status"] == "failed"
    assert task["retryable"] is True
    assert "服务重启" in task["error"]
    manager.shutdown()


def test_running_task_with_quick_snapshot_becomes_partial_after_restart(tmp_path):
    from webapp.task_store import TaskStore

    db = _db_path(tmp_path)
    store = TaskStore(db)
    store.create("interrupted-with-result")
    store.update("interrupted-with-result", status="running")
    store.append_event(
        "interrupted-with-result",
        "quick.ready",
        {"quick": {"conclusions": [{"claim": "已生成"}]}},
    )
    store.close()

    manager = TaskManager(db_path=db)
    task = manager.get("interrupted-with-result")

    assert task["status"] == "partial"
    assert task["result"]["stage"] == "partial"
    assert task["result"]["quick"]["conclusions"] == [{"claim": "已生成"}]
    assert task["retryable"] is True
    manager.shutdown()


def test_cancel_requested_running_task_restarts_as_retryable_failure_only(tmp_path):
    """重启恢复应清除未完成取消请求，避免 failed 与 cancelled 同时为真。"""
    from webapp.task_store import TaskStore

    db = _db_path(tmp_path)
    store = TaskStore(db)
    store.create("cancel-requested")
    store.update("cancel-requested", status="running", cancelled=True)
    store.close()

    manager = TaskManager(db_path=db)
    task = manager.get("cancel-requested")

    assert task["status"] == "failed"
    assert task["retryable"] is True
    assert task.get("cancelled") is None
    assert "服务重启" in task["error"]
    manager.shutdown()


def test_manager_recovers_interrupted_tasks_during_initialization(tmp_path):
    from webapp.task_store import TaskStore

    db = _db_path(tmp_path)
    store = TaskStore(db)
    store.create("pending-at-restart")
    store.close()

    manager = TaskManager(db_path=db)
    observer = TaskStore(db)
    task = observer.get("pending-at-restart")
    assert task["status"] == "failed"
    assert task["retryable"] is True
    observer.close()
    manager.shutdown()


def test_non_json_serializable_result_becomes_failed(tmp_path):
    tm = TaskManager(db_path=_db_path(tmp_path))
    task_id = tm.submit(lambda: object())

    task = _wait_status(tm, task_id, ("failed",))
    assert task["status"] == "failed"
    assert task["result"] is None
    assert "JSON" in task["error"]
    tm.shutdown()


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_non_finite_json_result_becomes_failed(tmp_path, value):
    """TaskManager 不得把标准 JSON 无法表示的非有限数持久化为 done。"""
    tm = TaskManager(db_path=_db_path(tmp_path))
    task_id = tm.submit(lambda: {"value": value})

    task = _wait_status(tm, task_id, ("done", "failed"))

    assert task["status"] == "failed"
    assert task["result"] is None
    assert "JSON" in task["error"]
    tm.shutdown()


def test_shutdown_waits_for_worker_to_persist_result(tmp_path):
    db = _db_path(tmp_path)
    tm = TaskManager(db_path=db)
    entered = threading.Event()

    def fn():
        entered.set()
        time.sleep(0.05)
        return {"persisted": True}

    task_id = tm.submit(fn)
    assert entered.wait(timeout=1.0)
    tm.shutdown()

    restarted = TaskManager(db_path=db)
    assert restarted.get(task_id)["result"] == {"persisted": True}
    restarted.shutdown()


def test_failed_sqlite_write_rolls_back_transaction(tmp_path):
    from webapp.task_store import TaskStorageError, TaskStore

    db = _db_path(tmp_path)
    store = TaskStore(db)
    store.create("rollback-check")
    with sqlite3.connect(db) as setup:
        setup.execute(
            """
            CREATE TRIGGER reject_task_update
            BEFORE UPDATE ON tasks
            BEGIN
                SELECT RAISE(ABORT, 'forced write failure');
            END
            """
        )

    with pytest.raises(TaskStorageError, match="forced write failure"):
        store.update("rollback-check", status="running")

    observer = sqlite3.connect(db, timeout=0)
    observer.execute("BEGIN IMMEDIATE")
    observer.rollback()
    observer.close()
    store.close()


def test_locked_worker_write_becomes_observable_and_recovers(tmp_path):
    from webapp.task_store import TaskStore

    db = _db_path(tmp_path)
    manager = TaskManager(db_path=db)
    release_worker = threading.Event()

    def fn():
        release_worker.wait(timeout=3.0)
        return {"answer": 42}

    task_id = manager.submit(fn)
    assert _wait_status(manager, task_id, ("running",))["status"] == "running"

    locker = sqlite3.connect(db, timeout=0, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        release_worker.set()
        assert _wait_until(lambda: manager.storage_error is not None)
        snapshot = manager.get(task_id)
        assert snapshot["status"] == "failed"
        assert snapshot["retryable"] is True
        assert snapshot["storage_error"] is True
    finally:
        locker.rollback()
        locker.close()

    recovered = _wait_status(manager, task_id, ("failed",))
    assert recovered["retryable"] is True
    assert "状态存储" in recovered["error"]

    observer = TaskStore(db)
    assert observer.get(task_id)["status"] == "failed"
    assert observer.get(task_id)["retryable"] is True
    observer.close()
    manager.shutdown()


def test_all_concurrent_shutdown_callers_wait_until_closed(tmp_path):
    db = _db_path(tmp_path)
    manager = TaskManager(db_path=db)
    worker_entered = threading.Event()
    release_worker = threading.Event()
    first_done = threading.Event()
    restarted_created = threading.Event()
    restarted = []

    def fn():
        worker_entered.set()
        release_worker.wait(timeout=3.0)
        return {"done": True}

    task_id = manager.submit(fn)
    assert worker_entered.wait(timeout=1.0)

    def first_shutdown():
        manager.shutdown()
        first_done.set()

    def second_shutdown_then_restart():
        manager.shutdown()
        restarted.append(TaskManager(db_path=db))
        restarted_created.set()

    first = threading.Thread(target=first_shutdown)
    first.start()
    assert _wait_until(
        lambda: getattr(manager, "_state", None) == "stopping"
        or getattr(manager, "_shutdown", False)
    )

    second = threading.Thread(target=second_shutdown_then_restart)
    second.start()
    try:
        assert not restarted_created.wait(timeout=0.15)
        assert not first_done.is_set()
    finally:
        release_worker.set()

    first.join(timeout=2.0)
    second.join(timeout=2.0)
    assert not first.is_alive()
    assert not second.is_alive()
    assert restarted_created.is_set()
    task = restarted[0].get(task_id)
    assert task["status"] == "done"
    assert task["error"] is None
    assert task.get("retryable") is None
    restarted[0].shutdown()
