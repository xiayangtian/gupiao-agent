from webapp.chat_models import Scope
from webapp.execution_executor import ExecutionExecutor
from webapp.execution_plan import ExecutionPlan
from webapp.source_runtime import SourceResult


def test_executor_preserves_finished_step_and_skips_following_steps_after_cancel():
    from webapp.chat_runs import ChatRunControl

    control = ChatRunControl("session", "run", timeout_seconds=10)
    calls = []
    plan = ExecutionPlan.from_dict({"objective": "行情", "source_mode": "external_market",
        "steps": [{"id": "quote", "kind": "market_quote", "required": True},
                  {"id": "web", "kind": "web_search", "required": False},
                  {"id": "answer", "kind": "answer"}], "acceptance": ["来源"]})

    def quote(*_args):
        calls.append("quote")
        control.cancel("user")
        return {"price": 10}

    result = ExecutionExecutor(market_quote=quote,
        web_search=lambda *_: calls.append("web")).execute(
            plan, "行情", Scope.whole_corpus(), control=control)
    assert calls == ["quote"]
    assert [(step.id, step.status) for step in result.steps] == [("quote", "completed")]


def test_executor_emits_sanitized_step_lifecycle_callbacks():
    plan = ExecutionPlan.from_dict({"objective": "行情", "source_mode": "external_market",
        "steps": [{"id": "quote", "kind": "market_quote", "required": True},
                  {"id": "answer", "kind": "answer"}], "acceptance": ["来源"]})
    events = []
    result = ExecutionExecutor(market_quote=lambda *_: {"price": 10}).execute(
        plan, "行情", Scope.whole_corpus(),
        on_step=lambda phase, step: events.append((phase, step.id, step.kind, step.status)),
    )
    assert result.steps[0].status == "completed"
    assert events == [("started", "quote", "market_quote", "running"),
                      ("completed", "quote", "market_quote", "completed")]


def test_market_plan_never_calls_retrieve():
    calls = []
    plan = ExecutionPlan.from_dict({"objective": "今日行情", "source_mode": "external_market",
        "steps": [{"id": "q", "kind": "market_quote", "required": True},
                  {"id": "w", "kind": "web_search", "depends_on": ["q"]},
                  {"id": "a", "kind": "answer", "depends_on": ["q", "w"]}], "acceptance": ["时间"]})
    result = ExecutionExecutor(retrieve=lambda *_: calls.append("retrieve"),
        market_quote=lambda *_: calls.append("quote"), web_search=lambda *_: calls.append("web")
    ).execute(plan, "分析今日行情", Scope.whole_corpus())
    assert calls == ["quote", "web"]
    assert result.source_summary["local_pdf"] == "未使用"


def test_required_failure_leaves_answer_pending_and_requires_limitation():
    plan = ExecutionPlan.from_dict({"objective": "复盘", "source_mode": "market_recap",
        "steps": [{"id": "m", "kind": "market_overview", "required": True},
                  {"id": "a", "kind": "answer", "depends_on": ["m"]}], "acceptance": ["来源"]})
    def missing(question, scope):
        return SourceResult("m1", "fixture", "overview", "market", "failed", error_code="timeout")
    result = ExecutionExecutor(market_overview=missing).execute(plan, "复盘", Scope.whole_corpus())
    assert result.context.required_missing
    assert result.steps[-1].status == "pending"
    assert result.source_summary["market_data"] == "获取失败"


def test_required_source_partial_success_keeps_usable_results_and_allows_limited_answer():
    plan = ExecutionPlan.from_dict({"objective": "上周复盘", "source_mode": "market_recap",
        "steps": [{"id": "m", "kind": "market_overview", "required": True},
                  {"id": "a", "kind": "answer", "depends_on": ["m"]}], "acceptance": ["披露缺项"]})

    def partial(_question, _scope):
        return (
            SourceResult("ok", "fixture", "market", "market", "success", as_of="2026-09-24"),
            SourceResult("bad", "fixture", "market", "market", "failed", error_code="timeout"),
        )

    result = ExecutionExecutor(market_overview=partial).execute(plan, "上周复盘", Scope.whole_corpus())

    assert result.steps[0].status == "partial"
    assert result.steps[-1].status == "completed"
    assert result.context.required_missing is False
    assert result.source_summary["market_data"] == "部分取得"
