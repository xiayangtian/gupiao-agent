import inspect

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


def test_authoritative_financial_trend_rejects_market_plan():
    plan = ExecutionPlan.from_dict({"objective": "营收趋势", "source_mode": "external_market", "steps": [
        {"id": "quote", "kind": "market_quote"}, {"id": "answer", "kind": "answer"},
    ], "acceptance": ["期间"]})

    assert "authoritative_intent" in inspect.signature(validate_execution_plan).parameters
    valid, issues = validate_execution_plan(
        plan, Scope.whole_corpus(), {"market_quote"}, 1,
        authoritative_intent="financial_trend",
    )

    assert valid is None
    assert any(issue.code == "intent_source_mismatch" for issue in issues)


def test_authoritative_market_trend_accepts_kline_but_rejects_indices():
    plan = ExecutionPlan.from_dict({"objective": "股价近五日走势", "source_mode": "external_market", "steps": [
        {"id": "indices", "kind": "market_indices"}, {"id": "answer", "kind": "answer"},
    ], "acceptance": ["窗口"]})

    assert "authoritative_intent" in inspect.signature(validate_execution_plan).parameters
    valid, issues = validate_execution_plan(
        plan, Scope.whole_corpus(), {"market_indices"}, 1,
        authoritative_intent="market_trend",
    )

    assert valid is None
    assert any(issue.code == "intent_source_mismatch" for issue in issues)


def test_authoritative_market_quote_rejects_rag_even_when_company_scope_exists():
    plan = ExecutionPlan.from_dict({"objective": "最新股价", "source_mode": "local_evidence", "steps": [
        {"id": "retrieve", "kind": "retrieve"}, {"id": "answer", "kind": "answer"},
    ], "acceptance": ["as_of"]})

    assert "authoritative_intent" in inspect.signature(validate_execution_plan).parameters
    valid, issues = validate_execution_plan(
        plan, Scope.company_only("600519", "贵州茅台"), {"retrieve"}, 0,
        authoritative_intent="market_quote",
    )

    assert valid is None
    assert any(issue.code == "intent_source_mismatch" for issue in issues)


def test_market_recap_accepts_server_controlled_market_overview_within_step_limit():
    plan = ExecutionPlan.from_dict({"objective":"今日A股复盘", "source_mode":"market_recap", "steps":[{"id":"overview","kind":"market_overview","required":True},{"id":"answer","kind":"answer","depends_on":["overview"]}], "acceptance":["as_of"]})
    valid, issues = validate_execution_plan(plan, Scope.whole_corpus(), {"market_overview"}, 1)
    assert valid == plan and not issues


def test_recap_plan_cost_matches_indices_mcp_aggregate_and_web_step_budget():
    plan = ExecutionPlan.from_dict({"objective": "上周 A 股复盘", "source_mode": "market_recap", "steps": [
        {"id": "indices", "kind": "market_indices"},
        {"id": "overview", "kind": "market_overview"},
        {"id": "web", "kind": "web_search"},
        {"id": "answer", "kind": "answer", "depends_on": ["indices", "overview", "web"]},
    ], "acceptance": ["披露覆盖"]})
    costs = {"market_indices": 4, "market_overview": 3, "web_search": 1}
    available = {"market_indices", "market_overview", "web_search"}

    valid, issues = validate_execution_plan(plan, Scope.whole_corpus(), available, 8, step_costs=costs)
    assert valid == plan and not issues
    limited, issues = validate_execution_plan(plan, Scope.whole_corpus(), available, 7, step_costs=costs)
    assert limited is None
    assert any(issue.code == "external_budget" for issue in issues)
