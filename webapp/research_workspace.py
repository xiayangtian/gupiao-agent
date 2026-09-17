"""Derived, metadata-only research workspace for persisted trusted-chat runs.

The workspace never copies message bodies, PDF snippets, or external result bodies.
Its only mutable state is a small sidecar of favorites keyed by the immutable run
and its owning session.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from webapp.chat_models import AnswerRun
from webapp.chat_store import ChatStore, ChatSessionRun

DEFAULT_PATH = "data/research_workspace.json"


def _utc_instant(value: object) -> datetime | None:
    """Parse one persisted timestamp as an aware UTC instant, or ``None``.

    ``ChatStore`` writes naive local ``updated_at`` values while ``AnswerRun`` and
    ``ResearchRun`` carry offset-aware timestamps.  A naive value is read as UTC
    because it is only used for ordering and display, and an unparseable value is
    ignored rather than allowed to win the comparison.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_timestamp(*values: object) -> str:
    """Return the newest value as a normalized UTC ISO timestamp ("" if none)."""
    instants = [instant for value in values if (instant := _utc_instant(value)) is not None]
    if not instants:
        return ""
    return max(instants).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ResearchWorkspaceItem:
    session_id: str
    run_id: str
    title: str
    company_codes: tuple[str, ...]
    industry: str
    periods: tuple[str, ...]
    intent: str
    status: str
    updated_at: str
    favorite: bool
    searchable_summary: str = ""
    industry_provider: str = ""
    evidence_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "title": self.title,
            "company_codes": list(self.company_codes),
            "industry": self.industry,
            "periods": list(self.periods),
            "intent": self.intent,
            "status": self.status,
            "updated_at": self.updated_at,
            "favorite": self.favorite,
            "searchable_summary": self.searchable_summary,
            "industry_provider": self.industry_provider,
            "evidence_available": self.evidence_available,
        }


@dataclass(frozen=True)
class ResearchWorkspaceQuery:
    company_code: str | None = None
    industry: str | None = None
    period: str | None = None
    intent: str | None = None
    status: str | None = None
    text: str | None = None
    favorite_only: bool = False


