import pytest

from webapp.chat_models import Scope
from webapp.execution_plan import ExecutionPlan, validate_execution_plan


def _market_plan():
    return ExecutionPlan.from_dict({
        "objective": "分析今日行情",
        "source_mode": "external_market",
        "steps": [
            {"id": "quote", "kind": "market_quote", "required": True},
            {"id": "news", "kind": "web_search", "required": False, "depends_on": ["quote"]},
            {"id": "answer", "kind": "answer", "depends_on": ["quote", "news"]},
        ],
        "acceptance": ["标注数据截至时间"],
    })


def test_validator_accepts_market_then_web_then_answer_without_retrieve():
    plan = _market_plan()
    valid, issues = validate_execution_plan(
        plan, Scope.whole_corpus(), {"market_quote", "web_search"}, 2,
    )
    assert valid == plan
    assert issues == ()


def test_plan_rejects_unknown_fields_and_unknown_step_kind():
    with pytest.raises(ValueError, match="unsupported execution step"):
        ExecutionPlan.from_dict({
            "objective": "x", "source_mode": "external_market",
            "steps": [{"id": "x", "kind": "unsafe"}],
            "acceptance": ["x"],
        })

    with pytest.raises(ValueError, match="unsupported execution plan fields"):
        ExecutionPlan.from_dict({
            "objective": "x", "source_mode": "local_evidence", "steps": [],
            "acceptance": ["x"], "reasoning": "must not persist",
        })


def test_validator_rejects_plan_without_terminal_answer_or_with_unavailable_step():
    plan = ExecutionPlan.from_dict({
        "objective": "x", "source_mode": "external_market",
        "steps": [{"id": "quote", "kind": "market_quote", "required": True}],
        "acceptance": ["x"],
    })
    valid, issues = validate_execution_plan(plan, Scope.whole_corpus(), set(), 0)
    assert valid is None
    assert {issue.code for issue in issues} >= {"missing_answer", "unavailable_step"}
