"""Projection of run-scoped source results into persisted answer evidence and status."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from webapp.chat_evidence import EvidenceNormalizer
from webapp.chat_facts import facts_from_market_result
from webapp.chat_models import EvidenceArtifact, Fact, Scope, ToolArtifact
from webapp.source_adapters import SourceAccess, normalize_source
from webapp.source_runtime import AnswerContext, CallBudget, SourceCall, SourceResult, SourceRuntime


@dataclass(frozen=True)
class SourceProjection:
    artifacts: tuple[EvidenceArtifact, ...]
    tool_artifacts: tuple[ToolArtifact, ...]
    facts: tuple[Fact, ...]
    source_summary: Mapping[str, str]
    had_failure: bool


def build_source_runtime(*, scope: Scope, policy: object, cfg: object, use_mcp: bool,
                         source_mode: str, access: SourceAccess, control: object | None = None) -> SourceRuntime:
    configured = getattr(cfg, "mcp_max_tool_calls", 1)
    if isinstance(configured, bool) or not isinstance(configured, int):
        configured = 1
    policy_limit = getattr(policy, "max_calls", configured)
    if isinstance(policy_limit, bool) or not isinstance(policy_limit, int):
        policy_limit = configured
    if source_mode == "market_recap":
        total = min(max(0, configured), 9)
        market, web = min(total, 7), min(total, 2)
    else:
        total = min(max(0, configured), max(0, policy_limit))
        market, web = total, total
    def authorize(call):
        if call.provider == "mcp" and not use_mcp:
            return False
        return access.permits(call)
    return SourceRuntime(scope, CallBudget(total, market, web), authorize, control=control)


def execute_source(runtime: SourceRuntime, call: SourceCall, invoke, *, fetched_at: str) -> SourceResult:
    return runtime.call(call, lambda: normalize_source(call, invoke(), fetched_at=fetched_at))


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
    # Scope was frozen before execution; use it only to prevent foreign ticker facts from entering the run.
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
        if result.category == "local":
            continue
        provider = result.provider if result.provider in {"mcp", "tencent", "web", "local"} else "other"
        coverage_summary = result.coverage.summary()
        artifact_summary = result.content[:400] if status in {"success", "partial"} else result.error_code[:120]
        if coverage_summary:
            artifact_summary = ("覆盖：" + coverage_summary + ("；" + artifact_summary if artifact_summary else ""))[:500]
        artifact = ToolArtifact(
            provider=provider,
            tool_name=result.operation[:120] or "source",
            as_of=result.as_of if status in {"success", "partial"} else "",
            status=status,
            result_summary=artifact_summary,
            fetched_at=result.fetched_at,
            source_id=result.call_id,
        )
        tools.append(artifact)
        if result.provider == "tencent" and result.operation == "kline":
            market_facts = facts_from_market_result(result, artifact)
            if scope.mode != "whole_corpus":
                allowed_codes = {company.code for company in scope.companies}
                market_facts = tuple(fact for fact in market_facts if fact.company_code in allowed_codes)
            facts.extend(market_facts)
        else:
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
