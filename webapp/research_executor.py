"""Dependency-safe, cancellable execution for bounded ResearchRun records."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from threading import Event
from typing import Any, Callable, Mapping

from webapp.research_models import ResearchRun, ResearchStep, ResearchStepRun

StepHandler = Callable[[ResearchStep, ResearchRun], Mapping[str, Any] | None]


class ResearchExecutor:
    def __init__(self, handlers: Mapping[str, StepHandler] | None = None, *, persist: Callable[[ResearchRun], None] | None = None) -> None:
        self.handlers = dict(handlers or {})
        self.persist = persist
        self._tool_cache: dict[str, Mapping[str, Any]] = {}

    def execute(self, run: ResearchRun, *, stop_event: Event, emit: Callable[[dict[str, Any]], None]) -> ResearchRun:
        if run.status == "planned":
            run = run.transition("running")
        elif run.status not in {"running", "stopped", "failed", "partial"}:
            raise ValueError("research run is not executable")
        return self._run(run, stop_event, emit)

    def resume(self, run: ResearchRun, *, stop_event: Event, emit: Callable[[dict[str, Any]], None]) -> ResearchRun:
        return self._run(run.resume(), stop_event, emit)

    def _save(self, run: ResearchRun) -> ResearchRun:
        if self.persist is not None:
            self.persist(run)
        return run

    @staticmethod
    def _ready(run: ResearchRun) -> list[ResearchStep]:
        states = {item.step_id: item.status for item in run.step_runs}
        ready: list[ResearchStep] = []
        for step in run.plan.steps:
            state = states.get(step.id, "pending")
            if state == "completed":
                continue
            if all(states.get(dependency) == "completed" for dependency in step.depends_on):
                ready.append(step)
        return ready

    @staticmethod
    def _event(emit: Callable[[dict[str, Any]], None], step: ResearchStep, status: str, **extra: Any) -> None:
        emit({"type": "research_step_" + status, "step_id": step.id, "label": step.label, "status": status, **extra})

    def _run(self, run: ResearchRun, stop_event: Event, emit: Callable[[dict[str, Any]], None]) -> ResearchRun:
        while True:
            ready = self._ready(run)
            if not ready:
                if all(item.status == "completed" for item in run.step_runs) and len(run.step_runs) == len(run.plan.steps):
                    return self._save(run.transition("verifying"))
                return self._save(run.transition("failed"))
            if stop_event.is_set():
                step = ready[0]
                stopped = ResearchStepRun(step.id, "stopped", input_summary=step.label).transition("stopped")
                run = self._save(run.with_step_run(stopped).transition("stopped"))
                self._event(emit, step, "stopped", reason="用户已停止研究")
                return run

            # Only independent external retrieval can overlap. All normalization,
            # comparison, verification and answer work remains ordered.
            parallel = [step for step in ready if step.kind in {"retrieve", "tool"}]
            batch = parallel if parallel else [ready[0]]
            if len(batch) == 1:
                run, terminal = self._execute_one(run, batch[0], stop_event, emit)
                if terminal:
                    return run
                continue
            with ThreadPoolExecutor(max_workers=min(3, len(batch)), thread_name_prefix="research-step") as pool:
                for step in batch:
                    self._event(emit, step, "running")
                futures = {pool.submit(self._call, step, run): step for step in batch}
                for future in as_completed(futures):
                    step = futures[future]
                    if stop_event.is_set():
                        for pending in futures:
                            pending.cancel()
                        stopped = ResearchStepRun(step.id, "stopped", input_summary=step.label).transition("stopped")
                        run = self._save(run.with_step_run(stopped).transition("stopped"))
                        self._event(emit, step, "stopped", reason="用户已停止研究")
                        return run
                    try:
                        result = future.result()
                    except Exception as exc:
                        run = self._fail(run, step, str(exc), emit)
                        return run
                    run = self._complete(run, step, result, emit)

    def _execute_one(self, run: ResearchRun, step: ResearchStep, stop_event: Event, emit: Callable[[dict[str, Any]], None]) -> tuple[ResearchRun, bool]:
        if stop_event.is_set():
            stopped = ResearchStepRun(step.id, "stopped", input_summary=step.label).transition("stopped")
            run = self._save(run.with_step_run(stopped).transition("stopped"))
            self._event(emit, step, "stopped", reason="用户已停止研究")
            return run, True
        self._event(emit, step, "running")
        try:
            result = self._call(step, run)
        except Exception as exc:
            return self._fail(run, step, str(exc), emit), True
        if stop_event.is_set():
            stopped = ResearchStepRun(step.id, "stopped", input_summary=step.label).transition("stopped")
            run = self._save(run.with_step_run(stopped).transition("stopped"))
            self._event(emit, step, "stopped", reason="用户已停止研究")
            return run, True
        return self._complete(run, step, result, emit), False

    def _call(self, step: ResearchStep, run: ResearchRun) -> Mapping[str, Any]:
        handler = self.handlers.get(step.kind)
        if handler is None:
            return {}
        cache_key = ""
        if step.kind == "tool":
            cache_key = "|".join((step.tools[0] if step.tools else "", step.id, run.plan.scope.to_dict().__repr__()))
            cached = self._tool_cache.get(cache_key)
            if cached is not None:
                return cached
        result = handler(step, run) or {}
        if not isinstance(result, Mapping):
            raise ValueError("research step handler must return an object")
        if cache_key:
            self._tool_cache[cache_key] = result
        return result

    def _complete(self, run: ResearchRun, step: ResearchStep, result: Mapping[str, Any], emit: Callable[[dict[str, Any]], None]) -> ResearchRun:
        completed = ResearchStepRun(
            step.id, "completed", input_summary=str(result.get("input_summary", step.label)),
            artifacts=tuple(result.get("artifacts", ())), facts=tuple(result.get("facts", ())),
            conflicts=tuple(result.get("conflicts", ())), verification=result.get("verification"),
        )
        run = self._save(run.with_step_run(completed))
        self._event(emit, step, "completed", evidence_count=len(completed.artifacts) + len(completed.facts))
        return run

    def _fail(self, run: ResearchRun, step: ResearchStep, error: str, emit: Callable[[dict[str, Any]], None]) -> ResearchRun:
        failed = ResearchStepRun(step.id, "failed", input_summary=step.label, error=error).transition("failed", error=error)
        run = self._save(run.with_step_run(failed).transition("failed"))
        self._event(emit, step, "failed", reason="该步骤未完成")
        return run
