"""SQLite-backed persistence for background task state."""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, TypeVar


_UNSET = object()
_TERMINAL_STATUSES = ("done", "failed", "cancelled")
_RECOVERY_ERROR = "任务因服务重启中断，请重新提交分析"
_SQLITE_ATTEMPTS = 3
_SQLITE_RETRY_DELAY = 0.05
_T = TypeVar("_T")


class TaskStorageError(RuntimeError):
    """A bounded SQLite operation failed and cannot be hidden from callers."""

    def __init__(self, operation: str, cause: sqlite3.Error) -> None:
        super().__init__(f"{operation}: {cause}")
        self.operation = operation
        self.cause = cause


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """Thread-safe SQLite repository for task snapshots."""

    def __init__(self, db_path: str, history_limit: int = 1000) -> None:
        self._db_path = db_path
        self._history_limit = max(0, int(history_limit))
        self._lock = threading.RLock()
        self._closed = False

        parent = os.path.dirname(os.path.abspath(db_path))
        if db_path != ":memory:":
            os.makedirs(parent, exist_ok=True)
        self._connection = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=0,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._write(
                "initialize task store",
                lambda: self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                      task_id TEXT PRIMARY KEY, status TEXT NOT NULL, progress REAL,
                      result_json TEXT, error TEXT, cancelled INTEGER NOT NULL DEFAULT 0,
                      retryable INTEGER NOT NULL DEFAULT 0,
                      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    )
                    """
                ),
            )

    def create(self, task_id: str) -> None:
        timestamp = _now()
        with self._lock:
            self._ensure_open()
            self._write(
                "create task",
                lambda: self._connection.execute(
                    """
                    INSERT INTO tasks (
                        task_id, status, progress, result_json, error,
                        cancelled, retryable, created_at, updated_at
                    ) VALUES (?, 'pending', NULL, NULL, NULL, 0, 0, ?, ?)
                    """,
                    (task_id, timestamp, timestamp),
                ),
            )

    def update(
        self,
        task_id: str,
        *,
        status: Any = _UNSET,
        progress: Any = _UNSET,
        result: Any = _UNSET,
        error: Any = _UNSET,
        cancelled: Any = _UNSET,
        retryable: Any = _UNSET,
    ) -> None:
        assignments = []
        values = []
        for column, value in (
            ("status", status),
            ("progress", progress),
            ("error", error),
        ):
            if value is not _UNSET:
                assignments.append(f"{column} = ?")
                values.append(value)
        if result is not _UNSET:
            result_json = json.dumps(result, ensure_ascii=False, allow_nan=False)
            assignments.append("result_json = ?")
            values.append(result_json)
        for column, value in (("cancelled", cancelled), ("retryable", retryable)):
            if value is not _UNSET:
                assignments.append(f"{column} = ?")
                values.append(int(bool(value)))
        if not assignments:
            return

        assignments.append("updated_at = ?")
        values.extend((_now(), task_id))
        with self._lock:
            self._ensure_open()
            self._write(
                "update task",
                lambda: self._connection.execute(
                    f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id = ?",
                    values,
                ),
            )

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            row = self._read(
                "read task",
                lambda: self._connection.execute(
                    """
                    SELECT status, progress, result_json, error, cancelled, retryable
                    FROM tasks WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone(),
            )
        if row is None:
            return None
        task = {
            "status": row["status"],
            "progress": row["progress"],
            "result": json.loads(row["result_json"]) if row["result_json"] is not None else None,
            "error": row["error"],
        }
        if row["cancelled"]:
            task["cancelled"] = True
        if row["retryable"]:
            task["retryable"] = True
        return task

    def recover_interrupted(self) -> int:
        """Atomically turn pre-restart work into retryable failures."""
        with self._lock:
            self._ensure_open()
            cursor = self._write(
                "recover interrupted tasks",
                lambda: self._connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'failed', error = ?, cancelled = 0,
                        retryable = 1, updated_at = ?
                    WHERE status IN ('pending', 'running')
                    """,
                    (_RECOVERY_ERROR, _now()),
                ),
            )
            return cursor.rowcount

    def cleanup(self) -> int:
        """Keep only the newest configured number of terminal tasks."""
        with self._lock:
            self._ensure_open()
            placeholders = ", ".join("?" for _ in _TERMINAL_STATUSES)

            def cleanup_operation() -> int:
                rows = self._connection.execute(
                    f"""
                    SELECT task_id FROM tasks
                    WHERE status IN ({placeholders})
                    ORDER BY updated_at DESC, created_at DESC, task_id DESC
                    LIMIT -1 OFFSET ?
                    """,
                    (*_TERMINAL_STATUSES, self._history_limit),
                ).fetchall()
                if rows:
                    self._connection.executemany(
                        "DELETE FROM tasks WHERE task_id = ?",
                        ((row["task_id"],) for row in rows),
                    )
                return len(rows)

            return self._write("cleanup task history", cleanup_operation)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._connection.close()
            except sqlite3.Error as exc:
                raise TaskStorageError("close task store", exc) from exc
            finally:
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("TaskStore is closed")

    def _read(self, operation: str, fn: Callable[[], _T]) -> _T:
        return self._run_sqlite(operation, fn, write=False)

    def _write(self, operation: str, fn: Callable[[], _T]) -> _T:
        return self._run_sqlite(operation, fn, write=True)

    def _run_sqlite(
        self,
        operation: str,
        fn: Callable[[], _T],
        *,
        write: bool,
    ) -> _T:
        for attempt in range(_SQLITE_ATTEMPTS):
            try:
                result = fn()
                if write:
                    self._connection.commit()
                return result
            except sqlite3.Error as exc:
                if write:
                    try:
                        self._connection.rollback()
                    except sqlite3.Error as rollback_exc:
                        raise TaskStorageError(
                            f"{operation} rollback",
                            rollback_exc,
                        ) from exc
                if self._is_transient_lock(exc) and attempt + 1 < _SQLITE_ATTEMPTS:
                    time.sleep(_SQLITE_RETRY_DELAY)
                    continue
                raise TaskStorageError(operation, exc) from exc
        raise AssertionError("unreachable SQLite retry state")

    @staticmethod
    def _is_transient_lock(exc: sqlite3.Error) -> bool:
        message = str(exc).lower()
        return isinstance(exc, sqlite3.OperationalError) and (
            "locked" in message or "busy" in message
        )
