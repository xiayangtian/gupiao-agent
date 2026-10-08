"""Shared, fail-closed identities for immutable evidence artifacts.

This module deliberately depends only on data-contract attributes.  It must remain
usable by memory, export, and evaluation code without importing server or storage
modules.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

if TYPE_CHECKING:
    from webapp.chat_models import AnswerRun, EvidenceArtifact, Fact


_PDF_URL_RE = re.compile(
    r"^/api/history-pdf/([^/?#]+)\?jump=(0|[1-9]\d*)#page=([1-9]\d*)$"
)


def make_fact_id(
    metric: str,
    value: float,
    unit: str,
    period: str,
    period_kind: str,
    entity_scope: str,
    company_code: str,
    evidence_ids: tuple[str, ...],
    source_category: str = "unknown",
    derived_from_ids: tuple[str, ...] = (),
    formula: str = "",
) -> str:
    """Build a deterministic stable ID from normalized fact identity fields."""
    payload = {
        "company_code": company_code,
        "entity_scope": entity_scope,
        "evidence_ids": sorted(evidence_ids),
        "metric": metric,
        "period": period,
        "period_kind": period_kind,
        "source_category": source_category,
        "derived_from_ids": sorted(derived_from_ids),
        "formula": formula,
        "unit": unit,
        "value": value,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"fact_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:20]}"


def validated_pdf_url(artifact: "EvidenceArtifact") -> str | None:
    """Return only a safe, page-matched local history-PDF URL."""
    url = getattr(artifact, "pdf_url", None)
    page = getattr(artifact, "page", None)
    if getattr(artifact, "source", None) != "pdf" or not isinstance(url, str) or not isinstance(page, int):
        return None
    match = _PDF_URL_RE.fullmatch(url)
    if match is None or int(match.group(3)) != page:
        return None
    filename = unquote(match.group(1))
    if (
        not filename
        or filename != os.path.basename(filename)
        or "/" in filename
        or "\\" in filename
        or not filename.lower().endswith(".pdf")
        or any(ord(char) < 32 for char in filename)
    ):
        return None
    return url


def artifact_evidence_ids(artifact: "EvidenceArtifact") -> tuple[str, ...]:
    """Return immutable artifact identities, excluding untrusted PDF URLs."""
    if getattr(artifact, "source", None) == "pdf":
        page = getattr(artifact, "page", None)
        report_id = getattr(artifact, "report_id", "")
        identifiers = []
        if isinstance(page, int) and not isinstance(page, bool) and page > 0 and report_id:
            identifiers.append(f"{report_id}#p{page}")
        pdf_url = validated_pdf_url(artifact)
        if pdf_url:
            identifiers.append(pdf_url)
        pdf_filename = getattr(artifact, "pdf_filename", "")
        if pdf_filename:
            identifiers.append(pdf_filename)
        return tuple(identifiers)
    url = getattr(artifact, "url", "")
    return (url,) if getattr(artifact, "source", None) == "web" and isinstance(url, str) and url else ()


def pdf_evidence_index(run: "AnswerRun") -> dict[str, "EvidenceArtifact"]:
    """Map identities to their immutable, positive-page PDF artifacts."""
    index: dict[str, EvidenceArtifact] = {}
    for artifact in run.artifacts:
        page = getattr(artifact, "page", None)
        if (
            getattr(artifact, "source", None) != "pdf"
            or isinstance(page, bool)
            or not isinstance(page, int)
            or page <= 0
        ):
            continue
        for identifier in artifact_evidence_ids(artifact):
            index.setdefault(identifier, artifact)
    return index


def fact_is_backed_by_pdf(fact: "Fact", run: "AnswerRun") -> bool:
    """Require base facts and every derived input to resolve to positive-page PDFs."""
    index = pdf_evidence_index(run)
    facts = {item.id: item for item in getattr(run, "facts", ()) if getattr(item, "id", "")}
    visiting: set[str] = set()

    def backed(candidate: "Fact") -> bool:
        evidence_ids: Any = getattr(candidate, "evidence_ids", ())
        if not evidence_ids:
            return False
        if getattr(candidate, "source_type", None) == "pdf":
            return all(identifier in index for identifier in evidence_ids)
        if (getattr(candidate, "source_type", None) != "derived"
                or getattr(candidate, "source_category", None) != "local_pdf"
                or not getattr(candidate, "derived_from_ids", ())
                or candidate.id in visiting):
            return False
        visiting.add(candidate.id)
        parents = [facts.get(identifier) for identifier in candidate.derived_from_ids]
        if any(parent is None or not backed(parent) for parent in parents):
            visiting.discard(candidate.id)
            return False
        parent_evidence = {identifier for parent in parents for identifier in parent.evidence_ids}
        visiting.discard(candidate.id)
        return set(evidence_ids) == parent_evidence

    return backed(fact)
