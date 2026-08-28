"""webapp.tasks — 并发长任务执行器

线程池并发执行任务（默认 max_workers=3，可用环境变量 TASK_MAX_WORKERS 覆盖），
任务表保存在内存（重启即清）。submit 后排队执行，同一时刻最多 max_workers
个任务并行——既满足多组财报分析并发，又控制 AI 请求的并发成本。
"""

import logging
import os
import queue
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from financial_report_fetcher.exceptions import AnalysisCancelledError

logger = logging.getLogger(__name__)

# 默认并发 worker 数；同一时刻最多这么多个模型请求并行
DEFAULT_MAX_WORKERS = 3


def _default_max_workers() -> int:
    raw = os.environ.get("TASK_MAX_WORKERS", "")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_WORKERS


class TaskManager:
    def __init__(self, max_workers: Optional[int] = None) -> None:
        self._max_workers = max_workers or _default_max_workers()
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="task-worker",
        )

    # ── 对外接口 ─────────────────────────────────────────

    def submit(
        self,
        fn: Callable[[], Any],
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[str]:
        """提交任务并排队执行，返回 task_id。

        stop_event: 可选取消信号；cancel(task_id) 会 set 该事件，
        任务函数内部应定期检查并以 AnalysisCancelledError 主动退出。
        """
        with self._lock:
            task_id = uuid.uuid4().hex[:12]
            self._tasks[task_id] = {
                "status": "pending",
                "progress": None,
                "result": None,
                "error": None,
            }
            if stop_event is not None:
                self._stop_events[task_id] = stop_event
        try:
            self._executor.submit(self._run_task, task_id, fn)
        except RuntimeError:
            # 线程池已关闭（shutdown 后），拒绝新任务
            with self._lock:
                self._tasks.pop(task_id, None)
                self._stop_events.pop(task_id, None)
            return None
        return task_id

    def cancel(self, task_id: str) -> bool:
        """请求取消任务（仅 pending/running 有效）；置标志并 set stop_event"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["status"] not in ("pending", "running"):
                return False
            task["cancelled"] = True
            stop_event = self._stop_events.get(task_id)
        if stop_event is not None:
            stop_event.set()
        return True

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        """返回任务快照（副本）或 None"""
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task is not None else None

    def shutdown(self) -> None:
        """关闭线程池（测试/进程退出时调用）"""
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ── 内部 ─────────────────────────────────────────────

    def _run_task(self, task_id: str, fn: Callable[[], Any]) -> None:
        with self._lock:
            # 排队期间被取消：直接置 cancelled 不执行
            if self._tasks[task_id].get("cancelled"):
                self._tasks[task_id]["status"] = "cancelled"
                self._tasks[task_id]["error"] = "任务已由用户停止"
                return
            self._tasks[task_id]["status"] = "running"
        try:
            result = fn()
        except AnalysisCancelledError:
            logger.info("任务 %s 已由用户取消", task_id)
            with self._lock:
                self._tasks[task_id]["status"] = "cancelled"
                self._tasks[task_id]["error"] = "任务已由用户停止"
        except Exception as exc:
            logger.exception("任务 %s 失败", task_id)
            with self._lock:
                self._tasks[task_id]["status"] = "failed"
                self._tasks[task_id]["error"] = str(exc)
        else:
            with self._lock:
                # 执行期间被取消（fn 正常返回未抛异常）也置 cancelled
                if self._tasks[task_id].get("cancelled"):
                    self._tasks[task_id]["status"] = "cancelled"
                    self._tasks[task_id]["error"] = "任务已由用户停止"
                else:
                    self._tasks[task_id]["status"] = "done"
                    self._tasks[task_id]["result"] = result
