from webapp.chat_models import Scope
from webapp.execution_plan import ExecutionPlan, validate_execution_plan


def test_general_web_rejects_rag_and_market_steps():
    plan = ExecutionPlan.from_dict({"objective":"天气", "source_mode":"general_web", "steps":[{"id":"r","kind":"retrieve"},{"id":"a","kind":"answer"}], "acceptance":["来源"]})
    _, issues = validate_execution_plan(plan, Scope.whole_corpus(), {"retrieve"}, 0)
    assert any(issue.code == "general_web_boundary" for issue in issues)


def test_market_recap_rejects_rag_and_accepts_indices_and_breadth():
    plan = ExecutionPlan.from_dict({"objective":"A股周复盘", "source_mode":"market_recap", "steps":[{"id":"i","kind":"market_indices"},{"id":"b","kind":"market_breadth"},{"id":"a","kind":"answer"}], "acceptance":["as_of"]})
    valid, issues = validate_execution_plan(plan, Scope.whole_corpus(), {"market_indices", "market_breadth"}, 2)
    assert valid == plan and not issues


def test_market_recap_accepts_server_controlled_market_overview_within_step_limit():
    plan = ExecutionPlan.from_dict({"objective":"今日A股复盘", "source_mode":"market_recap", "steps":[{"id":"overview","kind":"market_overview","required":True},{"id":"answer","kind":"answer","depends_on":["overview"]}], "acceptance":["as_of"]})
    valid, issues = validate_execution_plan(plan, Scope.whole_corpus(), {"market_overview"}, 1)
    assert valid == plan and not issues
