"""模型驱动的轻量执行计划器；模型输出不携带任何权限。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from webapp.chat_models import Scope
from webapp.execution_plan import ExecutionPlan, ExecutionStep, PlanIssue, validate_execution_plan


@dataclass(frozen=True)
class PlanningCapabilities:
    available_kinds: set[str]
    max_external_calls: int
    step_costs: Mapping[str, int] = field(default_factory=lambda: {
        "market_overview": 4, "market_indices": 4,
    })


@dataclass(frozen=True)
class PlanningResult:
    status: str
    plan: ExecutionPlan | None = None
    issues: tuple[PlanIssue, ...] = ()


class ExecutionPlanner:
    """将模型的受限 JSON 输出转换为已验证计划。"""
    def __init__(self, json_planner: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None) -> None:
        self._json_planner = json_planner

    def plan(self, question: str, scope: Scope, capabilities: PlanningCapabilities) -> PlanningResult:
        if self._json_planner is None:
            return PlanningResult("fallback", issues=(PlanIssue("planner_unavailable", "执行计划模型当前不可用。"),))
        snapshot = {"scope": scope.to_dict(), "available_steps": sorted(capabilities.available_kinds), "max_external_calls": capabilities.max_external_calls}
        try:
            raw = self._json_planner(question, snapshot)
            candidate = ExecutionPlan.from_dict(raw)
            # 明确公司 + 近期走势必须由服务端补足个股快照与 K 线；模型不能以大盘指数替代。
            trend_words = ("走势", "趋势", "近期", "近来", "最近", "表现")
            if scope.companies and any(word in question for word in trend_words):
                candidate = replace(candidate, source_mode="external_market", steps=(
                    ExecutionStep("quote", "market_quote", True),
                    ExecutionStep("kline", "market_kline", True),
                    ExecutionStep("answer", "answer", True),
                ))
            valid, issues = validate_execution_plan(
                candidate, scope, capabilities.available_kinds, capabilities.max_external_calls,
                step_costs=capabilities.step_costs,
            )
        except Exception as exc:
            # 计划器是可选能力：任何 provider/JSON 失败都必须 fail-closed，不能中断问答。
            return PlanningResult("fallback", issues=(PlanIssue("invalid_plan:" + type(exc).__name__, "执行计划未通过安全校验。"),))
        return PlanningResult("validated", valid, issues) if valid else PlanningResult("fallback", issues=issues)
