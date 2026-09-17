"""Fail-closed planner for bounded M3 research plans."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from webapp.chat_models import IntentDecision, Scope, ToolPolicy
from webapp.research_models import ResearchPlan, ResearchStep


@dataclass(frozen=True)
class PlanningIssue:
    code: str
    message: str


_ALLOWED_DEPENDENCIES = {
    "retrieve": frozenset(),
    "tool": frozenset(),
    "normalize": frozenset(("retrieve", "tool")),
    "compare": frozenset(("normalize",)),
    "verify": frozenset(("retrieve", "normalize", "compare", "tool")),
    "answer": frozenset(("verify",)),
}


def _issue(code: str, message: str) -> PlanningIssue:
    return PlanningIssue(code, message)


def validate_plan(plan: ResearchPlan, scope: Scope, policy: ToolPolicy) -> tuple[ResearchPlan | None, tuple[PlanningIssue, ...]]:
    """Return the plan only when it can neither widen Scope nor ToolPolicy."""
    issues: list[PlanningIssue] = []
    if plan.scope != scope:
        issues.append(_issue("scope_violation", "研究计划不得变更已冻结范围。"))
    by_id = {step.id: step for step in plan.steps}
    if len(plan.steps) > 8:
        issues.append(_issue("plan_too_long", "研究计划最多八步。"))
    for step in plan.steps:
        if any(report_id not in scope.report_ids for report_id in step.report_ids):
            issues.append(_issue("scope_violation", "步骤引用了范围外报告。"))
        if any(tool not in policy.allowed_tools for tool in step.tools):
            issues.append(_issue("tool_policy_violation", "步骤请求了策略未授权工具。"))
        allowed_kinds = _ALLOWED_DEPENDENCIES[step.kind]
        for dependency in step.depends_on:
            predecessor = by_id.get(dependency)
            if predecessor is None:
                issues.append(_issue("dependency_violation", "步骤依赖不存在。"))
            elif predecessor.kind not in allowed_kinds:
                issues.append(_issue("dependency_violation", "步骤依赖不符合类型顺序。"))
    # Directed cycle detection, including self-dependencies.
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(step_id: str) -> None:
        if step_id in visiting:
            issues.append(_issue("dependency_cycle", "研究步骤不能形成循环依赖。"))
            return
        if step_id in visited or step_id not in by_id:
            return
        visiting.add(step_id)
        for dependency in by_id[step_id].depends_on:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)
    for step in plan.steps:
        visit(step.id)
    return (None, tuple(issues)) if issues else (plan, ())


class ResearchPlanner:
    """Generates a deterministic plan or validates an injected JSON plan.

    The optional callback is intentionally narrow: it receives no authority object
    and its JSON is validated against the original Scope and ToolPolicy afterwards.
    """
    def __init__(self, json_planner: Callable[[str], str | Mapping[str, Any]] | None = None) -> None:
        self._json_planner = json_planner
        self.last_awaiting_input = ""

    @staticmethod
    def _requested(question: str, intent: IntentDecision) -> bool:
        return intent.intent == "research_task" or "制定研究计划" in (question or "")

    def plan(self, question: str, scope: Scope, intent: IntentDecision, policy: ToolPolicy) -> ResearchPlan | None:
        self.last_awaiting_input = ""
        if not self._requested(question, intent):
            return None
        if not scope.report_ids and scope.mode != "whole_corpus":
            self.last_awaiting_input = "需要至少一份本地已索引财报及明确报告期间。"
            return None
        candidate = self._from_json(question, scope)
        if candidate is not None:
            valid, _issues = validate_plan(candidate, scope, policy)
            if valid is not None:
                return valid
        return self._deterministic_plan(question, scope)

    def _from_json(self, question: str, scope: Scope) -> ResearchPlan | None:
        if self._json_planner is None:
            return None
        try:
            raw = self._json_planner(question)
            data = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(data, Mapping):
                return None
            # Scope is never accepted from model output; installing the caller's
            # frozen scope makes accidental scope expansion impossible by design.
            payload = dict(data)
            payload["scope"] = scope.to_dict()
            return ResearchPlan.from_dict(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _deterministic_plan(question: str, scope: Scope) -> ResearchPlan:
        reports = tuple(scope.report_ids)
        return ResearchPlan(
            objective=(question or "研究问题").strip(), scope=scope,
            steps=(
                ResearchStep("retrieve", "retrieve", "检索已授权披露", report_ids=reports, acceptance=("取得范围内来源",)),
                ResearchStep("normalize", "normalize", "整理可核验事实", ("retrieve",), ("统一期间与口径",)),
                ResearchStep("compare", "compare", "比较关键指标", ("normalize",), ("完成范围内比较",)),
                ResearchStep("verify", "verify", "核对来源与结论", ("compare",), ("核对来源",)),
                ResearchStep("answer", "answer", "形成研究结论", ("verify",), ("披露限制与结果",)),
            ),
            acceptance=("核对来源", "说明范围和数据限制"),
        )
