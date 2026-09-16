"""M3 planner → executor → verifier orchestration without free-form multi-agent delegation."""
from __future__ import annotations

from threading import Event
from typing import Any, Callable

from webapp.chat_models import AnswerRun, EvidenceArtifact, Fact, FactConflict, IntentDecision, Scope, ToolPolicy
from webapp.chat_verifier import ClaimVerifier
from webapp.research_executor import ResearchExecutor
from webapp.research_models import ResearchRun
from webapp.research_planner import ResearchPlanner


class ResearchAgent:
    def __init__(self, *, planner: ResearchPlanner | None = None, executor: ResearchExecutor | None = None,
                 verifier: Any | None = None, persist: Callable[[ResearchRun], None] | None = None,
                 normal_answer: Callable[..., str] | None = None) -> None:
        self.planner = planner or ResearchPlanner()
        self.executor = executor or ResearchExecutor(persist=persist)
        # The executor owns every step transition, so it must use the same
        # persistence callback as the final orchestrator record.
        if persist is not None and self.executor.persist is None:
            self.executor.persist = persist
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
        blocked = False

        def relay(event: dict[str, Any]) -> None:
            nonlocal blocked
            kind = event.get("type")
            if kind in {"research_step_started", "research_step_completed", "research_step_failed"}:
                emit(event)
            elif kind == "research_step_stopped":
                blocked = True
                emit({"type": "research_blocked", "reason": event.get("reason", "研究已停止")})
        emit({"type": "research_running", "status": "running"})
        executed = self.executor.resume(run, stop_event=stop_event, emit=relay) if resume else self.executor.execute(run, stop_event=stop_event, emit=relay)
        artifacts, facts, conflicts = self._evidence(executed)
        answer_step = next((item for item in executed.step_runs if item.step_id == "answer"), None)
        content = answer_step.result_summary if answer_step else ""
        verification = None
        if executed.status == "verifying" and not (artifacts or facts):
            executed = executed.transition("partial")
            content = "未取得可核验的范围内来源，研究不能完成。"
            emit({"type": "research_blocked", "reason": "未取得可核验的范围内来源。"})
        elif executed.status == "verifying":
            if not content:
                executed = executed.transition("partial")
                content = "已取得部分来源，但未形成可核验的研究结论。"
                emit({"type": "research_blocked", "reason": "未形成可核验的研究结论。"})
            else:
                verification = self.verifier.verify(content, scope, facts, artifacts, conflicts)
                executed = executed.transition("partial" if verification.status != "passed" else "completed")
        if not content:
            content = "研究未完成；请根据步骤状态补充来源或重试。"
        if executed.status == "failed" and not blocked:
            # 失败步骤已经写入可恢复的持久化产物，这里把业务阻塞原因送入公开流。
            emit({"type": "research_blocked", "reason": "有研究步骤未完成，可继续研究或重新生成。"})
        self.last_run = executed
        if self.persist:
            self.persist(executed)
        status = {"completed": "completed", "partial": "partial", "stopped": "stopped", "failed": "failed"}.get(executed.status, "partial")
        if verification is not None and verification.status == "blocked":
            content = "未找到可核验的披露，不能确认该结论。"
        answer = AnswerRun(content=content, status=status, scope=scope, facts=facts, conflicts=conflicts,
                           artifacts=artifacts, intent_decision=intent, tool_policy=policy,
                           verification_report=verification, research_run_id=executed.id,
                           research_summary={"status": executed.status, "resume_from_step_id": executed.resume_from_step_id})
        emit({"type": "research_done", "run": executed.to_dict(), "status": status})
        return answer

    @staticmethod
    def _evidence(run: ResearchRun) -> tuple[tuple[EvidenceArtifact, ...], tuple[Fact, ...], tuple[FactConflict, ...]]:
        artifacts: list[EvidenceArtifact] = []
        facts: list[Fact] = []
        conflicts: list[FactConflict] = []
        for step in run.step_runs:
            for raw in step.artifacts:
                try:
                    artifacts.append(EvidenceArtifact.from_dict(raw))
                except ValueError:
                    continue
            for raw in step.facts:
                try:
                    facts.append(Fact.from_dict(raw))
                except ValueError:
                    continue
            for raw in step.conflicts:
                try:
                    conflicts.append(FactConflict.from_dict(raw))
                except ValueError:
                    continue
        return tuple(artifacts), tuple(facts), tuple(conflicts)
