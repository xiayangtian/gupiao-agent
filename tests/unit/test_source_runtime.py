from concurrent.futures import ThreadPoolExecutor

import pytest


def test_budget_counts_physical_attempts_and_keeps_local_free():
    from webapp.source_runtime import CallBudget

    budget = CallBudget(total=4, market=4, web=2)
    assert all(budget.reserve("market") for _ in range(4))
    assert not budget.reserve("web")
    assert budget.reserve("local")
    assert budget.snapshot() == {"total": 4, "market": 4, "web": 0}


def test_duplicate_success_invokes_provider_once():
    from webapp.chat_models import Scope
    from webapp.source_runtime import CallBudget, SourceCall, SourceResult, SourceRuntime

    runtime = SourceRuntime(Scope.whole_corpus(), CallBudget(3, 3, 3), lambda call: True)
    call = SourceCall("fixture", "quote", "market", {"symbol": "600900"})
    invocations = []

    def invoke():
        invocations.append("quote")
        return SourceResult("", "fixture", "quote", "market", "success", as_of="2026-09-24")

    first = runtime.call(call, invoke)
    second = runtime.call(call, invoke)
    assert len(invocations) == 1
    assert first == second
    assert runtime.budget.snapshot()["total"] == 1


def test_failed_attempt_consumes_budget_and_is_cached():
    from webapp.chat_models import Scope
    from webapp.source_runtime import CallBudget, SourceCall, SourceResult, SourceRuntime

    runtime = SourceRuntime(Scope.whole_corpus(), CallBudget(1, 1, 1), lambda call: True)
    call = SourceCall("fixture", "quote", "market", {})
    invocations = []

    def invoke():
        invocations.append(True)
        return SourceResult("", "fixture", "quote", "market", "failed", error_code="timeout")

    assert runtime.call(call, invoke).status == "failed"
    assert runtime.call(call, invoke).status == "failed"
    assert len(invocations) == 1
    assert runtime.budget.snapshot() == {"total": 1, "market": 1, "web": 0}


def test_unauthorized_call_does_not_consume_budget_or_read_cached_result():
    from webapp.chat_models import Scope
    from webapp.source_runtime import CallBudget, SourceCall, SourceResult, SourceRuntime

    allowed = {"value": True}
    runtime = SourceRuntime(Scope.whole_corpus(), CallBudget(2, 2, 2), lambda call: allowed["value"])
    call = SourceCall("fixture", "quote", "market", {})
    expected = SourceResult("", "fixture", "quote", "market", "success", as_of="2026-09-24")
    actual = runtime.call(call, lambda: expected)
    assert actual.status == "success"
    assert actual.call_id == "source-1"
    allowed["value"] = False
    assert runtime.call(call, lambda: pytest.fail("unauthorized invoke")).status == "unavailable"
    assert runtime.budget.snapshot()["total"] == 1


def test_runtime_instances_do_not_share_results_or_budget():
    from webapp.chat_models import Scope
    from webapp.source_runtime import CallBudget, SourceCall, SourceResult, SourceRuntime

    call = SourceCall("fixture", "quote", "market", {})
    invocations = []
    results = []
    for run_id in range(2):
        runtime = SourceRuntime(Scope.whole_corpus(), CallBudget(1, 1, 1), lambda call: True)
        result = SourceResult("", "fixture", "quote", "market", "success",
                              content=str(run_id), as_of="2026-09-24")
        results.append(runtime.call(call, lambda result=result: (invocations.append(True), result)[1]))
    assert [result.content for result in results] == ["0", "1"]
    assert len(invocations) == 2


def test_cancelled_runtime_rejects_next_source_before_authorization_or_provider_call():
    from webapp.chat_models import Scope
    from webapp.chat_runs import ChatRunCancelled, ChatRunControl
    from webapp.source_runtime import CallBudget, SourceCall, SourceRuntime

    control = ChatRunControl("session", "run", timeout_seconds=10)
    authorizations = []
    invocations = []
    runtime = SourceRuntime(Scope.whole_corpus(), CallBudget(2, 2, 2),
                           lambda call: authorizations.append(call.operation) or True,
                           control=control)
    control.cancel("user")

    with pytest.raises(ChatRunCancelled):
        runtime.call(SourceCall("fixture", "next", "market", {}),
                     lambda: invocations.append("called"))
    assert authorizations == []
    assert invocations == []


def test_concurrent_duplicate_calls_invoke_provider_once():
    from webapp.chat_models import Scope
    from webapp.source_runtime import CallBudget, SourceCall, SourceResult, SourceRuntime

    runtime = SourceRuntime(Scope.whole_corpus(), CallBudget(2, 2, 2), lambda call: True)
    call = SourceCall("fixture", "quote", "market", {"symbol": "600900"})
    invocations = []

    def invoke():
        invocations.append(True)
        return SourceResult("", "fixture", "quote", "market", "success", as_of="2026-09-24")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: runtime.call(call, invoke), range(4)))
    assert len(invocations) == 1
    assert len(set(results)) == 1
    assert runtime.budget.snapshot()["total"] == 1
