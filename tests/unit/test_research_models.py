from dataclasses import replace

import pytest

from webapp.chat_models import Scope
from webapp.research_models import ResearchPlan, ResearchRun, ResearchStep, ResearchStepRun


def _plan():
    return ResearchPlan(
        objective="比较盈利质量",
        scope=Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"]),
        steps=(
            ResearchStep("s1", "retrieve", "检索披露", (), ("取得披露",)),
            ResearchStep("s2", "compare", "比较指标", ("s1",), ("完成比较",)),
        ),
        acceptance=("核对来源",),
    )


def test_research_run_allows_running_to_verifying_to_completed():
    run = ResearchRun.new(_plan())
    assert run.transition("running").transition("verifying").transition("completed").status == "completed"


def test_stopped_run_can_resume_only_from_incomplete_step():
    plan = _plan()
    stopped = ResearchRun(
        id="r1", plan=plan, status="stopped",
        step_runs=(ResearchStepRun("s1", "completed"), ResearchStepRun("s2", "stopped")),
    )
    assert stopped.resume_from_step_id == "s2"
    assert stopped.resume().status == "running"


def test_completed_run_cannot_transition_to_running():
    completed = ResearchRun.new(_plan()).transition("running").transition("verifying").transition("completed")
    with pytest.raises(ValueError, match="completed"):
        completed.transition("running")


def test_completed_step_is_immutable_and_round_trips_without_reasoning():
    plan = _plan()
    completed = ResearchStepRun("s1", "completed", input_summary="披露范围", artifacts=({"id": "a"},))
    with pytest.raises(ValueError, match="immutable"):
        completed.transition("failed")
    restored = ResearchRun.from_dict(ResearchRun("r1", plan, "stopped", (completed,)).to_dict())
    assert restored.step_runs[0].input_summary == "披露范围"
    assert "reasoning" not in restored.to_dict()


def test_resume_requires_completed_dependencies():
    plan = _plan()
    run = ResearchRun("r1", plan, "stopped", (ResearchStepRun("s1", "failed"), ResearchStepRun("s2", "stopped")))
    assert run.resume_from_step_id == "s1"
