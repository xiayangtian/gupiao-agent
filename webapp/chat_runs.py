"""Bounded, cooperative control plane for long-running chat requests."""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable


class ChatRunCancelled(RuntimeError):
    """Raised when a chat run has been cooperatively cancelled."""


class ChatRunDeadlineExceeded(TimeoutError):
    """Raised when a chat run reaches its server-side deadline."""


class ChatRunControl:
    def __init__(self, session_id: str, run_id: str, timeout_seconds: float,
                 started_at: float | None = None) -> None:
        if not session_id or not run_id:
            raise ValueError("session_id and run_id are required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.session_id = session_id
        self.run_id = run_id
        self.started_at = time.monotonic() if started_at is None else started_at
        self.deadline = self.started_at + timeout_seconds
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._cancel_reason = ""

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def cancel_reason(self) -> str:
        with self._lock:
            return self._cancel_reason

    @property
    def expired(self) -> bool:
        return self.remaining_seconds() <= 0

    def cancel(self, reason: str = "user") -> bool:
        with self._lock:
            if self.cancel_event.is_set():
                return False
            self._cancel_reason = reason or "user"
            self.cancel_event.set()
            return True

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check_active(self) -> None:
        if self.cancel_event.is_set():
            raise ChatRunCancelled(self.cancel_reason or "cancelled")
        if self.expired:
            raise ChatRunDeadlineExceeded("chat run deadline exceeded")


@dataclass
class _RunEntry:
    control: ChatRunControl
    future: Future[Any]
    finish_requested: bool = False


class ChatEventChannel:
    """A bounded cross-thread queue with async consumer wakeups and real backpressure."""

    def __init__(self, maxsize: int = 64) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None

    def _notify_consumer(self) -> None:
        with self._lock:
            loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass

    def publish(self, event: Any, control: ChatRunControl, *, terminal: bool = False) -> bool:
        while True:
            with self._lock:
                if self._closed:
                    return False
            try:
                self._queue.put(event, timeout=min(0.05, max(0.001, control.remaining_seconds())))
            except queue.Full:
                if not terminal:
                    try:
                        control.check_active()
                    except (ChatRunCancelled, ChatRunDeadlineExceeded):
                        return False
                continue
            self._notify_consumer()
            return True

    async def receive(self, timeout: float = 0.1) -> dict[str, Any] | None:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is None:
                self._loop = loop
                self._wake = asyncio.Event()
            elif self._loop is not loop:
                raise RuntimeError("ChatEventChannel cannot be consumed from multiple event loops")
            wake = self._wake
        assert wake is not None
        deadline = loop.time() + timeout
        while True:
            try:
                return self._queue.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                if self._closed:
                    return None
                wake.clear()
            # Recheck after clearing so a producer cannot strand an event between
            # the empty check and awaiting the wakeup signal.
            try:
                return self._queue.get_nowait()
            except queue.Empty:
                pass
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(wake.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def qsize(self) -> int:
        return self._queue.qsize()


class ChatRunRegistry:
    """Admission control for chat workers; capacity is held until work truly exits."""

    def __init__(self, max_workers: int = 4) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="chat-run")
        self._lock = threading.Lock()
        self._runs: dict[str, _RunEntry] = {}
        self._shutdown = False

    def start(self, control: ChatRunControl, target: Callable[[ChatRunControl], Any]) -> bool:
        with self._lock:
            if self._shutdown or control.run_id in self._runs or len(self._runs) >= self.max_workers:
                return False
            try:
                future = self._executor.submit(target, control)
            except RuntimeError:
                return False
            self._runs[control.run_id] = _RunEntry(control, future)
            future.add_done_callback(lambda completed, run_id=control.run_id: self._worker_done(run_id, completed))
            return True

    def _worker_done(self, run_id: str, future: Future[Any]) -> None:
        del future
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is not None and entry.finish_requested:
                self._runs.pop(run_id, None)

    def cancel(self, session_id: str, run_id: str) -> bool:
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None or entry.control.session_id != session_id or entry.future.done():
                return False
            return entry.control.cancel("user") or entry.control.cancelled

    def finish(self, run_id: str) -> None:
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return
            entry.finish_requested = True
            if entry.future.done():
                self._runs.pop(run_id, None)

    def wait(self, run_id: str, timeout: float | None = None) -> bool:
        with self._lock:
            entry = self._runs.get(run_id)
            future = entry.future if entry else None
        if future is None:
            return True
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            return False
        return True

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._runs)

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            self._shutdown = True
            for entry in self._runs.values():
                entry.control.cancel("shutdown")
        self._executor.shutdown(wait=wait, cancel_futures=True)
