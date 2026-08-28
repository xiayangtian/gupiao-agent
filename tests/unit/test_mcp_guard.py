"""MCP 熔断器单元测试：连续失败熔断、冷却后探测恢复、状态统计"""

import time

from webapp.mcp_guard import McpCircuitBreaker


def test_allows_until_threshold():
    b = McpCircuitBreaker(failure_threshold=3, cooldown_seconds=300)
    assert b.allow() is True
    b.record_failure("err1")
    b.record_failure("err2")
    assert b.allow() is True      # 未达阈值
    b.record_failure("err3")
    assert b.allow() is False     # 达阈值 → 熔断
    assert b.status()["circuit"] == "open"


def test_success_resets_breaker():
    b = McpCircuitBreaker(failure_threshold=2, cooldown_seconds=300)
    b.record_failure("e")
    b.record_success()
    assert b.allow() is True
    assert b.status()["consecutive_failures"] == 0


def test_half_open_probe_after_cooldown():
    b = McpCircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
    b.record_failure("e")
    b.record_failure("e")
    assert b.allow() is False
    time.sleep(0.06)
    assert b.allow() is True       # 冷却后允许一次探测
    b.record_success()
    assert b.status()["circuit"] == "closed"


def test_status_counts_and_last_error():
    b = McpCircuitBreaker(failure_threshold=5, cooldown_seconds=300)
    b.record_success()
    b.record_failure("连接超时")
    st = b.status()
    assert st["total_calls"] == 2
    assert st["success_calls"] == 1
    assert st["consecutive_failures"] == 1
    assert st["last_error"] == "连接超时"
    assert st["enabled"] is True
