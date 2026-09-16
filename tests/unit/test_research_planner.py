from webapp.chat_models import IntentDecision, Scope, ToolPolicy
from webapp.research_models import ResearchPlan, ResearchStep
from webapp.research_planner import ResearchPlanner, validate_plan


def _scope():
    return Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"])


def _policy():
    return ToolPolicy("research_task", allowed_tools=("web_search",), max_calls=1, max_rounds=1)


def test_research_question_builds_retrieve_normalize_compare_verify_answer_plan():
    plan = ResearchPlanner().plan("比较农业银行近三年盈利质量", _scope(), IntentDecision("research_task"), _policy())
    assert [step.kind for step in plan.steps] == ["retrieve", "normalize", "compare", "verify", "answer"]
    assert plan.acceptance


def test_plan_with_out_of_scope_company_is_rejected():
    scope = _scope()
    plan = ResearchPlan("比较", scope, (ResearchStep("r", "retrieve", "检索", report_ids=("600900:2024-12-31:annual",)),), ("核对",))
    valid, issues = validate_plan(plan, scope, _policy())
    assert valid is None
    assert issues[0].code == "scope_violation"


def test_simple_report_fact_returns_none_plan():
    assert ResearchPlanner().plan("营收是多少？", _scope(), IntentDecision("report_fact"), _policy()) is None


def test_unapproved_tool_and_invalid_dependency_are_rejected():
    scope = _scope()
    plan = ResearchPlan("比较", scope, (
        ResearchStep("r", "retrieve", "检索", tools=("unapproved",)),
        ResearchStep("a", "answer", "形成结论", ("r",)),
    ), ("核对",))
    valid, issues = validate_plan(plan, scope, _policy())
    assert valid is None
    assert {issue.code for issue in issues} >= {"tool_policy_violation", "dependency_violation"}
