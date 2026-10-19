"""Cross-process monthly reservation ledger for billable DeepSeek Search calls."""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
HARD_MONTHLY_LIMIT = 1_000


class SearchUsageStore:
    """Track successful calls and in-flight reservations in SQLite transactions."""

    def __init__(self, path: str, monthly_limit: int = HARD_MONTHLY_LIMIT,
                 *, clock: Callable[[], datetime] | None = None) -> None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must not be empty")
        if isinstance(monthly_limit, bool) or not isinstance(monthly_limit, int) or monthly_limit < 1:
            raise ValueError("monthly_limit must be a positive integer")
        self.path = path
        self.monthly_limit = min(monthly_limit, HARD_MONTHLY_LIMIT)
        self.clock = clock or (lambda: datetime.now(SHANGHAI))

    def _month_now(self) -> str:
        now = self.clock()
        if not isinstance(now, datetime):
            raise ValueError("clock must return datetime")
        if now.tzinfo is None:
            now = now.replace(tzinfo=SHANGHAI)
        return now.astimezone(SHANGHAI).strftime("%Y-%m")

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS deepseek_search_monthly_usage ("
            "month TEXT PRIMARY KEY, successful INTEGER NOT NULL CHECK(successful >= 0))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS deepseek_search_reservations ("
            "id TEXT PRIMARY KEY, month TEXT NOT NULL)"
        )
        return connection

    def reserve_deepseek_slot(self) -> str | None:
        """Reserve capacity before dispatch; SQLite errors fail closed."""
        month = self._month_now()
        reservation_id = uuid.uuid4().hex
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO deepseek_search_monthly_usage(month, successful) VALUES (?, 0)",
                (month,),
            )
            successful = connection.execute(
                "SELECT successful FROM deepseek_search_monthly_usage WHERE month = ?", (month,),
            ).fetchone()[0]
            in_flight = connection.execute(
                "SELECT COUNT(*) FROM deepseek_search_reservations WHERE month = ?", (month,),
            ).fetchone()[0]
            if successful + in_flight >= self.monthly_limit:
                connection.rollback()
                return None
            connection.execute(
                "INSERT INTO deepseek_search_reservations(id, month) VALUES (?, ?)",
                (reservation_id, month),
            )
            connection.commit()
            return reservation_id
        except (OSError, sqlite3.Error):
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            return None
        finally:
            if connection is not None:
                connection.close()

    def finish_deepseek_slot(self, reservation_id: str, *, billable_success: bool) -> bool:
        """Commit a nonempty HTTP 200 result or release its reservation."""
        if not isinstance(reservation_id, str) or not reservation_id:
            return False
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT month FROM deepseek_search_reservations WHERE id = ?", (reservation_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            if billable_success:
                connection.execute(
                    "UPDATE deepseek_search_monthly_usage SET successful = successful + 1 WHERE month = ?",
                    (row[0],),
                )
            connection.execute("DELETE FROM deepseek_search_reservations WHERE id = ?", (reservation_id,))
            connection.commit()
            return True
        except (OSError, sqlite3.Error):
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            return False
        finally:
            if connection is not None:
                connection.close()

    def usage_for_month(self, month: str | None = None) -> dict[str, int]:
        month = month or self._month_now()
        if len(month) != 7 or month[4] != "-":
            raise ValueError("month must use YYYY-MM format")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT successful FROM deepseek_search_monthly_usage WHERE month = ?", (month,),
            ).fetchone()
            in_flight = connection.execute(
                "SELECT COUNT(*) FROM deepseek_search_reservations WHERE month = ?", (month,),
            ).fetchone()[0]
            connection.commit()
            return {
                "successful": int(row[0]) if row else 0,
                "in_flight": int(in_flight),
                "limit": self.monthly_limit,
            }
        finally:
            connection.close()
