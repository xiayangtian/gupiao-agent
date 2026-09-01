"""webapp.tasks — SQLite 持久化的并发长任务执行器。"""

import logging
import math
import os
import threading
import uuid
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from financial_report_fetcher.exceptions import AnalysisCancelledError

from .task_store import TaskStorageError, TaskStore

logger = logging.getLogger(__name__)

# 默认并发 worker 数；同一时刻最多这么多个模型请求并行
DEFAULT_MAX_WORKERS = 3
DEFAULT_HISTORY_LIMIT = 1000
DEFAULT_DB_PATH = os.path.join("data", "tasks.sqlite3")
_STATE_ACCEPTING = "accepting"
_STATE_STOPPING = "stopping"
_STATE_CLOSED = "closed"
_STORAGE_FAILURE_PREFIX = "任务状态存储失败，请重新提交分析"


def _default_max_workers() -> int:
    raw = os.environ.get("TASK_MAX_WORKERS", "")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_WORKERS


def _default_history_limit() -> int:
    raw = os.environ.get("TASK_HISTORY_LIMIT", "")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_LIMIT


class TaskManager:
    def __init__(
        self,
        max_workers: Optional[int] = None,
        db_path: Optional[str] = None,
        history_limit: Optional[int] = None,
    ) -> None:
        self._max_workers = max_workers or _default_max_workers()
        self._db_path = db_path or os.environ.get("TASK_DB_PATH") or DEFAULT_DB_PATH
        self._history_limit = (
            _default_history_limit() if history_limit is None else max(0, history_limit)
        )
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._state = _STATE_ACCEPTING
        self._storage_error: Optional[str] = None
        self._deferred_failures: Dict[str, str] = {}
        self._futures: Dict[str, Future[Any]] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._store: Optional[TaskStore] = TaskStore(self._db_path, self._history_limit)
        self._store.recover_interrupted()
        self._store.cleanup()
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="task-worker",
        )

    # ── 对外接口 ─────────────────────────────────────────

    def submit(
        self,
        fn: Callable[..., Any],
        stop_event: Optional[threading.Event] = None,
        progressive: bool = False,
    ) -> Optional[str]:
        """提交任务并排队执行，返回 task_id。

        stop_event: 可选取消信号；cancel(task_id) 会 set 该事件。
        progressive: 为 True 时 fn 接收 report_progress(float) 回调，进度会持久化。
        """
        with self._condition:
            if self._state != _STATE_ACCEPTING:
                return None
            store = self._ensure_store()
            task_id = uuid.uuid4().hex[:12]
            try:
                store.create(task_id)
            except TaskStorageError as exc:
                self._set_storage_error_locked(exc)
                return None
            if stop_event is not None:
                self._stop_events[task_id] = stop_event
            try:
                submitted_fn = fn
                if progressive:
                    submitted_fn = lambda accepted_fn=fn: accepted_fn(
                        self._progress_reporter(task_id)
                    )
                future = self._executor.submit(self._run_task, task_id, submitted_fn)
            except RuntimeError:
                try:
                    store.update(
                        task_id,
                        status="failed",
                        result=None,
                        error="任务执行器已关闭",
                        retryable=True,
                    )
                    store.cleanup()
                except TaskStorageError as exc:
                    self._record_storage_failure_locked(task_id, exc)
                self._stop_events.pop(task_id, None)
                return None
            self._futures[task_id] = future
            future.add_done_callback(
                lambda completed, accepted_id=task_id: self._observe_future(
                    accepted_id,
                    completed,
                )
            )
        return task_id

    def cancel(self, task_id: str) -> bool:
        """请求取消任务（仅 pending/running 有效）；置标志并 set stop_event"""
        with self._condition:
            if self._state != _STATE_ACCEPTING:
                return False
            store = self._ensure_store()
            if task_id in self._deferred_failures:
                return False
            try:
                task = store.get(task_id)
            except TaskStorageError as exc:
                self._set_storage_error_locked(exc)
                if task_id not in self._futures:
                    return False
                self._record_storage_failure_locked(task_id, exc)
                task = {"status": "running"}
            if task is None or task["status"] not in ("pending", "running"):
                return False
            try:
                store.update(task_id, cancelled=True)
            except TaskStorageError as exc:
                self._record_storage_failure_locked(task_id, exc)
            stop_event = self._stop_events.get(task_id)
        if stop_event is not None:
            stop_event.set()
        return True

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        """返回任务快照（副本）或 None"""
        with self._condition:
            if self._state != _STATE_ACCEPTING:
                return None
            store = self._ensure_store()
            if task_id in self._deferred_failures:
                return self._reconcile_storage_failure_locked(task_id, store)
            try:
                return store.get(task_id)
            except TaskStorageError as exc:
                self._set_storage_error_locked(exc)
                return self._storage_failure_snapshot(str(exc))

    @property
    def storage_error(self) -> Optional[str]:
        """Return the latest bounded storage failure, if one is still active."""
        with self._condition:
            return self._storage_error

    def shutdown(self) -> None:
        """关闭线程池（测试/进程退出时调用）"""
        with self._condition:
            if self._state == _STATE_CLOSED:
                return
            if self._state == _STATE_STOPPING:
                self._condition.wait_for(lambda: self._state == _STATE_CLOSED)
                return
            self._state = _STATE_STOPPING

        try:
            self._executor.shutdown(wait=True, cancel_futures=True)
        finally:
            with self._condition:
                if self._store is not None:
                    self._flush_deferred_failures_locked(self._store)
                    try:
                        self._store.close()
                    except (TaskStorageError, RuntimeError) as exc:
                        self._storage_error = str(exc)
                        logger.exception("关闭任务状态数据库失败")
                    self._store = None
                self._state = _STATE_CLOSED
                self._condition.notify_all()

    # ── 内部 ─────────────────────────────────────────────

    def _progress_reporter(self, task_id: str) -> Callable[[float], None]:
        def report(value: float) -> None:
            progress = float(value)
            if not math.isfinite(progress):
                raise ValueError("任务进度必须是有限数字")
            progress = max(0.0, min(1.0, progress))
            with self._condition:
                store = self._ensure_store()
                try:
                    store.update(task_id, progress=progress)
                except TaskStorageError as exc:
                    self._record_storage_failure_locked(task_id, exc)

        return report

    def _run_task(self, task_id: str, fn: Callable[[], Any]) -> None:
        with self._condition:
            store = self._ensure_store()
            # 排队期间被取消：直接置 cancelled 不执行
            try:
                task = store.get(task_id)
            except TaskStorageError as exc:
                self._record_storage_failure_locked(task_id, exc)
                self._stop_events.pop(task_id, None)
                return
            if task is None:
                return
            if task.get("cancelled"):
                self._persist_terminal_locked(
                    task_id,
                    status="cancelled",
                    result=None,
                    error="任务已由用户停止",
                    retryable=False,
                )
                self._cleanup_locked(store)
                self._stop_events.pop(task_id, None)
                return
            try:
                store.update(task_id, status="running")
            except TaskStorageError as exc:
                self._record_storage_failure_locked(task_id, exc)
                self._stop_events.pop(task_id, None)
                return
        try:
            result = fn()
        except AnalysisCancelledError:
            logger.info("任务 %s 已由用户取消", task_id)
            with self._condition:
                self._persist_terminal_locked(
                    task_id,
                    status="cancelled",
                    result=None,
                    error="任务已由用户停止",
                    retryable=False,
                )
        except Exception as exc:
            logger.exception("任务 %s 失败", task_id)
            with self._condition:
                self._persist_terminal_locked(
                    task_id,
                    status="failed",
                    result=None,
                    error=str(exc),
                    retryable=False,
                )
        else:
            with self._condition:
                # 执行期间被取消（fn 正常返回未抛异常）也置 cancelled
                try:
                    task = store.get(task_id)
                except TaskStorageError as exc:
                    self._record_storage_failure_locked(task_id, exc)
                    task = None
                if task is not None and task.get("cancelled"):
                    self._persist_terminal_locked(
                        task_id,
                        status="cancelled",
                        result=None,
                        error="任务已由用户停止",
                        retryable=False,
                    )
                elif task_id not in self._deferred_failures:
                    try:
                        store.update(
                            task_id,
                            status="done",
                            result=result,
                            error=None,
                            cancelled=False,
                            retryable=False,
                        )
                    except (TypeError, ValueError, RecursionError) as exc:
                        logger.exception("任务 %s 的结果无法 JSON 序列化", task_id)
                        self._persist_terminal_locked(
                            task_id,
                            status="failed",
                            result=None,
                            error=f"任务结果无法 JSON 序列化: {exc}",
                            retryable=False,
                        )
                    except TaskStorageError as exc:
                        self._record_storage_failure_locked(task_id, exc)
        finally:
            with self._condition:
                self._cleanup_locked(store)
                self._stop_events.pop(task_id, None)

    def _ensure_store(self) -> TaskStore:
        if self._store is None:
            raise RuntimeError("TaskManager is shut down")
        return self._store

    def _observe_future(self, task_id: str, future: Future[Any]) -> None:
        try:
            exception = future.exception()
        except CancelledError:
            exception = RuntimeError("任务在线程池关闭前未执行")
        with self._condition:
            self._futures.pop(task_id, None)
            if exception is not None:
                logger.error(
                    "任务 %s 的 worker Future 异常",
                    task_id,
                    exc_info=(type(exception), exception, exception.__traceback__),
                )
                self._record_storage_failure_locked(task_id, exception)

    def _persist_terminal_locked(self, task_id: str, **fields: Any) -> bool:
        try:
            self._ensure_store().update(task_id, **fields)
            return True
        except TaskStorageError as exc:
            self._record_storage_failure_locked(task_id, exc)
            return False

    def _cleanup_locked(self, store: TaskStore) -> None:
        try:
            store.cleanup()
        except TaskStorageError as exc:
            self._set_storage_error_locked(exc)

    def _record_storage_failure_locked(self, task_id: str, exc: BaseException) -> None:
        message = f"{_STORAGE_FAILURE_PREFIX}: {exc}"
        self._deferred_failures[task_id] = message
        self._storage_error = str(exc)

    def _set_storage_error_locked(self, exc: BaseException) -> None:
        self._storage_error = str(exc)

    def _reconcile_storage_failure_locked(
        self,
        task_id: str,
        store: TaskStore,
    ) -> Dict[str, Any]:
        message = self._deferred_failures[task_id]
        try:
            store.update(
                task_id,
                status="failed",
                result=None,
                error=message,
                cancelled=False,
                retryable=True,
            )
            task = store.get(task_id)
        except TaskStorageError as exc:
            self._set_storage_error_locked(exc)
            return self._storage_failure_snapshot(message)

        self._deferred_failures.pop(task_id, None)
        if not self._deferred_failures:
            self._storage_error = None
        self._cleanup_locked(store)
        return task or self._storage_failure_snapshot(message)

    def _flush_deferred_failures_locked(self, store: TaskStore) -> None:
        for task_id in list(self._deferred_failures):
            self._reconcile_storage_failure_locked(task_id, store)

    @staticmethod
    def _storage_failure_snapshot(message: str) -> Dict[str, Any]:
        return {
            "status": "failed",
            "progress": None,
            "result": None,
            "error": message,
            "retryable": True,
            "storage_error": True,
        }
