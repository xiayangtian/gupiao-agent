"""Immutable, JSON-safe contracts for bounded M3 research runs.

These records deliberately retain business inputs and evidence summaries only.  Model
reasoning, prompts, vector distances and raw tool arguments are never accepted.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal, Mapping
from uuid import uuid4

from webapp.chat_models import Scope

ResearchStepKind = Literal["retrieve", "normalize", "compare", "tool", "verify", "answer"]
ResearchRunStatus = Literal["planned", "running", "verifying", "completed", "awaiting_input", "partial", "stopped", "failed"]
ResearchStepStatus = Literal["pending", "running", "completed", "stopped", "failed", "skipped"]

_STEP_KINDS = frozenset(("retrieve", "normalize", "compare", "tool", "verify", "answer"))
_RUN_STATUSES = frozenset(("planned", "running", "verifying", "completed", "awaiting_input", "partial", "stopped", "failed"))
_STEP_STATUSES = frozenset(("pending", "running", "completed", "stopped", "failed", "skipped"))
_TERMINAL_RESUMABLE = frozenset(("partial", "stopped", "failed"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: object, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{name} must be a {'possibly empty ' if empty else 'non-empty '}string")
    return value


def _texts(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{name} must be an array")
    return tuple(_text(item, f"{name}[]") for item in value)


def _safe_json(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    # Explicitly reject internal material even if a caller tries to persist it under
    # a nested artifact envelope.
    forbidden = {"reasoning", "chain_of_thought", "prompt", "raw_arguments", "vector_distance"}
    if forbidden.intersection(value):
        raise ValueError(f"{name} contains non-persistable internal data")
    return {str(key): item for key, item in value.items()}


@dataclass(frozen=True)
class ResearchStep:
    id: str
    kind: ResearchStepKind
    label: str
    depends_on: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    report_ids: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.id, "step id")
        if self.kind not in _STEP_KINDS:
            raise ValueError("unsupported research step kind")
        _text(self.label, "step label")
        for name in ("depends_on", "acceptance", "report_ids", "tools"):
            if not isinstance(getattr(self, name), tuple):
                raise ValueError(f"{name} must be a tuple")
            _texts(getattr(self, name), name)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "depends_on": list(self.depends_on), "acceptance": list(self.acceptance),
                "report_ids": list(self.report_ids), "tools": list(self.tools)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResearchStep":
        if not isinstance(data, Mapping):
            raise ValueError("research step must be an object")
        return cls(_text(data.get("id"), "step id"), _text(data.get("kind"), "step kind"),
                   _text(data.get("label"), "step label"), _texts(data.get("depends_on", []), "depends_on"),
                   _texts(data.get("acceptance", []), "acceptance"), _texts(data.get("report_ids", []), "report_ids"),
                   _texts(data.get("tools", []), "tools"))


@dataclass(frozen=True)
class ResearchPlan:
    objective: str
    scope: Scope
    steps: tuple[ResearchStep, ...]
    acceptance: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.objective, "objective")
        if not isinstance(self.scope, Scope):
            raise ValueError("scope must be a Scope")
        if not isinstance(self.steps, tuple) or not self.steps or len(self.steps) > 8:
            raise ValueError("steps must contain one to eight ResearchStep records")
        if not all(isinstance(step, ResearchStep) for step in self.steps):
            raise ValueError("steps must be ResearchStep records")
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("research step ids must be unique")
        if not isinstance(self.acceptance, tuple) or not self.acceptance:
            raise ValueError("acceptance must be a non-empty tuple")
        _texts(self.acceptance, "acceptance")

    def to_dict(self) -> dict[str, Any]:
        return {"objective": self.objective, "scope": self.scope.to_dict(),
                "steps": [step.to_dict() for step in self.steps], "acceptance": list(self.acceptance)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResearchPlan":
        if not isinstance(data, Mapping):
            raise ValueError("research plan must be an object")
        scope = data.get("scope")
        if not isinstance(scope, Mapping):
            raise ValueError("research plan scope must be an object")
        steps = data.get("steps", [])
        return cls(_text(data.get("objective"), "objective"), Scope.from_dict(scope),
                   tuple(ResearchStep.from_dict(item) for item in steps), _texts(data.get("acceptance", []), "acceptance"))


@dataclass(frozen=True)
class ResearchStepRun:
    step_id: str
    status: ResearchStepStatus = "pending"
    input_summary: str = ""
    result_summary: str = ""
    artifacts: tuple[dict[str, Any], ...] = ()
    facts: tuple[dict[str, Any], ...] = ()
    conflicts: tuple[dict[str, Any], ...] = ()
    verification: dict[str, Any] | None = None
    error: str = ""
    started_at: str = ""
    finished_at: str = ""

    def __post_init__(self) -> None:
        _text(self.step_id, "step_id")
        if self.status not in _STEP_STATUSES:
            raise ValueError("unsupported research step status")
        for name in ("input_summary", "result_summary", "error", "started_at", "finished_at"):
            _text(getattr(self, name), name, empty=True)
        for name in ("artifacts", "facts", "conflicts"):
            values = getattr(self, name)
            if not isinstance(values, tuple):
                raise ValueError(f"{name} must be a tuple")
            object.__setattr__(self, name, tuple(_safe_json(item, name) for item in values))
        if self.verification is not None:
            object.__setattr__(self, "verification", _safe_json(self.verification, "verification"))
        if self.status == "completed" and not self.finished_at:
            object.__setattr__(self, "finished_at", _now())

    def transition(self, status: ResearchStepStatus, *, error: str = "") -> "ResearchStepRun":
        if self.status == "completed" and status != "completed":
            raise ValueError("completed research step is immutable")
        if status not in _STEP_STATUSES:
            raise ValueError("unsupported research step status")
        return replace(self, status=status, error=error, started_at=self.started_at or _now(),
                       finished_at=_now() if status in {"completed", "stopped", "failed", "skipped"} else "")

    def to_dict(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "status": self.status, "input_summary": self.input_summary,
                "result_summary": self.result_summary, "artifacts": list(self.artifacts), "facts": list(self.facts), "conflicts": list(self.conflicts),
                "verification": self.verification, "error": self.error, "started_at": self.started_at,
                "finished_at": self.finished_at}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResearchStepRun":
        if not isinstance(data, Mapping):
            raise ValueError("research step run must be an object")
        verification = data.get("verification")
        return cls(_text(data.get("step_id"), "step_id"), _text(data.get("status", "pending"), "step status"),
                   _text(data.get("input_summary", ""), "input_summary", empty=True),
                   _text(data.get("result_summary", ""), "result_summary", empty=True),
                   tuple(_safe_json(item, "artifacts") for item in data.get("artifacts", [])),
                   tuple(_safe_json(item, "facts") for item in data.get("facts", [])),
                   tuple(_safe_json(item, "conflicts") for item in data.get("conflicts", [])),
                   _safe_json(verification, "verification") if verification is not None else None,
                   _text(data.get("error", ""), "error", empty=True), _text(data.get("started_at", ""), "started_at", empty=True),
                   _text(data.get("finished_at", ""), "finished_at", empty=True))


@dataclass(frozen=True)
class ResearchRun:
    id: str
    plan: ResearchPlan
    status: ResearchRunStatus = "planned"
    step_runs: tuple[ResearchStepRun, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    resume_from_step_id: str = ""

    def __post_init__(self) -> None:
        _text(self.id, "research run id")
        if not isinstance(self.plan, ResearchPlan) or self.status not in _RUN_STATUSES:
            raise ValueError("invalid research run")
        if not isinstance(self.step_runs, tuple) or not all(isinstance(run, ResearchStepRun) for run in self.step_runs):
            raise ValueError("step_runs must be a tuple of ResearchStepRun")
        allowed = {step.id for step in self.plan.steps}
        if any(run.step_id not in allowed for run in self.step_runs) or len({run.step_id for run in self.step_runs}) != len(self.step_runs):
            raise ValueError("step runs must have unique plan step ids")
        for name in ("started_at", "finished_at", "resume_from_step_id"):
            _text(getattr(self, name), name, empty=True)
        if self.status in _TERMINAL_RESUMABLE and not self.resume_from_step_id:
            object.__setattr__(self, "resume_from_step_id", self._first_resume_step() or "")
        if self.status == "completed":
            object.__setattr__(self, "finished_at", self.finished_at or _now())

    @classmethod
    def new(cls, plan: ResearchPlan, *, run_id: str | None = None) -> "ResearchRun":
        return cls(run_id or uuid4().hex, plan)

    def _first_resume_step(self) -> str | None:
        runs = {item.step_id: item for item in self.step_runs}
        for step in self.plan.steps:
            item = runs.get(step.id)
            if item is not None and item.status == "completed":
                continue
            if all(runs.get(dep) is not None and runs[dep].status == "completed" for dep in step.depends_on):
                return step.id
        return None

    def transition(self, status: ResearchRunStatus) -> "ResearchRun":
        allowed = {
            "planned": {"running", "stopped", "failed"},
            "running": {"verifying", "awaiting_input", "partial", "stopped", "failed"},
            "verifying": {"completed", "partial", "stopped", "failed"},
        }
        if self.status == "completed":
            raise ValueError("completed research run is terminal")
        if status not in allowed.get(self.status, set()):
            raise ValueError(f"cannot transition {self.status} to {status}; resumable states require resume()")
        return replace(self, status=status, started_at=self.started_at or _now(),
                       finished_at=_now() if status in {"completed", "awaiting_input", "partial", "stopped", "failed"} else "",
                       resume_from_step_id=(self._first_resume_step() or "") if status in _TERMINAL_RESUMABLE else "")

    def resume(self) -> "ResearchRun":
        if self.status not in _TERMINAL_RESUMABLE:
            raise ValueError("only partial, stopped, or failed research runs can resume")
        step_id = self._first_resume_step()
        if not step_id:
            raise ValueError("research run has no dependency-safe incomplete step")
        return replace(self, status="running", started_at=_now(), finished_at="", resume_from_step_id=step_id)

    def with_step_run(self, step_run: ResearchStepRun) -> "ResearchRun":
        if step_run.step_id not in {step.id for step in self.plan.steps}:
            raise ValueError("step run is outside plan")
        existing = {item.step_id: item for item in self.step_runs}
        prior = existing.get(step_run.step_id)
        if prior is not None and prior.status == "completed" and step_run != prior:
            raise ValueError("completed research step is immutable")
        existing[step_run.step_id] = step_run
        ordered = tuple(existing[step.id] for step in self.plan.steps if step.id in existing)
        return replace(self, step_runs=ordered)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "plan": self.plan.to_dict(), "status": self.status,
                "step_runs": [item.to_dict() for item in self.step_runs], "started_at": self.started_at,
                "finished_at": self.finished_at, "resume_from_step_id": self.resume_from_step_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResearchRun":
        if not isinstance(data, Mapping) or not isinstance(data.get("plan"), Mapping):
            raise ValueError("research run must contain a plan")
        return cls(_text(data.get("id"), "research run id"), ResearchPlan.from_dict(data["plan"]),
                   _text(data.get("status", "planned"), "research status"),
                   tuple(ResearchStepRun.from_dict(item) for item in data.get("step_runs", [])),
                   _text(data.get("started_at", ""), "started_at", empty=True),
                   _text(data.get("finished_at", ""), "finished_at", empty=True),
                   _text(data.get("resume_from_step_id", ""), "resume_from_step_id", empty=True))
