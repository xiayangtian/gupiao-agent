"""渐进式分析管线的 AI 适配器；模型生成内容，程序约束证据引用。"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from .analysis_config import AnalysisConfig
from .analysis_pipeline import ProgressiveAnalysisPipeline
from .analysis_result import QuickConclusion, QuickResult
from .evidence.document import DocumentExtractor
from .evidence.models import EvidenceRecord, VerificationState
from .evidence.ocr import PaddleStructureEngine
from .evidence.resolver import EvidenceResolver
from .evidence.structured import build_structured_gateway
from .insights import (
    InsightCandidate,
    InsightFinding,
    InsightPlanner,
    InsightScorer,
    InsightSection,
)


def _parse_json(text: str) -> dict[str, Any]:
    content = text.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(lines[1:-1])
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("AI 分析结果必须是 JSON 对象")
    return data


def _evidence_payload(records: Sequence[EvidenceRecord]) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": record.stable_id,
            "fact_name": record.fact_name,
            "value": None if record.value is None else str(record.value),
            "unit": record.unit,
            "currency": record.currency,
            "period": record.period,
            "entity_scope": record.entity_scope.value,
            "verification_state": record.verification_state.value,
            "source_type": record.source_type.value,
            "text": (record.text or "")[:600],
        }
        for record in records[:120]
    ]


def _ask_json(ai_client, *, system: str, prompt: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    text = ai_client.ask(
        json.dumps(prompt, ensure_ascii=False),
        system=system,
        temperature=0.1,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    return _parse_json(text)


class AiQuickAnalyzer:
    def __init__(self, ai_client, max_conclusions: int = 3):
        self.ai_client = ai_client
        self.max_conclusions = max_conclusions

    def analyze(
        self, records: Sequence[EvidenceRecord], interests: Sequence[str]
    ) -> QuickResult:
        data = _ask_json(
            self.ai_client,
            system=(
                "你是财报快速分析器。只基于给定证据输出 JSON；不得补全未披露数据。"
                "格式为 conclusions 数组，每项包含 conclusion_id、claim、key_data、"
                "significance、evidence_ids。优先用户关注方向，最多输出指定数量。"
            ),
            prompt={
                "interests": list(interests),
                "max_conclusions": self.max_conclusions,
                "evidence": _evidence_payload(records),
            },
            max_tokens=1800,
        )
        evidence = {record.stable_id: record for record in records}
        conclusions: list[QuickConclusion] = []
        for raw in data.get("conclusions", ()):
            if not isinstance(raw, Mapping):
                continue
            ids = tuple(dict.fromkeys(
                str(item) for item in raw.get("evidence_ids", ()) if str(item) in evidence
            ))
            cited = [evidence[item] for item in ids]
            if not cited or any(
                item.verification_state in {
                    VerificationState.CONFLICT,
                    VerificationState.UNKNOWN_SCOPE,
                }
                for item in cited
            ):
                continue
            conclusion_id = str(raw.get("conclusion_id", "")).strip()
            claim = str(raw.get("claim", "")).strip()
            if not conclusion_id or not claim:
                continue
            state = (
                VerificationState.VERIFIED
                if all(item.verification_state is VerificationState.VERIFIED for item in cited)
                else VerificationState.SINGLE_SOURCE
            )
            conclusions.append(QuickConclusion(
                conclusion_id=conclusion_id,
                claim=claim,
                key_data=str(raw.get("key_data", "")).strip(),
                significance=str(raw.get("significance", "")).strip(),
                evidence_ids=ids,
                verification_state=state,
            ))
            if len(conclusions) == self.max_conclusions:
                break
        return QuickResult(conclusions=conclusions)

    def correct(self, quick: QuickResult, records: Sequence[EvidenceRecord]):
        return ()


class AiTopicGenerator:
    def __init__(self, ai_client):
        self.ai_client = ai_client

    def __call__(
        self,
        evidence: Mapping[str, EvidenceRecord],
        interests: Sequence[str],
    ) -> list[dict[str, Any]]:
        data = _ask_json(
            self.ai_client,
            system=(
                "你是财报主题规划器。不要使用固定主题模板；根据本次证据动态提出候选。"
                "输出 candidates 数组，每项包含 candidate_id、title、summary、interest_tags、"
                "evidence_ids、materiality_score(0-20)、clarity_score(0-15)。"
            ),
            prompt={
                "interests": list(interests),
                "evidence": _evidence_payload(list(evidence.values())),
            },
            max_tokens=2200,
        )
        known = set(evidence)
        candidates: list[dict[str, Any]] = []
        for raw in data.get("candidates", ()):
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            item["evidence_ids"] = [
                str(value) for value in raw.get("evidence_ids", ()) if str(value) in known
            ]
            if item["evidence_ids"]:
                candidates.append(item)
        return candidates


def _section_state(records: Sequence[EvidenceRecord]) -> VerificationState:
    states = {record.verification_state for record in records}
    for state in (
        VerificationState.CONFLICT,
        VerificationState.UNKNOWN_SCOPE,
        VerificationState.SINGLE_SOURCE,
        VerificationState.VERIFIED,
    ):
        if state in states:
            return state
    return VerificationState.SINGLE_SOURCE


class AiInsightAnalyzer:
    def __init__(self, ai_client, scorer: InsightScorer):
        self.ai_client = ai_client
        self.scorer = scorer

    def analyze(
        self,
        candidate: InsightCandidate,
        records: Sequence[EvidenceRecord],
        interests: Sequence[str],
    ) -> InsightSection:
        evidence = {record.stable_id: record for record in records}
        cited_records = [evidence[item] for item in candidate.evidence_ids if item in evidence]
        data = _ask_json(
            self.ai_client,
            system=(
                "你是财报深度分析器。只分析给定动态主题并引用证据 ID。"
                "输出 findings 数组，每项包含 claim、significance、evidence_ids、"
                "highlight_spans(最多2项)、risk_state；未披露内容不要输出。"
            ),
            prompt={
                "topic": {
                    "id": candidate.candidate_id,
                    "title": candidate.title,
                    "summary": candidate.summary,
                },
                "interests": list(interests),
                "evidence": _evidence_payload(cited_records),
            },
            max_tokens=2600,
        )
        findings: list[InsightFinding] = []
        for raw in data.get("findings", ()):
            if not isinstance(raw, Mapping):
                continue
            ids = tuple(dict.fromkeys(
                str(item) for item in raw.get("evidence_ids", ())
                if str(item) in evidence and str(item) in candidate.evidence_ids
            ))
            claim = str(raw.get("claim", "")).strip()
            significance = str(raw.get("significance", "")).strip()
            if not ids or not claim or not significance:
                continue
            verified_risk = (
                raw.get("risk_state") == "verified_risk"
                and all(evidence[item].verification_state is VerificationState.VERIFIED for item in ids)
            )
            findings.append(InsightFinding(
                claim=claim,
                significance=significance,
                evidence_ids=ids,
                highlight_spans=tuple(
                    str(item).strip() for item in raw.get("highlight_spans", ())
                    if str(item).strip()
                )[:2],
                risk_state="verified_risk" if verified_risk else "neutral",
            ))
        return InsightSection(
            section_id=candidate.candidate_id,
            title=candidate.title,
            summary=candidate.summary,
            findings=tuple(findings),
            score=self.scorer.score(candidate, evidence, interests),
            verification_state=_section_state(cited_records),
        )


def build_progressive_pipeline(
    ai_client,
    output_dir: str,
    config: AnalysisConfig | None = None,
) -> ProgressiveAnalysisPipeline:
    resolved_config = config or AnalysisConfig.load()
    scorer = InsightScorer()
    return ProgressiveAnalysisPipeline(
        structured_gateway=build_structured_gateway(resolved_config),
        document_extractor=DocumentExtractor(min_chars=resolved_config.ocr_min_chars),
        ocr_engine=PaddleStructureEngine(),
        resolver=EvidenceResolver(resolved_config.numeric_relative_tolerance),
        insight_planner=InsightPlanner(AiTopicGenerator(ai_client)),
        insight_scorer=scorer,
        quick_analyzer=AiQuickAnalyzer(
            ai_client, max_conclusions=resolved_config.max_quick_conclusions
        ),
        insight_analyzer=AiInsightAnalyzer(ai_client, scorer),
        output_dir=output_dir,
        config=resolved_config,
    )
