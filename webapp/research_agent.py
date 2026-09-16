"""M3 planner → executor → verifier orchestration without free-form multi-agent delegation."""
from __future__ import annotations

from threading import Event
from typing import Any, Callable

from webapp.chat_models import AnswerRun, IntentDecision, Scope, ToolPolicy
from webapp.chat_verifier import ClaimVerifier
from webapp.research_executor import ResearchExecutor
from webapp.research_models import ResearchRun
from webapp.research_planner import ResearchPlanner


class ResearchAgent:
    def __init__(self, *, planner: ResearchPlanner | None = None, executor: ResearchExecutor | None = None,
                 verifier: Any | None = None, persist: Callable[[ResearchRun], None] | None = None,
                 normal_answer: Callable[..., str] | None = None) -> None:
        self.planner = planner or ResearchPlanner()
        self.executor = executor or ResearchExecutor()
        self.verifier = verifier or ClaimVerifier()
        self.persist = persist
        self.normal_answer = normal_answer or (lambda question, *_args: "请基于本地可核验披露回答。")
        self.last_run: ResearchRun | None = None

    def run(self, question: str, scope: Scope, intent: IntentDecision, policy: ToolPolicy, *, stop_event: Event,
            emit: Callable[[dict[str, Any]], None]) -> AnswerRun:
        plan = self.planner.plan(question, scope, intent, policy)
        if plan is None:
            missing = self.planner.last_awaiting_input
            if missing:
                emit({"type": "research_blocked", "reason": missing})
                return AnswerRun(content=missing, status="partial", scope=scope, intent_decision=intent, tool_policy=policy)
            return AnswerRun(content=self.normal_answer(question, scope, intent, policy), status="completed", scope=scope,
                             intent_decision=intent, tool_policy=policy)
        emit({"type": "research_plan", "plan": plan.to_dict()})
        return self._execute(ResearchRun.new(plan), scope, intent, policy, stop_event, emit)

    def resume(self, research_run: ResearchRun, *, stop_event: Event, emit: Callable[[dict[str, Any]], None]) -> AnswerRun:
        emit({"type": "research_plan", "plan": research_run.plan.to_dict(), "resumed": True})
        return self._execute(research_run, research_run.plan.scope, None, None, stop_event, emit, resume=True)

    def _execute(self, run: ResearchRun, scope: Scope, intent: IntentDecision | None, policy: ToolPolicy | None,
                 stop_event: Event, emit: Callable[[dict[str, Any]], None], resume: bool = False) -> AnswerRun:
        def relay(event: dict[str, Any]) -> None:
            kind = event.get("type")
            if kind in {"research_step_started", "research_step_completed", "research_step_failed"}:
                emit(event)
            elif kind == "research_step_stopped":
                emit({"type": "research_blocked", "reason": event.get("reason", "研究已停止")})
        executed = self.executor.resume(run, stop_event=stop_event, emit=relay) if resume else self.executor.execute(run, stop_event=stop_event, emit=relay)
        content = "研究已完成：已按计划核对范围内来源。"
        verification = None
        if executed.status == "verifying":
            verification = self.verifier.verify(content, scope, (), (), ())
            executed = executed.transition("partial" if verification.status == "blocked" else "completed")
        self.last_run = executed
        if self.persist:
            self.persist(executed)
        status = {"completed": "completed", "partial": "partial", "stopped": "stopped", "failed": "failed"}.get(executed.status, "partial")
        if verification is not None and verification.status == "blocked":
            content = "未找到可核验的披露，不能确认该结论。"
        answer = AnswerRun(content=content, status=status, scope=scope, intent_decision=intent, tool_policy=policy,
                           verification_report=verification, research_run_id=executed.id,
                           research_summary={"status": executed.status, "resume_from_step_id": executed.resume_from_step_id})
        emit({"type": "research_done", "run": executed.to_dict(), "status": status})
        return answer
