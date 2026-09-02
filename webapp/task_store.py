"""SQLite-backed persistence for background task state."""

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, TypeVar


_UNSET = object()
_TERMINAL_STATUSES = ("done", "failed", "cancelled", "partial")
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


@dataclass(frozen=True)
class TaskEvent:
    id: int
    task_id: str
    event_type: str
    payload: Dict[str, Any]
    created_at: str


def _apply_event(snapshot: Any, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(snapshot) if isinstance(snapshot, dict) else {}
    if event_type == "job.stage_changed" and isinstance(payload.get("stage"), str):
        result["stage"] = payload["stage"]
    elif event_type == "quick.ready" and isinstance(payload.get("quick"), dict):
        result["quick"] = payload["quick"]
    elif event_type in {"section.ready", "section.updated"}:
        section = payload.get("section")
        if isinstance(section, dict) and section.get("section_id"):
            sections = [
                item for item in result.get("sections", [])
                if isinstance(item, dict) and item.get("section_id") != section["section_id"]
            ]
            sections.append(section)
            result["sections"] = sections
    elif event_type == "quick.corrected" and isinstance(payload.get("correction"), dict):
        quick = dict(result.get("quick") or {})
        corrections = list(quick.get("corrections") or [])
        corrections.append(payload["correction"])
        quick["corrections"] = corrections
        result["quick"] = quick
    elif event_type.startswith("job.") and isinstance(payload.get("analysis"), dict):
        result = dict(payload["analysis"])
    if event_type in {"job.completed", "job.partial", "job.cancelled", "job.failed"}:
        result["stage"] = event_type.removeprefix("job.")
    return result


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
        self._connection.execute("PRAGMA foreign_keys = ON")
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
            self._write(
                "initialize task events",
                lambda: self._connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS task_events (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      task_id TEXT NOT NULL,
                      event_type TEXT NOT NULL,
                      payload_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_task_events_task_id_id
                    ON task_events(task_id, id);
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

    def append_event(
        self,
        task_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> TaskEvent:
        if not event_type.strip():
            raise ValueError("event_type 不能为空")
        if not isinstance(payload, dict):
            raise TypeError("payload 必须是字典")
        payload_json = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        timestamp = _now()

        def append_operation() -> tuple[int, Dict[str, Any]]:
            row = self._connection.execute(
                "SELECT result_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"任务不存在: {task_id}")
            current = json.loads(row["result_json"]) if row["result_json"] else None
            snapshot = _apply_event(current, event_type, payload)
            cursor = self._connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, event_type, payload_json, timestamp),
            )
            self._connection.execute(
                "UPDATE tasks SET result_json = ?, updated_at = ? WHERE task_id = ?",
                (
                    json.dumps(snapshot, ensure_ascii=False, allow_nan=False),
                    timestamp,
                    task_id,
                ),
            )
            return int(cursor.lastrowid), snapshot

        with self._lock:
            self._ensure_open()
            event_id, _ = self._write("append task event", append_operation)
        return TaskEvent(event_id, task_id, event_type, dict(payload), timestamp)

    def list_events(
        self,
        task_id: str,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[TaskEvent]:
        if after_id < 0:
            raise ValueError("after_id 不能为负数")
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        with self._lock:
            self._ensure_open()
            rows = self._read(
                "list task events",
                lambda: self._connection.execute(
                    """
                    SELECT id, task_id, event_type, payload_json, created_at
                    FROM task_events
                    WHERE task_id = ? AND id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (task_id, int(after_id), int(limit)),
                ).fetchall(),
            )
        return [
            TaskEvent(
                id=int(row["id"]),
                task_id=row["task_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def recover_interrupted(self) -> int:
        """有可消费快照的中断任务恢复为 partial，否则为 retryable failed。"""
        with self._lock:
            self._ensure_open()

            def recover_operation() -> int:
                rows = self._connection.execute(
                    "SELECT task_id, result_json FROM tasks WHERE status IN ('pending', 'running')"
                ).fetchall()
                timestamp = _now()
                for row in rows:
                    snapshot = json.loads(row["result_json"]) if row["result_json"] else None
                    has_result = isinstance(snapshot, dict) and bool(
                        snapshot.get("quick") or snapshot.get("sections")
                    )
                    if has_result:
                        snapshot = dict(snapshot)
                        snapshot["stage"] = "partial"
                        self._connection.execute(
                            """
                            UPDATE tasks SET status = 'partial', result_json = ?, error = ?,
                                cancelled = 0, retryable = 1, updated_at = ? WHERE task_id = ?
                            """,
                            (
                                json.dumps(snapshot, ensure_ascii=False, allow_nan=False),
                                _RECOVERY_ERROR,
                                timestamp,
                                row["task_id"],
                            ),
                        )
                    else:
                        self._connection.execute(
                            """
                            UPDATE tasks SET status = 'failed', error = ?, cancelled = 0,
                                retryable = 1, updated_at = ? WHERE task_id = ?
                            """,
                            (_RECOVERY_ERROR, timestamp, row["task_id"]),
                        )
                return len(rows)

            return self._write("recover interrupted tasks", recover_operation)

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
                        "DELETE FROM task_events WHERE task_id = ?",
                        ((row["task_id"],) for row in rows),
                    )
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
