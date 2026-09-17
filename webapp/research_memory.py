"""Explicit, revocable research memory backed by immutable trusted-chat evidence.

This store has no automatic write path: callers must invoke one of the ``save_*``
methods after an explicit user action.  It persists only a Fact, EvidenceArtifact,
or user Decision payload plus its source AnswerRun id; AnswerRun bodies and model
reasoning are deliberately never copied into the memory sidecar.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Mapping
from uuid import uuid4

from webapp.chat_models import AnswerRun, EvidenceArtifact, Fact

DEFAULT_PATH = "data/research_memory.json"
MemoryKind = Literal["fact", "artifact", "decision"]
RunLookup = Callable[[str], AnswerRun | None]


@dataclass(frozen=True)
class MemoryEntry:
    id: str
    kind: MemoryKind
    source_run_id: str
    payload: dict[str, Any]
    created_at: str
    expires_at: str | None
    revoked_at: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("memory id must be a non-empty string")
        if self.kind not in {"fact", "artifact", "decision"}:
            raise ValueError("memory kind must be fact, artifact, or decision")
        if not isinstance(self.source_run_id, str) or not self.source_run_id:
            raise ValueError("source_run_id must be a non-empty string")
        if not isinstance(self.payload, Mapping):
            raise ValueError("memory payload must be an object")
        _parse_time(self.created_at, "created_at")
        if self.expires_at is not None:
            _parse_time(self.expires_at, "expires_at")
        if self.revoked_at is not None:
            _parse_time(self.revoked_at, "revoked_at")
        object.__setattr__(self, "payload", _validated_payload(self.kind, self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source_run_id": self.source_run_id,
            "payload": self.payload,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MemoryEntry":
        if not isinstance(value, Mapping):
            raise ValueError("memory entry must be an object")
        expires_at = value.get("expires_at")
        revoked_at = value.get("revoked_at")
        if expires_at is not None and not isinstance(expires_at, str):
            raise ValueError("expires_at must be a string or null")
        if revoked_at is not None and not isinstance(revoked_at, str):
            raise ValueError("revoked_at must be a string or null")
        return cls(
            id=_text(value.get("id"), "memory id"),
            kind=_text(value.get("kind"), "memory kind"),
            source_run_id=_text(value.get("source_run_id"), "source_run_id"),
            payload=_mapping(value.get("payload"), "memory payload"),
            created_at=_text(value.get("created_at"), "created_at"),
            expires_at=expires_at,
            revoked_at=revoked_at,
        )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _parse_time(value: str, name: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not value:
        raise ValueError(f"{name} must be a non-empty array")
    result = tuple(_text(item, f"{name}[]") for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _validated_payload(kind: MemoryKind, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Allow only the immutable evidence fields that a memory entry needs."""
    if kind == "fact":
        return Fact.from_dict(payload).to_dict()
    if kind == "artifact":
        return EvidenceArtifact.from_dict(payload).to_dict()
    if set(payload) != {"text", "evidence_ids"}:
        raise ValueError("decision payload contains unsupported fields")
    return {
        "text": _text(payload.get("text"), "decision text").strip(),
        "evidence_ids": list(_strings(payload.get("evidence_ids"), "decision evidence_ids")),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expires_after(days: int, created_at: str) -> str:
    return (_parse_time(created_at, "created_at") + timedelta(days=days)).isoformat(timespec="seconds")


class ResearchMemoryStore:
    """Persist only user-selected, traceable research memories.

    ``run_lookup`` is intentionally optional to avoid coupling this primitive to a
    particular session store.  Fact saves fail closed without it because eligibility
    depends on the immutable source AnswerRun.  API callers supply an owner-checked
    lookup after verifying the session boundary.
    """

    def __init__(self, path: str | None = None, *, run_lookup: RunLookup | None = None) -> None:
        if run_lookup is not None and not callable(run_lookup):
            raise ValueError("run_lookup must be callable")
        self.path = path or DEFAULT_PATH
        self._run_lookup = run_lookup
        self._lock = threading.RLock()
        self._entries = self._load()

    def _load(self) -> list[MemoryEntry]:
        try:
            with open(self.path, encoding="utf-8") as source:
                payload = json.load(source)
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, Mapping) or not isinstance(payload.get("entries"), list):
            return []
        entries = []
        for raw in payload["entries"]:
            try:
                entries.append(MemoryEntry.from_dict(raw))
            except ValueError:
                # A malformed sidecar entry must never become active memory.
                continue
        return entries

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temporary_path = f"{self.path}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as target:
            json.dump(
                {"schema_version": 1, "entries": [entry.to_dict() for entry in self._entries]},
                target,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(temporary_path, self.path)

    def _append(
        self,
        kind: MemoryKind,
        source_run_id: str,
        payload: Mapping[str, Any],
        *,
        expires_after_days: int | None,
    ) -> MemoryEntry:
        source_run_id = _text(source_run_id, "source_run_id")
        created_at = _now()
        entry = MemoryEntry(
            id=uuid4().hex,
            kind=kind,
            source_run_id=source_run_id,
            payload=dict(payload),
            created_at=created_at,
            expires_at=_expires_after(expires_after_days, created_at) if expires_after_days else None,
            revoked_at=None,
        )
        self._entries.append(entry)
        self._save()
        return entry

    def _source_run(self, run_id: str) -> AnswerRun:
        run_id = _text(run_id, "source_run_id")
        if self._run_lookup is None:
            raise ValueError("来源运行不可用，不能保存未核验事实")
        run = self._run_lookup(run_id)
        if not isinstance(run, AnswerRun) or run.id != run_id:
            raise ValueError("来源运行不存在，不能保存未核验事实")
        return run

    @staticmethod
    def _eligible_fact_source(run: AnswerRun, fact: Fact) -> bool:
        if run.status not in {"completed", "partial"} or run.verification_report is None:
            return False
        report = run.verification_report
        return (
            report.status in {"passed", "partial"}
            and set(fact.evidence_ids).issubset(report.supported_fact_ids)
        )

    def save_fact(self, fact: Fact, run_id: str) -> MemoryEntry:
        """Save only a verified fact from a completed or verified-partial run."""
        if not isinstance(fact, Fact) or fact.verification != "verified" or fact.source_type != "pdf":
            raise ValueError("只有已验证的 PDF 事实可以保存到研究记忆")
        with self._lock:
            run = self._source_run(run_id)
            if fact not in run.facts or not self._eligible_fact_source(run, fact):
                raise ValueError("来源运行未完成或事实未通过已验证")
            if not any(
                artifact.source == "pdf"
                and isinstance(artifact.page, int)
                and artifact.page > 0
                for artifact in run.artifacts
            ):
                raise ValueError("已验证事实必须具有正页码 PDF 证据")
            # One report-period revision cycle is represented by one year; this
            # preserves a review point without silently becoming permanent memory.
            return self._append("fact", run_id, fact.to_dict(), expires_after_days=365)

    def save_artifact(self, artifact: EvidenceArtifact, run_id: str) -> MemoryEntry:
        """Save a validated external URL or an available PDF artifact from its run."""
        if not isinstance(artifact, EvidenceArtifact):
            raise ValueError("artifact must be an EvidenceArtifact")
        with self._lock:
            if artifact.source == "pdf":
                run = self._source_run(run_id)
                if artifact.availability != "available" or artifact not in run.artifacts:
                    raise ValueError("PDF 证据必须是来源运行中现有的可用 artifact")
                return self._append("artifact", run_id, artifact.to_dict(), expires_after_days=None)
            # EvidenceArtifact validates http(s) URL and fetched_at at construction.
            return self._append("artifact", run_id, artifact.to_dict(), expires_after_days=30)

    def save_decision(self, text: str, run_id: str, evidence_ids: tuple[str, ...]) -> MemoryEntry:
        """Save an explicit user decision that cites at least one evidence id."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("研究决策不能为空")
        try:
            ids = _strings(evidence_ids, "决策证据")
        except ValueError as exc:
            raise ValueError("研究决策必须引用至少一条证据") from exc
        with self._lock:
            return self._append(
                "decision",
                run_id,
                {"text": text.strip(), "evidence_ids": list(ids)},
                expires_after_days=None,
            )

    def revoke(self, entry_id: str) -> bool:
        """Mark an entry revoked without deleting its audit record."""
        if not isinstance(entry_id, str) or not entry_id:
            return False
        with self._lock:
            for index, entry in enumerate(self._entries):
                if entry.id != entry_id or entry.revoked_at is not None:
                    continue
                self._entries[index] = MemoryEntry(
                    id=entry.id,
                    kind=entry.kind,
                    source_run_id=entry.source_run_id,
                    payload=entry.payload,
                    created_at=entry.created_at,
                    expires_at=entry.expires_at,
                    revoked_at=_now(),
                )
                self._save()
                return True
        return False

    def list_entries(self) -> list[MemoryEntry]:
        with self._lock:
            return list(self._entries)

    def list_active(self) -> list[MemoryEntry]:
        """Return non-revoked, non-expired entries; audit entries remain on disk."""
        now = datetime.now(timezone.utc)
        with self._lock:
            return [
                entry for entry in self._entries
                if entry.revoked_at is None
                and (entry.expires_at is None or _parse_time(entry.expires_at, "expires_at") > now)
            ]
