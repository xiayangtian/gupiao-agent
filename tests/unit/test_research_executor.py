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


def test_parallel_sibling_failure_then_stop_keeps_a_legal_terminal_state():
    """并行兄弟步骤失败后又收到停止：运行必须停在合法终态，不得抛非法转换。

    failed 与 stopped 都是可恢复终态；先落盘 sibling 失败的运行不能再执行
    failed→stopped 转换，否则停止请求会把调用方炸成 ValueError。
    """
    scope = Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"])
    plan = ResearchPlan("研究", scope, (
        ResearchStep("bad", "retrieve", "失败来源"),
        ResearchStep("good", "retrieve", "可用来源"),
    ), ("核对",))
    stop = Event()

    def retrieve(step, run):
        if step.id == "bad":
            time.sleep(0.02)
            raise RuntimeError("provider unavailable")
        time.sleep(0.2)
        # 兄弟步骤失败已经落盘之后，恢复流/断开观察才把停止事件送达。
        stop.set()
        return {"artifacts": [{"id": "real-source"}]}

    run = ResearchExecutor(handlers={"retrieve": retrieve}).execute(
        ResearchRun.new(plan), stop_event=stop, emit=lambda _: None)

    assert run.status in {"failed", "stopped"}
    assert next(item for item in run.step_runs if item.step_id == "bad").status == "failed"
    assert run.resume_from_step_id


def test_execute_routes_resumable_terminal_run_through_legal_resume():
    """execute() 收到可恢复终态（stopped/failed/partial）时必须走 resume 合法路径。"""
    scope = Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"])
    plan = ResearchPlan("研究", scope, (
        ResearchStep("t", "tool", "查询", tools=("quote",)),
        ResearchStep("c", "compare", "比较", ("t",)),
    ), ("核对",))
    run = ResearchRun("run", plan, "stopped", (ResearchStepRun("t", "completed"), ResearchStepRun("c", "stopped")))
    calls = {"tool": 0, "compare": 0}

    def tool(step, run): calls["tool"] += 1; return {}
    def compare(step, run): calls["compare"] += 1; return {}

    resumed = ResearchExecutor(handlers={"tool": tool, "compare": compare}).execute(
        run, stop_event=Event(), emit=lambda _: None)

    assert resumed.status == "verifying"
    assert calls == {"tool": 0, "compare": 1}


def test_ordered_step_is_persisted_running_before_its_external_call():
    """步骤必须在外部调用前持久化为 running，而不是只在 completed 后落盘。"""
    scope = Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"])
    plan = ResearchPlan("研究", scope, (
        ResearchStep("r", "retrieve", "检索"),
        ResearchStep("n", "normalize", "整理", ("r",)),
    ), ("核对",))
    persisted = []
    observed = {}

    def snapshot():
        return {item.step_id: item.status for item in persisted[-1].step_runs}

    def retrieve(step, run):
        observed["retrieve"] = snapshot()
        return {"artifacts": [{"id": "source"}]}

    def normalize(step, run):
        observed["normalize"] = snapshot()
        return {}

    ResearchExecutor(handlers={"retrieve": retrieve, "normalize": normalize}, persist=persisted.append).execute(
        ResearchRun.new(plan), stop_event=Event(), emit=lambda _: None)

    assert observed["retrieve"] == {"r": "running"}
    assert observed["normalize"] == {"r": "completed", "n": "running"}


def test_parallel_steps_are_persisted_running_before_their_external_calls():
    """并行批次的每个步骤也必须在外部调用前落盘为 running。"""
    scope = Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"])
    plan = ResearchPlan("研究", scope, (
        ResearchStep("r1", "retrieve", "检索一"),
        ResearchStep("r2", "retrieve", "检索二"),
    ), ("核对",))
    persisted = []

    def retrieve(step, run):
        return {"artifacts": [{"id": step.id}]}

    ResearchExecutor(handlers={"retrieve": retrieve}, persist=persisted.append).execute(
        ResearchRun.new(plan), stop_event=Event(), emit=lambda _: None)

    # 每个步骤首次被落盘的状态必须是 running；只在完成后落盘的实现无法满足。
    first_seen = {}
    for item_run in persisted:
        for item in item_run.step_runs:
            first_seen.setdefault(item.step_id, item.status)
    assert first_seen == {"r1": "running", "r2": "running"}
    # 并且同一条持久化记录里两个步骤都处于 running：批次在外部调用前已落盘。
    assert any({item.step_id for item in item_run.step_runs if item.status == "running"} >= {"r1", "r2"}
               for item_run in persisted)


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
