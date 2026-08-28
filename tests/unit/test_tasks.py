"""TaskManager 取消支持单元测试"""

import threading
import time

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


def test_cancel_running_task_sets_cancelled():
    """运行中的任务 cancel 后置 stop_event，fn 感知后抛 AnalysisCancelledError → cancelled"""
    tm = TaskManager()
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


def test_cancel_unknown_task_returns_false():
    tm = TaskManager()
    assert tm.cancel("nope") is False


def test_cancel_done_task_returns_false():
    tm = TaskManager()
    task_id = tm.submit(lambda: "ok")
    _wait_status(tm, task_id, ("done", "failed", "cancelled"))
    assert tm.cancel(task_id) is False
    assert tm.get(task_id)["status"] == "done"


def test_normal_task_unaffected_by_cancel_api():
    """未调用 cancel 的任务正常 done"""
    tm = TaskManager()
    task_id = tm.submit(lambda: "ok")
    t = _wait_status(tm, task_id, ("done", "failed", "cancelled"))
    assert t["status"] == "done"
    assert t["result"] == "ok"


def test_task_failure_keeps_failed_status():
    """fn 抛普通异常仍标记 failed（不影响既有行为）"""
    tm = TaskManager()

    def fn():
        raise ValueError("boom")

    task_id = tm.submit(fn)
    t = _wait_status(tm, task_id, ("done", "failed", "cancelled"))
    assert t["status"] == "failed"
    assert "boom" in t["error"]


def test_tasks_run_concurrently():
    """多个任务可同时进入 running（线程池并发执行）"""
    tm = TaskManager(max_workers=3)
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


def test_submit_queues_when_workers_full():
    """worker 满时新任务排队（pending）而非拒绝，放行后全部完成"""
    tm = TaskManager(max_workers=1)
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
    assert t is not None
    assert t["status"] == "pending"
    gate.set()
    assert _wait_status(tm, t1, ("done", "failed", "cancelled"))["status"] == "done"
    assert _wait_status(tm, t2, ("done", "failed", "cancelled"))["status"] == "done"
    assert released == ["first", "second"]
