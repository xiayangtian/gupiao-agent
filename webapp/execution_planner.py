"""模型驱动的轻量执行计划器；模型输出不携带任何权限。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from webapp.chat_models import Scope
from webapp.execution_plan import ExecutionPlan, PlanIssue, validate_execution_plan


@dataclass(frozen=True)
class PlanningCapabilities:
    available_kinds: set[str]
    max_external_calls: int


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
            valid, issues = validate_execution_plan(candidate, scope, capabilities.available_kinds, capabilities.max_external_calls)
        except (TypeError, ValueError):
            return PlanningResult("fallback", issues=(PlanIssue("invalid_plan", "执行计划未通过安全校验。"),))
        return PlanningResult("validated", valid, issues) if valid else PlanningResult("fallback", issues=issues)
