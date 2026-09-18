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
