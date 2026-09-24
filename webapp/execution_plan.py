"""受限智能问答执行计划的不可变契约与服务端校验。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from webapp.chat_models import Scope

StepKind = Literal["retrieve", "market_quote", "market_kline", "market_indices", "market_breadth", "sector_performance", "market_fund_flow", "market_overview", "web_search", "answer"]
SourceMode = Literal["local_evidence", "external_market", "market_recap", "general_web", "mixed"]

_STEP_KINDS = frozenset(("retrieve", "market_quote", "market_kline", "market_indices", "market_breadth", "sector_performance", "market_fund_flow", "market_overview", "web_search", "answer"))
_SOURCE_MODES = frozenset(("local_evidence", "external_market", "market_recap", "general_web", "mixed"))


@dataclass(frozen=True)
class PlanIssue:
    code: str
    message: str


@dataclass(frozen=True)
class ExecutionStep:
    id: str
    kind: StepKind
    required: bool = False
    depends_on: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionStep":
        if not isinstance(data, Mapping):
            raise ValueError("execution step must be an object")
        unknown = set(data) - {"id", "kind", "required", "depends_on"}
        if unknown:
            raise ValueError("unsupported execution step fields")
        step_id, kind = data.get("id"), data.get("kind")
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError("execution step id must be non-empty")
        if kind not in _STEP_KINDS:
            raise ValueError("unsupported execution step")
        required = data.get("required", False)
        depends_on = data.get("depends_on", [])
        if not isinstance(required, bool) or not isinstance(depends_on, (list, tuple)) or not all(isinstance(v, str) and v for v in depends_on):
            raise ValueError("invalid execution step fields")
        return cls(step_id, kind, required, tuple(depends_on))


@dataclass(frozen=True)
class ExecutionPlan:
    objective: str
    source_mode: SourceMode
    steps: tuple[ExecutionStep, ...]
    acceptance: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionPlan":
        if not isinstance(data, Mapping):
            raise ValueError("execution plan must be an object")
        unknown = set(data) - {"objective", "source_mode", "steps", "acceptance"}
        if unknown:
            raise ValueError("unsupported execution plan fields")
        objective, source_mode = data.get("objective"), data.get("source_mode")
        steps, acceptance = data.get("steps"), data.get("acceptance")
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("execution objective must be non-empty")
        if source_mode not in _SOURCE_MODES:
            raise ValueError("unsupported execution source mode")
        if not isinstance(steps, (list, tuple)) or not isinstance(acceptance, (list, tuple)):
            raise ValueError("execution plan steps and acceptance must be arrays")
        if not acceptance or not all(isinstance(item, str) and item.strip() for item in acceptance):
            raise ValueError("execution plan acceptance must be non-empty strings")
        return cls(objective.strip(), source_mode, tuple(ExecutionStep.from_dict(item) for item in steps), tuple(acceptance))


def validate_execution_plan(plan: ExecutionPlan, scope: Scope, available_kinds: set[str], max_external_calls: int) -> tuple[ExecutionPlan | None, tuple[PlanIssue, ...]]:
    """校验计划只使用服务端可用来源，且拥有有限、无环的步骤图。"""
    del scope  # Scope 由调用方冻结；本契约不接受模型传入的范围字段。
    issues: list[PlanIssue] = []
    if not 1 <= len(plan.steps) <= 4:
        issues.append(PlanIssue("step_limit", "普通问答计划必须包含一至四步。"))
    ids = [step.id for step in plan.steps]
    if len(set(ids)) != len(ids):
        issues.append(PlanIssue("duplicate_step", "计划步骤 ID 不能重复。"))
    if not plan.steps or plan.steps[-1].kind != "answer":
        issues.append(PlanIssue("missing_answer", "计划必须以最终回答步骤结束。"))
    if plan.source_mode == "general_web" and any(step.kind not in {"web_search", "answer"} for step in plan.steps):
        issues.append(PlanIssue("general_web_boundary", "非股票问题只允许网页搜索和模型回答。"))
    if plan.source_mode == "market_recap" and "retrieve" in {step.kind for step in plan.steps}:
        issues.append(PlanIssue("market_recap_boundary", "A 股复盘默认不得检索财报。"))
    external = 0
    by_id = {step.id: step for step in plan.steps}
    for step in plan.steps:
        if step.kind != "answer" and step.kind not in available_kinds:
            issues.append(PlanIssue("unavailable_step", "计划请求的来源当前不可用。"))
        if step.kind in {"market_quote", "market_kline", "market_indices", "market_breadth", "sector_performance", "market_fund_flow", "market_overview", "web_search"}:
            external += 1
        if any(dep not in by_id for dep in step.depends_on):
            issues.append(PlanIssue("unknown_dependency", "计划步骤依赖不存在。"))
    if external > max_external_calls:
        issues.append(PlanIssue("external_budget", "计划超过外部来源调用预算。"))
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(step_id: str) -> None:
        if step_id in visiting:
            issues.append(PlanIssue("dependency_cycle", "计划步骤不能形成循环依赖。"))
            return
        if step_id in visited:
            return
        visiting.add(step_id)
        for dep in by_id[step_id].depends_on:
            if dep in by_id:
                visit(dep)
        visiting.remove(step_id)
        visited.add(step_id)
    for step in plan.steps:
        visit(step.id)
    return (None, tuple(issues)) if issues else (plan, ())
