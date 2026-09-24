from webapp.chat_models import Scope
from webapp.execution_plan import ExecutionPlan, validate_execution_plan


def test_aggregated_market_plan_uses_weighted_call_cost():
    plan = ExecutionPlan.from_dict({"objective": "复盘", "source_mode": "market_recap",
        "steps": [{"id": "m", "kind": "market_overview", "required": True},
                  {"id": "a", "kind": "answer", "depends_on": ["m"]}], "acceptance": ["来源"]})
    accepted, issues = validate_execution_plan(plan, Scope.whole_corpus(),
        {"market_overview"}, 1, step_costs={"market_overview": 4})
    assert accepted is None
    assert any(issue.code == "external_budget" for issue in issues)


def test_forward_dependency_and_duplicate_answer_are_rejected():
    plan = ExecutionPlan.from_dict({"objective": "x", "source_mode": "external_market",
        "steps": [{"id": "a", "kind": "answer", "depends_on": ["q"]},
                  {"id": "q", "kind": "market_quote"}], "acceptance": ["x"]})
    accepted, issues = validate_execution_plan(plan, Scope.whole_corpus(), {"market_quote"}, 4)
    assert accepted is None
    assert any(issue.code == "forward_dependency" for issue in issues)
