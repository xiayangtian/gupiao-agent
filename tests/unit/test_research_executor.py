from threading import Event
import time

from webapp.chat_models import Scope
from webapp.research_executor import ResearchExecutor
from webapp.research_models import ResearchPlan, ResearchRun, ResearchStep, ResearchStepRun


def _plan():
    scope = Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"])
    return ResearchPlan("研究", scope, (
        ResearchStep("r1", "retrieve", "检索一"),
        ResearchStep("r2", "retrieve", "检索二"),
        ResearchStep("n1", "normalize", "整理", ("r1", "r2")),
    ), ("核对",))


def test_independent_retrieve_steps_run_before_normalize_dependency():
    events = []
    executor = ResearchExecutor(handlers={kind: lambda step, run: {"artifacts": [{"id": step.id}]} for kind in ("retrieve", "normalize")})
    run = executor.execute(ResearchRun.new(_plan()), stop_event=Event(), emit=events.append)
    order = [event["step_id"] + ":" + event["status"] for event in events if "step_id" in event]
    assert order.index("r1:completed") < order.index("n1:running")
    assert order.index("r2:completed") < order.index("n1:running")
    assert run.status == "verifying"


def test_stop_preserves_completed_steps_and_marks_current_step_stopped():
    stop = Event()
    stop.set()
    run = ResearchExecutor().execute(ResearchRun.new(_plan()), stop_event=stop, emit=lambda _: None)
    assert run.status == "stopped"
    assert run.step_runs[0].status == "stopped"


def test_parallel_failure_persists_completed_sibling_and_resume_does_not_repeat_it():
    scope = Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"])
    plan = ResearchPlan("研究", scope, (
        ResearchStep("good", "retrieve", "可用来源"),
        ResearchStep("bad", "retrieve", "失败来源"),
    ), ("核对",))
    persisted = []
    calls = {"good": 0, "bad": 0}
    def retrieve(step, run):
        calls[step.id] += 1
        if step.id == "bad":
            raise RuntimeError("provider unavailable")
        time.sleep(0.05)
        return {"artifacts": [{"id": "real-source"}]}
    executor = ResearchExecutor(handlers={"retrieve": retrieve}, persist=persisted.append)
    failed = executor.execute(ResearchRun.new(plan), stop_event=Event(), emit=lambda _: None)
    assert failed.status == "failed"
    assert next(item for item in failed.step_runs if item.step_id == "good").status == "completed"
    assert any(next((step for step in item.step_runs if step.step_id == "good"), None) for item in persisted)
    resumed = executor.resume(failed, stop_event=Event(), emit=lambda _: None)
    assert calls["good"] == 1
    assert calls["bad"] == 2
    assert next(item for item in resumed.step_runs if item.step_id == "good").status == "completed"


def test_resume_does_not_repeat_completed_external_tool_step():
    scope = Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"])
    plan = ResearchPlan("研究", scope, (
        ResearchStep("t", "tool", "查询", tools=("quote",)),
        ResearchStep("c", "compare", "比较", ("t",)),
    ), ("核对",))
    run = ResearchRun("run", plan, "failed", (ResearchStepRun("t", "completed"), ResearchStepRun("c", "failed")))
    calls = {"tool": 0, "compare": 0}
    def tool(step, run): calls["tool"] += 1; return {}
    def compare(step, run): calls["compare"] += 1; return {}
    resumed = ResearchExecutor(handlers={"tool": tool, "compare": compare}).resume(run, stop_event=Event(), emit=lambda _: None)
    assert calls == {"tool": 0, "compare": 1}
    assert resumed.step_runs[0].status == "completed"
