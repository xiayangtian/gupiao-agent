"""Fail-closed intent routing and external-tool policy for trusted chat M2."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from webapp.chat_models import IntentDecision, Scope, SourcePolicy, ToolPolicy


@dataclass(frozen=True)
class ToolAvailability:
    """Only currently usable, non-circuit-open provider tool names."""
    names: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def available(cls, *names: str) -> "ToolAvailability":
        return cls(frozenset(name for name in names if isinstance(name, str) and name))

    def permits(self, name: str) -> bool:
        return name in self.names


class IntentRouter:
    _REALTIME = ("今天", "最新", "近期", "实时", "涨跌")
    _EVENT = ("新闻", "公告", "异动", "原因")
    _INDUSTRY = ("行业", "同业", "竞争", "排名", "对比")
    _RESEARCH = ("研究计划", "研究任务", "分步骤", "调研", "研究一下")
    _TREND = ("同比", "环比", "多年", "历年", "变化", "趋势", "近三年", "近两年")

    def classify(self, question: str, scope: Scope) -> IntentDecision:
        # Scope is deliberately accepted now: classifiers must not be reusable as a
        # bypass around scope resolution. It is not a privilege source.
        del scope
        text = question or ""
        if any(word in text for word in self._REALTIME):
            return IntentDecision("realtime_market", "high", True, True, True)
        if any(word in text for word in self._EVENT):
            return IntentDecision("event_attribution", "high", True, True, True)
        if any(word in text for word in self._INDUSTRY):
            return IntentDecision("industry_benchmark", "high", True)
        if any(word in text for word in self._RESEARCH):
            return IntentDecision("research_task", "high", True)
        if any(word in text for word in self._TREND):
            return IntentDecision("company_trend", "medium", True)
        return IntentDecision("report_fact", "medium", True)


class ToolPolicyResolver:
    def __init__(self, timeout_seconds: int = 30) -> None:
        self.timeout_seconds = min(30, max(1, int(timeout_seconds)))

    def resolve(self, decision: IntentDecision, scope: Scope, availability: ToolAvailability) -> ToolPolicy:
        del scope
        allowed: Iterable[str] = ()
        fallback = "请基于本地可核验财报披露回答。"
        if decision.intent == "realtime_market":
            allowed = ("get_quote", "web_search")
            fallback = "实时数据暂不可用；请以本地披露为准。"
        elif decision.intent == "event_attribution":
            allowed = ("web_search", "get_quote")
            fallback = "外部事件来源暂不可用；不能确认归因。"
        elif decision.intent == "research_task":
            fallback = "详细研究规划将在 M3 提供；当前仅能进行本地查证。"
        names = tuple(name for name in allowed if availability.permits(name))
        external = bool(names)
        return ToolPolicy(
            intent=decision.intent,
            allowed_tools=names,
            max_calls=3 if external else 0,
            max_rounds=2 if external else 0,
            timeout_seconds=self.timeout_seconds,
            source_policy=SourcePolicy(local_pdf=True, historical_analysis=True, market_data=external, web="web_search" in names),
            fallback_message=fallback,
        )
