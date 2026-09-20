from webapp.chat_models import Scope
from webapp.execution_planner import ExecutionPlanner, PlanningCapabilities


def test_planner_accepts_model_market_plan_for_unrecognized_wording():
    planner = ExecutionPlanner(json_planner=lambda *_: {
        "objective": "盘面解读", "source_mode": "external_market",
        "steps": [
            {"id": "quote", "kind": "market_quote", "required": True},
            {"id": "answer", "kind": "answer", "depends_on": ["quote"]},
        ], "acceptance": ["显示截至时间"],
    })
    result = planner.plan("看一下盘面", Scope.whole_corpus(), PlanningCapabilities({"market_quote"}, 1))
    assert result.status == "validated"
    assert [step.kind for step in result.plan.steps] == ["market_quote", "answer"]


def test_planner_falls_back_when_model_requests_unavailable_web():
    planner = ExecutionPlanner(json_planner=lambda *_: {
        "objective": "x", "source_mode": "external_market",
        "steps": [{"id": "web", "kind": "web_search", "required": True}, {"id": "answer", "kind": "answer", "depends_on": ["web"]}],
        "acceptance": ["x"],
    })
    result = planner.plan("x", Scope.whole_corpus(), PlanningCapabilities(set(), 0))
    assert result.plan is None and result.status == "fallback"


def test_company_recent_trend_forces_quote_and_kline_not_indices():
    from webapp.chat_models import CompanyRef, Scope
    planner = ExecutionPlanner(lambda question, snapshot: {
        "objective": question, "source_mode": "external_market",
        "steps": [{"id": "indices", "kind": "market_indices", "required": True}, {"id": "answer", "kind": "answer", "required": True}],
        "acceptance": ["trend"],
    })
    scope = Scope("company_only", (CompanyRef("600900", "长江电力"),), ("600900:2025-12-31:annual",))
    result = planner.plan("分析长江电力近期走势", scope, PlanningCapabilities({"market_quote", "market_kline", "market_indices"}, 2))
    assert result.plan is not None
    assert [step.kind for step in result.plan.steps] == ["market_quote", "market_kline", "answer"]
