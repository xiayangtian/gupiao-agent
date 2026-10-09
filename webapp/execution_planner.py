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
        "market_overview": 3, "market_indices": 4,
    })


@dataclass(frozen=True)
class PlanningResult:
    status: str
    plan: ExecutionPlan | None = None
    issues: tuple[PlanIssue, ...] = ()


def safe_market_plan(
    intent: str, available_kinds: set[str], max_external_calls: int,
    *, step_costs: Mapping[str, int] | None = None,
) -> ExecutionPlan | None:
    """Build a deterministic plan for the frozen market intent, never adding PDF retrieval."""
    if isinstance(max_external_calls, bool) or not isinstance(max_external_calls, int) or max_external_calls < 0:
        return None
    costs = {"market_indices": 4, "market_overview": 3, **dict(step_costs or {})}
    contracts = {
        "market_quote": ("external_market", "market_quote"),
        "market_trend": ("external_market", "market_kline"),
        "market_recap": ("market_recap", "market_indices"),
    }
    contract = contracts.get(intent)
    if contract is None:
        return None
    source_mode, required_kind = contract
    if required_kind not in available_kinds:
        return None
    steps: list[ExecutionStep] = [ExecutionStep(required_kind, required_kind, True)]
    if (intent == "market_recap" and "market_overview" in available_kinds
            and max_external_calls >= costs.get("market_indices", 4) + costs.get("market_overview", 3)):
        steps.append(ExecutionStep("overview", "market_overview", True, (required_kind,)))
    answer_dependencies = tuple(step.id for step in steps)
    steps.append(ExecutionStep("answer", "answer", True, answer_dependencies))
    plan = ExecutionPlan(
        objective="按已确认的行情对象和时间窗口分析",
        source_mode=source_mode,
        steps=tuple(steps),
        acceptance=("仅依据已取得且与请求对象/时间匹配的市场证据作答",),
    )
    scope = Scope.company_only("000000", "company") if intent in {"market_quote", "market_trend"} else Scope.whole_corpus()
    valid, _ = validate_execution_plan(
        plan, scope, available_kinds, max_external_calls,
        step_costs=costs, authoritative_intent=intent,
    )
    return valid


class ExecutionPlanner:
    """将模型的受限 JSON 输出转换为已验证计划。"""
    def __init__(self, json_planner: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None) -> None:
        self._json_planner = json_planner

    def plan(
        self, question: str, scope: Scope, capabilities: PlanningCapabilities,
        *, authoritative_intent: str | None = None,
        authoritative_window: Mapping[str, Any] | None = None,
    ) -> PlanningResult:
        if self._json_planner is None:
            return PlanningResult("fallback", issues=(PlanIssue("planner_unavailable", "执行计划模型当前不可用。"),))
        snapshot = {"scope": scope.to_dict(), "available_steps": sorted(capabilities.available_kinds), "max_external_calls": capabilities.max_external_calls}
        if authoritative_intent is not None:
            snapshot["authoritative_intent"] = authoritative_intent
        if authoritative_window is not None:
            snapshot["authoritative_window"] = dict(authoritative_window)
        try:
            raw = self._json_planner(question, snapshot)
            candidate = ExecutionPlan.from_dict(raw)
            # 明确公司 + 近期走势必须由服务端补足个股快照与 K 线；模型不能以大盘指数替代。
            trend_words = ("走势", "趋势", "近期", "近来", "最近", "表现")
            if authoritative_intent is None and scope.companies and any(word in question for word in trend_words):
                candidate = replace(candidate, source_mode="external_market", steps=(
                    ExecutionStep("quote", "market_quote", True),
                    ExecutionStep("kline", "market_kline", True),
                    ExecutionStep("answer", "answer", True),
                ))
            valid, issues = validate_execution_plan(
                candidate, scope, capabilities.available_kinds, capabilities.max_external_calls,
                step_costs=capabilities.step_costs,
                authoritative_intent=authoritative_intent,
            )
        except Exception as exc:
            # 计划器是可选能力：任何 provider/JSON 失败都必须 fail-closed，不能中断问答。
            return PlanningResult("fallback", issues=(PlanIssue("invalid_plan:" + type(exc).__name__, "执行计划未通过安全校验。"),))
        return PlanningResult("validated", valid, issues) if valid else PlanningResult("fallback", issues=issues)
