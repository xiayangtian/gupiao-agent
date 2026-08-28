"""webapp.mcp_guard — MCP 工具调用熔断器。

连续失败达到阈值后「熔断」：问答不再注入/调用 MCP 工具（避免每次问答都
尝试一个已不可用的服务）；冷却期过后自动进入 half-open 探测，成功一次即
恢复，失败则继续熔断。同时记录调用统计供状态检查使用。
"""

import threading
import time
from typing import Any, Dict, Optional


class McpCircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 300,
    ) -> None:
        self._lock = threading.Lock()
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.consecutive_failures = 0
        self.total_calls = 0
        self.success_calls = 0
        self.last_error: Optional[str] = None
        self.open_since: Optional[float] = None

    def allow(self) -> bool:
        """是否允许发起 MCP 调用：熔断中且未到冷却期 → False；否则 True"""
        with self._lock:
            if self.consecutive_failures < self.failure_threshold:
                return True
            # 熔断中：冷却期过后放行一次探测（half-open）
            if self.open_since is not None:
                return time.time() - self.open_since >= self.cooldown_seconds
            return True

    def record_success(self) -> None:
        with self._lock:
            self.total_calls += 1
            self.success_calls += 1
            self.consecutive_failures = 0
            self.open_since = None

    def record_failure(self, error: Any) -> None:
        with self._lock:
            self.total_calls += 1
            self.consecutive_failures += 1
            self.last_error = str(error)[:200]
            if self.consecutive_failures >= self.failure_threshold:
                if self.open_since is None:
                    self.open_since = time.time()

    @property
    def is_open(self) -> bool:
        """熔断是否打开（连续失败达阈值）"""
        with self._lock:
            return self.consecutive_failures >= self.failure_threshold

    def status(self) -> Dict[str, Any]:
        """熔断器状态快照（供状态检查/前端展示）"""
        with self._lock:
            circuit = "open"
            if self.consecutive_failures < self.failure_threshold:
                circuit = "closed"
            elif self.open_since is not None and time.time() - self.open_since >= self.cooldown_seconds:
                circuit = "half_open"
            return {
                "enabled": True,
                "circuit": circuit,
                "consecutive_failures": self.consecutive_failures,
                "failure_threshold": self.failure_threshold,
                "total_calls": self.total_calls,
                "success_calls": self.success_calls,
                "last_error": self.last_error,
                "cooldown_seconds": self.cooldown_seconds,
                "open_since": self.open_since,
            }
