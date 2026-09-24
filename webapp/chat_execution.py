"""Projection of run-scoped source results into persisted answer evidence and status."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from webapp.chat_evidence import EvidenceNormalizer
from webapp.chat_models import EvidenceArtifact, Fact, Scope, ToolArtifact
from webapp.source_runtime import SourceResult


@dataclass(frozen=True)
class SourceProjection:
    artifacts: tuple[EvidenceArtifact, ...]
    tool_artifacts: tuple[ToolArtifact, ...]
    facts: tuple[Fact, ...]
    source_summary: Mapping[str, str]
    had_failure: bool


def resolve_answer_status(*, stopped: bool, failed: bool, waiting_consent: bool,
                          required_missing: bool, had_source_failure: bool,
                          retrieval_degraded: bool, verification: str | None) -> str:
    if stopped:
        return "stopped"
    if failed:
        return "failed"
    if waiting_consent:
        return "waiting_consent"
    if (required_missing or had_source_failure or retrieval_degraded
            or verification != "passed"):
        return "partial"
    return "completed"


def project_sources(results: Sequence[SourceResult], scope: Scope) -> SourceProjection:
    del scope  # Scope is an explicit API input: authorization was frozen before execution.
    normalizer = EvidenceNormalizer()
    unique: list[SourceResult] = []
    seen: set[str] = set()
    for result in results:
        if result.call_id and result.call_id in seen:
            continue
        if result.call_id:
            seen.add(result.call_id)
        unique.append(result)

    evidence: list[EvidenceArtifact] = []
    tools: list[ToolArtifact] = []
    facts: list[Fact] = []
    states: dict[str, list[str]] = {"local_pdf": [], "market_data": [], "web": []}
    had_failure = False
    for result in unique:
        key = "local_pdf" if result.category == "local" else ("web" if result.category == "web" else "market_data")
        status = result.status
        if status == "success" and result.category == "market" and not result.as_of:
            status = "partial"
        states[key].append(status)
        if status in {"failed", "unavailable", "partial"}:
            had_failure = True
        evidence.extend(result.artifacts)
        evidence.extend(normalizer.normalize_web_sources(result.payload or (), result.fetched_at)
                        if result.category == "web" and result.fetched_at else ())
        provider = result.provider if result.provider in {"mcp", "tencent", "web", "local"} else "other"
        artifact = ToolArtifact(
            provider=provider,
            tool_name=result.operation[:120] or "source",
            as_of=result.as_of if status == "success" else "",
            status=status,
            result_summary=result.content[:500] if status in {"success", "partial"} else result.error_code[:120],
            fetched_at=result.fetched_at,
            source_id=result.call_id,
        )
        tools.append(artifact)
        facts.extend(normalizer.facts_from_structured_tool_payload(result.payload, artifact))

    summary: dict[str, str] = {}
    for key, values in states.items():
        if not values:
            summary[key] = "未使用"
        elif all(value == "success" for value in values):
            summary[key] = "已使用"
        elif any(value in {"success", "partial"} for value in values):
            summary[key] = "部分取得"
        elif all(value == "unavailable" for value in values):
            summary[key] = "不可用"
        else:
            summary[key] = "获取失败"
    return SourceProjection(tuple(evidence), tuple(tools), tuple(facts), summary, had_failure)