class ResearchWorkspaceStore:
    """Lazily index immutable run metadata and persist favorites only."""

    def __init__(self, chat_store: ChatStore, path: str | None = None) -> None:
        self.chat_store = chat_store
        self.path = path or DEFAULT_PATH
        self._lock = threading.RLock()
        self._favorites = self._load_favorites()
        self.chat_store.add_session_delete_hook(self._remove_session_favorites)

    def _load_favorites(self) -> set[tuple[str, str]]:
        try:
            with open(self.path, encoding="utf-8") as source:
                payload = json.load(source)
        except (OSError, json.JSONDecodeError):
            return set()
        if not isinstance(payload, Mapping) or not isinstance(payload.get("favorites"), list):
            return set()
        return {
            (entry["session_id"], entry["run_id"])
            for entry in payload["favorites"]
            if isinstance(entry, Mapping)
            and isinstance(entry.get("session_id"), str)
            and entry["session_id"]
            and isinstance(entry.get("run_id"), str)
            and entry["run_id"]
        }

    def _save_favorites(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        payload = {
            "schema_version": 1,
            "favorites": [
                {"session_id": session_id, "run_id": run_id}
                for session_id, run_id in sorted(self._favorites)
            ],
        }
        temporary_path = f"{self.path}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as target:
            json.dump(payload, target, ensure_ascii=False, indent=2)
        os.replace(temporary_path, self.path)

    def _remove_session_favorites(self, session_id: str) -> None:
        """Drop only favorites whose immutable owner was actually deleted."""
        with self._lock:
            remaining = {key for key in self._favorites if key[0] != session_id}
            if remaining != self._favorites:
                self._favorites = remaining
                self._save_favorites()

    @staticmethod
    def _unique(values: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value for value in values if value))

    @staticmethod
    def _periods(report_ids: Iterable[str]) -> tuple[str, ...]:
        return ResearchWorkspaceStore._unique(
            parts[1] for report_id in report_ids
            if len(parts := report_id.split(":")) >= 2 and parts[1]
        )

    @staticmethod
    def _saved_decision_summaries(record: ChatSessionRun) -> tuple[str, ...]:
        """Read only explicitly designated decision summaries from safe M3 metadata."""
        summary = record.run.research_summary
        if not isinstance(summary, Mapping):
            return ()
        values = summary.get("decision_summaries", ())
        if isinstance(values, str):
            values = (values,)
        if not isinstance(values, (tuple, list)):
            return ()
        return tuple(value.strip() for value in values if isinstance(value, str) and value.strip())

    @staticmethod
    def _evidence_available(run: AnswerRun) -> bool:
        """Report whether the immutable run persisted a reopenable evidence artifact.

        Derived from the persisted artifacts only: an unavailable PDF or a run
        migrated from a legacy message kept no evidence a reader can open, and the
        workbench row must not claim otherwise.
        """
        return any(
            artifact.url if artifact.source == "web" else artifact.availability == "available"
            for artifact in run.artifacts
        )

    @classmethod
    def _item_from_record(cls, record: ChatSessionRun, favorites: set[tuple[str, str]]) -> ResearchWorkspaceItem | None:
        run = record.run
        if not run.id:
            # Historic records without a stable AnswerRun identity cannot safely
            # be targeted by favorite/export/memory actions.
            return None
        scope = run.scope
        company_codes = cls._unique(company.code for company in scope.companies) if scope else ()
        industry = scope.industry.name if scope and scope.industry else ""
        provider = scope.industry.provider if scope and scope.industry else ""
        periods = cls._periods(scope.report_ids) if scope else ()
        intent = run.intent_decision.intent if run.intent_decision else ""
        decisions = cls._saved_decision_summaries(record)
        # Normalize before comparing: lexicographic maxima over mixed naive/aware
        # ISO strings can select the older run.
        updated_at = _utc_timestamp(
            run.completed_at,
            run.created_at,
            record.research_run.finished_at if record.research_run else "",
            record.research_run.started_at if record.research_run else "",
            record.updated_at,
        )
        return ResearchWorkspaceItem(
            session_id=record.session_id,
            run_id=run.id,
            title=record.title,
            company_codes=company_codes,
            industry=industry,
            periods=periods,
            intent=intent,
            status=run.status,
            updated_at=updated_at,
            favorite=(record.session_id, run.id) in favorites,
            searchable_summary="\n".join(decisions),
            industry_provider=provider,
            evidence_available=cls._evidence_available(run),
        )

    def _all_items(self) -> list[ResearchWorkspaceItem]:
        records = self.chat_store.iter_session_runs()
        items = [item for record in records if (item := self._item_from_record(record, self._favorites))]
        valid_keys = {(item.session_id, item.run_id) for item in items}
        if self._favorites - valid_keys:
            self._favorites.intersection_update(valid_keys)
            self._save_favorites()
            # The returned items must reflect pruning without a second source read.
            items = [
                ResearchWorkspaceItem(**{**item.__dict__, "favorite": (item.session_id, item.run_id) in self._favorites})
                for item in items
            ]
        return items

    @staticmethod
    def _matches(item: ResearchWorkspaceItem, query: ResearchWorkspaceQuery) -> bool:
        if query.company_code and query.company_code not in item.company_codes:
            return False
        if query.industry and query.industry not in {item.industry, item.industry_provider}:
            return False
        if query.period and query.period not in item.periods:
            return False
        if query.intent and query.intent != item.intent:
            return False
        if query.status and query.status != item.status:
            return False
        if query.favorite_only and not item.favorite:
            return False
        if query.text:
            needle = query.text.casefold().strip()
            haystack = f"{item.title}\n{item.searchable_summary}".casefold()
            if needle and needle not in haystack:
                return False
        return True

    def list_items(self, query: ResearchWorkspaceQuery | None = None) -> list[ResearchWorkspaceItem]:
        query = query or ResearchWorkspaceQuery()
        with self._lock:
            items = [item for item in self._all_items() if self._matches(item, query)]
            by_updated_at = sorted(items, key=lambda item: item.updated_at, reverse=True)
            return sorted(by_updated_at, key=lambda item: not item.favorite)

    def set_favorite(self, session_id: str, run_id: str, favorite: bool) -> ResearchWorkspaceItem:
        if not isinstance(favorite, bool):
            raise ValueError("favorite must be a boolean")
        with self._lock:
            item = next(
                (candidate for candidate in self._all_items()
                 if candidate.session_id == session_id and candidate.run_id == run_id),
                None,
            )
            if item is None:
                raise ValueError("workspace run does not exist")
            key = (session_id, run_id)
            if favorite:
                self._favorites.add(key)
            else:
                self._favorites.discard(key)
            self._save_favorites()
            return ResearchWorkspaceItem(**{**item.__dict__, "favorite": favorite})
