"""Fail-closed intent routing and external-tool policy for trusted chat M2."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from financial_report_fetcher.rag.mcp_tools import (
    WEB_SEARCH_TOOL_FAMILY,
    REALTIME_QUOTE_TOOL_FAMILY,
    is_realtime_quote_tool,
    is_web_search_tool,
)
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
    """按意图把可允许的工具家族解析为实际可调用的 provider 工具名。

    允许集不是写死的工具名，而是「家族 × 当前可用工具名」的交集，两者都与
    provider 定义同源，因此新增加/改名行情工具后策略仍能授权，也不会授权未列出的
    家族（如财务指标、报表工具）。
    """

    # 家族谓词：以 provider 工具名定义为准（mcp_tools）
    _FAMILIES: dict[str, Callable[[str], bool]] = {
        REALTIME_QUOTE_TOOL_FAMILY: is_realtime_quote_tool,
        WEB_SEARCH_TOOL_FAMILY: is_web_search_tool,
    }

    # 意图 → 允许的工具家族（顺序即允许集顺序，对提示与展示都确定）
    _INTENT_FAMILIES: dict[str, tuple[str, ...]] = {
        "realtime_market": (REALTIME_QUOTE_TOOL_FAMILY, WEB_SEARCH_TOOL_FAMILY),
        "event_attribution": (WEB_SEARCH_TOOL_FAMILY, REALTIME_QUOTE_TOOL_FAMILY),
    }

    def __init__(self, timeout_seconds: int = 30) -> None:
        self.timeout_seconds = min(30, max(1, int(timeout_seconds)))

    @classmethod
    def _names_in_families(cls, availability: ToolAvailability, families: tuple[str, ...]) -> tuple[str, ...]:
        names: list[str] = []
        for family in families:
            matches = cls._FAMILIES.get(family)
            if matches is None:
                continue
            for name in sorted(availability.names):
                if name not in names and matches(name):
                    names.append(name)
        return tuple(names)

    def resolve(self, decision: IntentDecision, scope: Scope, availability: ToolAvailability) -> ToolPolicy:
        del scope
        fallback = "请基于本地可核验财报披露回答。"
        if decision.intent == "realtime_market":
            fallback = "实时数据暂不可用；请以本地披露为准。"
        elif decision.intent == "event_attribution":
            fallback = "外部事件来源暂不可用；不能确认归因。"
        elif decision.intent == "research_task":
            fallback = "研究将严格按当前范围与工具策略执行；无可用来源时会明确说明限制。"

        names = self._names_in_families(
            availability, self._INTENT_FAMILIES.get(decision.intent, ()),
        )
        market_data = any(is_realtime_quote_tool(name) for name in names)
        web = any(is_web_search_tool(name) for name in names)
        external = bool(names)
        return ToolPolicy(
            intent=decision.intent,
            allowed_tools=names,
            max_calls=3 if external else 0,
            max_rounds=2 if external else 0,
            timeout_seconds=self.timeout_seconds,
            source_policy=SourcePolicy(
                local_pdf=True, historical_analysis=True, market_data=market_data, web=web,
            ),
            fallback_message=fallback,
        )
