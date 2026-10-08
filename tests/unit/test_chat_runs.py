import asyncio
import threading
import time

import pytest

from webapp.chat_runs import (
    ChatEventChannel,
    ChatRunCancelled,
    ChatRunControl,
    ChatRunDeadlineExceeded,
    ChatRunRegistry,
)


def test_run_control_cancel_is_idempotent_and_keeps_first_reason():
    control = ChatRunControl("session-a", "run-a", timeout_seconds=2)

    assert control.cancel("user") is True
    assert control.cancel("disconnect") is False
    assert control.cancelled
    assert control.cancel_reason == "user"
    with pytest.raises(ChatRunCancelled):
        control.check_active()


def test_run_control_deadline_uses_monotonic_time_and_raises():
    control = ChatRunControl("session-a", "run-a", timeout_seconds=0.01)
    time.sleep(0.02)

    assert control.remaining_seconds() == 0
    with pytest.raises(ChatRunDeadlineExceeded):
        control.check_active()


def test_event_channel_applies_backpressure_and_releases_after_receive():
    async def scenario():
        control = ChatRunControl("s", "r", timeout_seconds=2)
        channel = ChatEventChannel(maxsize=1)
        publisher_result = []
        assert channel.publish({"event": 1}, control)

        publisher = threading.Thread(target=lambda: publisher_result.append(
            channel.publish({"event": 2}, control)
        ))
        publisher.start()
        await asyncio.sleep(0.05)
        assert publisher.is_alive(), "second event should wait while the bounded channel is full"
        assert await channel.receive(timeout=0.5) == {"event": 1}
        publisher.join(timeout=1)
        assert publisher_result == [True]
        assert await channel.receive(timeout=0.5) == {"event": 2}
        channel.close()
        assert await channel.receive(timeout=0.1) is None

    asyncio.run(scenario())


def test_event_channel_cancel_unblocks_a_producer_waiting_on_full_queue():
    async def scenario():
        control = ChatRunControl("s", "r", timeout_seconds=3)
        channel = ChatEventChannel(maxsize=1)
        channel.publish({"event": 1}, control)
        publisher_result = []
        publisher = threading.Thread(target=lambda: publisher_result.append(
            channel.publish({"event": 2}, control)
        ))
        publisher.start()
        await asyncio.sleep(0.03)
        control.cancel("user")
        publisher.join(timeout=1)
        assert not publisher.is_alive()
        assert publisher_result == [False]
        channel.close()

    asyncio.run(scenario())


def test_close_wakes_async_consumer_and_rejects_new_events():
    async def scenario():
        channel = ChatEventChannel(maxsize=1)
        waiting = asyncio.create_task(channel.receive(timeout=2))
        await asyncio.sleep(0)
        channel.close()
        assert await asyncio.wait_for(waiting, timeout=0.5) is None
        control = ChatRunControl("s", "r", timeout_seconds=1)
        assert channel.publish({"late": True}, control) is False

    asyncio.run(scenario())


def test_registry_enforces_capacity_and_releases_it_when_run_finishes():
    registry = ChatRunRegistry(max_workers=1)
    started = threading.Event()
    release = threading.Event()

    def blocking(control):
        started.set()
        release.wait(1)

    first = ChatRunControl("s1", "r1", timeout_seconds=2)
    second = ChatRunControl("s2", "r2", timeout_seconds=2)
    assert registry.start(first, blocking)
    assert started.wait(1)
    assert registry.start(second, lambda _control: None) is False
    release.set()
    assert registry.wait("r1", timeout=1)
    registry.finish("r1")
    assert registry.start(second, lambda _control: None)
    assert registry.wait("r2", timeout=1)
    registry.finish("r2")
    assert registry.active_count == 0
    registry.shutdown()


def test_registry_cancel_requires_matching_session_and_active_run():
    registry = ChatRunRegistry(max_workers=1)
    started = threading.Event()
    release = threading.Event()

    def blocking(_control):
        started.set()
        release.wait(1)

    control = ChatRunControl("session-a", "run-a", timeout_seconds=2)
    assert registry.start(control, blocking)
    assert started.wait(1)
    assert registry.cancel("session-b", "run-a") is False
    assert control.cancelled is False
    assert registry.cancel("session-a", "run-a") is True
    assert control.cancelled
    release.set()
    assert registry.wait("run-a", timeout=1)
    assert registry.cancel("session-a", "run-a") is False
    registry.shutdown()
