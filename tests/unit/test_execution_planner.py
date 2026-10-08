import inspect

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


def test_authoritative_financial_trend_keeps_report_retrieval_instead_of_market_override():
    planner = ExecutionPlanner(lambda question, snapshot: {
        "objective": question, "source_mode": "local_evidence",
        "steps": [{"id": "retrieve", "kind": "retrieve", "required": True},
                  {"id": "answer", "kind": "answer", "required": True}],
        "acceptance": ["报告期间"],
    })
    scope = Scope.company_only("600900", "长江电力", ["600900:2025-12-31:annual"])

    assert "authoritative_intent" in inspect.signature(ExecutionPlanner.plan).parameters
    result = planner.plan(
        "长江电力2025年营收趋势", scope,
        PlanningCapabilities({"retrieve", "market_quote", "market_kline"}, 2),
        authoritative_intent="financial_trend",
    )

    assert result.plan is not None
    assert [step.kind for step in result.plan.steps] == ["retrieve", "answer"]


def test_authoritative_window_is_visible_to_planner_but_not_model_selectable():
    snapshots = []
    planner = ExecutionPlanner(lambda question, snapshot: (
        snapshots.append(dict(snapshot)) or {
            "objective": question, "source_mode": "external_market",
            "steps": [{"id": "kline", "kind": "market_kline"}, {"id": "answer", "kind": "answer"}],
            "acceptance": ["same window"],
        }
    ))
    scope = Scope.company_only("600900", "长江电力", ())
    window = {"label": "上周", "start_date": "2026-09-28", "end_date": "2026-10-04"}

    assert "authoritative_window" in inspect.signature(ExecutionPlanner.plan).parameters
    result = planner.plan(
        "长江电力上周股价走势", scope,
        PlanningCapabilities({"market_kline"}, 1),
        authoritative_intent="market_trend", authoritative_window=window,
    )

    assert result.plan is not None
    assert snapshots[0]["authoritative_intent"] == "market_trend"
    assert snapshots[0]["authoritative_window"] == window


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
