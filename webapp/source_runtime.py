"""Per-answer source execution contract: authorization, budgets and deduplication."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from threading import Lock
from typing import Any, Callable, Mapping

from webapp.chat_models import EvidenceArtifact, Fact, Scope, ToolArtifact


@dataclass(frozen=True)
class SourceCall:
    provider: str
    operation: str
    category: str  # market / web / local
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category not in {"market", "web", "local"}:
            raise ValueError("category must be market, web, or local")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must not be empty")
        if not isinstance(self.operation, str) or not self.operation.strip():
            raise ValueError("operation must not be empty")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("arguments must be a mapping")
        object.__setattr__(self, "arguments", copy.deepcopy(dict(self.arguments)))


@dataclass(frozen=True)
class SourceCoverage:
    returned_rows: int | None = None
    limit: int | None = None
    total_rows: int | None = None
    query_window: str | None = None
    data_window: str | None = None

    def __post_init__(self) -> None:
        for name in ("returned_rows", "limit", "total_rows"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or null")
        for name in ("query_window", "data_window"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or len(value) > 120):
                raise ValueError(f"{name} must be a bounded string or null")

    def summary(self) -> str:
        parts = []
        if self.returned_rows is not None:
            parts.append(f"返回 {self.returned_rows} 条")
        if self.limit is not None:
            parts.append(f"请求上限 {self.limit} 条")
        if self.total_rows is not None:
            parts.append(f"来源总量 {self.total_rows} 条")
        elif self.limit is not None:
            if self.returned_rows is not None and self.returned_rows >= self.limit:
                parts.append("已达到返回上限，总量未知")
            else:
                parts.append("来源总量未知")
        if self.query_window is not None:
            parts.append(f"来源查询期 {self.query_window}")
        if self.data_window is not None:
            parts.append(f"实际数据期 {self.data_window}")
        return "；".join(parts)[:500]


@dataclass(frozen=True)
class SourceResult:
    call_id: str
    provider: str
    operation: str
    category: str
    status: str
    content: str = ""
    as_of: str = ""
    fetched_at: str = ""
    error_code: str = ""
    payload: Mapping[str, Any] | tuple[Mapping[str, Any], ...] | None = None
    retrieval_hits: tuple[Mapping[str, Any], ...] = ()
    facts: tuple[Fact, ...] = ()
    artifacts: tuple[EvidenceArtifact, ...] = ()
    tool_artifacts: tuple[ToolArtifact, ...] = ()
    coverage: SourceCoverage = SourceCoverage()

    def __post_init__(self) -> None:
        if self.status not in {"success", "partial", "failed", "unavailable"}:
            raise ValueError("unsupported source status")
        if self.category not in {"market", "web", "local"}:
            raise ValueError("unsupported source category")
        if not isinstance(self.coverage, SourceCoverage):
            raise ValueError("coverage must be SourceCoverage")
        if not all(isinstance(value, str) for value in (self.content, self.as_of, self.fetched_at, self.error_code)):
            raise ValueError("source text fields must be strings")
        object.__setattr__(self, "payload", copy.deepcopy(self.payload))
        object.__setattr__(self, "retrieval_hits", tuple(copy.deepcopy(self.retrieval_hits)))
        object.__setattr__(self, "facts", tuple(self.facts))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "tool_artifacts", tuple(self.tool_artifacts))


@dataclass(frozen=True)
class AnswerContext:
    sources: tuple[SourceResult, ...] = ()
    retrieval_hits: tuple[Mapping[str, Any], ...] = ()
    required_missing: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "retrieval_hits", tuple(copy.deepcopy(self.retrieval_hits)))


class CallBudget:
    """Thread-safe application-call counter. Local retrieval is not an external call."""

    def __init__(self, total: int, market: int, web: int) -> None:
        for name, value in (("total", total), ("market", market), ("web", web)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} budget must be a non-negative integer")
        self._limits = {"total": total, "market": market, "web": web}
        self._used = {"total": 0, "market": 0, "web": 0}
        self._lock = Lock()

    def reserve(self, category: str) -> bool:
        if category == "local":
            return True
        if category not in {"market", "web"}:
            raise ValueError("unknown budget category")
        with self._lock:
            if (self._used["total"] >= self._limits["total"] or
                    self._used[category] >= self._limits[category]):
                return False
            self._used["total"] += 1
            self._used[category] += 1
            return True

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._used)


class SourceRuntime:
    """One answer's frozen scope, authorization gate, attempt budget, and result cache."""

    def __init__(self, scope: Scope, budget: CallBudget, authorize: Callable[[SourceCall], bool], *, control: Any = None) -> None:
        if not isinstance(scope, Scope):
            raise ValueError("scope must be a Scope")
        if not isinstance(budget, CallBudget):
            raise ValueError("budget must be a CallBudget")
        if not callable(authorize):
            raise ValueError("authorize must be callable")
        self.scope = scope
        self.budget = budget
        self.authorize = authorize
        self.control = control
        self._guard = Lock()
        self._locks: dict[str, Lock] = {}
        self._cache: dict[str, SourceResult] = {}
        self._sequence = 0

    def _key(self, call: SourceCall) -> str:
        return json.dumps({
            "scope": self.scope.to_dict(),
            "provider": call.provider,
            "operation": call.operation,
            "category": call.category,
            "arguments": call.arguments,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)

    def _next_id(self) -> str:
        with self._guard:
            self._sequence += 1
            return f"source-{self._sequence}"

    def unavailable(self, call: SourceCall, code: str) -> SourceResult:
        return SourceResult(self._next_id(), call.provider, call.operation, call.category,
                            "unavailable", error_code=code)

    def call(self, call: SourceCall, invoke: Callable[[], SourceResult]) -> SourceResult:
        if self.control is not None:
            self.control.check_active()
        if not isinstance(call, SourceCall) or not callable(invoke):
            raise ValueError("call must be SourceCall and invoke must be callable")
        try:
            permitted = bool(self.authorize(call))
        except Exception:
            permitted = False
        if not permitted:
            return self.unavailable(call, "not_authorized")
        key = self._key(call)
        with self._guard:
            lock = self._locks.setdefault(key, Lock())
        with lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            if not self.budget.reserve(call.category):
                return self.unavailable(call, "budget_exhausted")
            try:
                result = invoke()
                if not isinstance(result, SourceResult):
                    raise TypeError("source invoker must return SourceResult")
                # Identity fields always come from the server-created SourceCall.
                result = replace(result, call_id=self._next_id(), provider=call.provider,
                                 operation=call.operation, category=call.category)
            except Exception:
                result = SourceResult(self._next_id(), call.provider, call.operation,
                                      call.category, "failed", error_code="source_failed")
            self._cache[key] = result
            return result
