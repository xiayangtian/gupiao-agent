"""Asia/Shanghai collection and digest schedule for investment news."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo


_SHANGHAI = ZoneInfo("Asia/Shanghai")


class InvestmentNewsScheduler:
    """Single-process scheduler; the store's slot key prevents duplicate hourly dispatch."""

    def __init__(
        self,
        service: Any,
        store: Any,
        clock: Callable[[], datetime],
        wait: Callable[[threading.Event, float], Any] | None = None,
    ) -> None:
        self.service = service
        self.store = store
        self.clock = clock
        self.wait = wait or (lambda event, seconds: event.wait(seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._startup_catchup_done = False

    @staticmethod
    def _localize(now: datetime) -> datetime:
        if now.tzinfo is None:
            now = now.replace(tzinfo=_SHANGHAI)
        return now.astimezone(_SHANGHAI)

    @staticmethod
    def _slot_id(now: datetime) -> str:
        offset = now.strftime("%z")
        return f"{now:%Y-%m-%dT%H:00}{offset[:3]}:{offset[3:]}"

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._startup_catchup_done = False
            self._thread = threading.Thread(
                target=self._loop, name="investment-news-scheduler", daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def _dispatch_slot(self, now: datetime) -> dict[str, Any] | None:
        if not 9 <= now.hour <= 16:
            return None
        slot_id = self._slot_id(now)
        if not self.store.mark_schedule_slot(slot_id, created_at=now.isoformat()):
            return {"status": "skipped", "reason": "slot_already_dispatched", "slot_id": slot_id}
        queued = self.service.submit_collection("scheduled")
        run_id = queued.get("run_id")
        completed = self.service.wait_for_run(run_id) if run_id else None
        if completed is None:
            return {"status": "failed", "reason": "collection_run_missing", "slot_id": slot_id}
        collection_result = completed.get("result") or {}
        collection_status = collection_result.get("status", completed.get("status"))
        result: dict[str, Any] = {
            "status": collection_status,
            "slot_id": slot_id,
            "collection_run_id": run_id,
        }
        if now.hour in {9, 16} and collection_status in {"succeeded", "partial"}:
            result["digest"] = self.service.submit_digest("scheduled", force=False)
        return result

    def run_once(self, now: datetime, startup: bool = False) -> dict[str, Any] | None:
        local_now = self._localize(now)
        if startup:
            if self._startup_catchup_done:
                return None
            self._startup_catchup_done = True
            if not 9 <= local_now.hour <= 16:
                return None
            if local_now.minute == 0:
                return self._dispatch_slot(local_now)
            queued = self.service.submit_collection("startup_catchup")
            return {"status": "queued", "trigger": "startup_catchup", **queued}
        if local_now.minute != 0:
            return None
        return self._dispatch_slot(local_now)

    def _loop(self) -> None:
        if not self._stop_event.is_set():
            self.run_once(self.clock(), startup=True)
        while not self._stop_event.is_set():
            now = self._localize(self.clock())
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            seconds = max(0.05, (next_hour - now).total_seconds())
            self.wait(self._stop_event, seconds)
            if self._stop_event.is_set():
                return
            self.run_once(self.clock(), startup=False)
