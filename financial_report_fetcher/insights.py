"""基于证据动态规划、评分并约束财报洞察内容。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

from .evidence.models import EntityScope, EvidenceRecord, VerificationState


def _bounded(value: int, upper: int) -> int:
    return max(0, min(int(value), upper))


@dataclass(frozen=True)
class InsightCandidate:
    candidate_id: str
    title: str
    summary: str
    interest_tags: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    materiality_score: int
    clarity_score: int

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.title.strip():
            raise ValueError("candidate_id 和 title 不能为空")
        object.__setattr__(self, "materiality_score", _bounded(self.materiality_score, 20))
        object.__setattr__(self, "clarity_score", _bounded(self.clarity_score, 15))


@dataclass(frozen=True)
class InsightScore:
    evidence_sufficiency: int
    source_reliability: int
    materiality: int
    clarity: int
    interest_relevance: int
    disqualifiers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field, upper in (
            ("evidence_sufficiency", 30),
            ("source_reliability", 25),
            ("materiality", 20),
            ("clarity", 15),
            ("interest_relevance", 10),
        ):
            value = getattr(self, field)
            if not 0 <= value <= upper:
                raise ValueError(f"{field} 必须在 0 到 {upper} 之间")

    @property
    def total(self) -> int:
        return (
            self.evidence_sufficiency
            + self.source_reliability
            + self.materiality
            + self.clarity
            + self.interest_relevance
        )


@dataclass(frozen=True)
class InsightFinding:
    claim: str
    significance: str
    evidence_ids: tuple[str, ...]
    highlight_spans: tuple[str, ...]
    risk_state: str = "neutral"
    has_disclosed_content: bool = True


@dataclass(frozen=True)
class InsightSection:
    section_id: str
    title: str
    summary: str
    findings: tuple[InsightFinding, ...]
    score: InsightScore
    verification_state: VerificationState

    @property
    def is_detailed_eligible(self) -> bool:
        return len(self.findings) >= 3 and not self.score.disqualifiers


@dataclass(frozen=True)
class RankedInsights:
    details: tuple[InsightCandidate, ...]
    observations: tuple[InsightCandidate, ...]
    filtered: tuple[InsightCandidate, ...]


class InsightPlanner:
    """接收模型生成的动态候选，不内置行业或主题模板。"""

    def __init__(
        self,
        generator: Callable[[Mapping[str, EvidenceRecord], Sequence[str]], Sequence[Mapping[str, Any]]],
    ):
        self._generator = generator

    def plan(
        self,
        evidence: Mapping[str, EvidenceRecord],
        interests: Sequence[str] = (),
    ) -> tuple[InsightCandidate, ...]:
        raw_candidates = self._generator(evidence, interests)
        candidates: list[InsightCandidate] = []
        seen: set[str] = set()
        for raw in raw_candidates:
            candidate_id = str(raw.get("candidate_id", "")).strip()
            title = str(raw.get("title", "")).strip()
            if not candidate_id or not title or candidate_id in seen:
                continue
            known_ids = tuple(
                str(item) for item in raw.get("evidence_ids", ()) if str(item) in evidence
            )
            if not known_ids:
                continue
            candidates.append(InsightCandidate(
                candidate_id=candidate_id,
                title=title,
                summary=str(raw.get("summary", "")).strip(),
                interest_tags=tuple(str(item).strip() for item in raw.get("interest_tags", ()) if str(item).strip()),
                evidence_ids=known_ids,
                materiality_score=int(raw.get("materiality_score", 0)),
                clarity_score=int(raw.get("clarity_score", 0)),
            ))
            seen.add(candidate_id)
        return tuple(candidates)


class InsightScorer:
    """只用本地证据状态计算证据、来源与关注相关性分数。"""

    def score(
        self,
        candidate: InsightCandidate,
        evidence: Mapping[str, EvidenceRecord],
        interests: Sequence[str] = (),
    ) -> InsightScore:
        records = [evidence[item] for item in candidate.evidence_ids if item in evidence]
        disqualifiers: list[str] = []
        if not records:
            disqualifiers.append("缺少证据")
        if any(
            not (record.source_locator.provider or record.source_locator.page or record.source_locator.record_id)
            for record in records
        ):
            disqualifiers.append("证据缺少定位")
        scopes = {record.entity_scope for record in records}
        if EntityScope.UNKNOWN in scopes:
            disqualifiers.append("主体范围未知")
        if len(scopes - {EntityScope.UNKNOWN}) > 1:
            disqualifiers.append("主体范围混用")
        if any(record.verification_state is VerificationState.CONFLICT for record in records):
            disqualifiers.append("重大冲突")

        states = {record.verification_state for record in records}
        if VerificationState.VERIFIED in states:
            reliability = 25
        elif VerificationState.SINGLE_SOURCE in states:
            reliability = 16
        elif VerificationState.UNKNOWN_SCOPE in states:
            reliability = 8
        else:
            reliability = 0
        normalized_interests = {item.strip() for item in interests if item.strip()}
        relevance = 10 if normalized_interests.intersection(candidate.interest_tags) else 0
        return InsightScore(
            evidence_sufficiency=min(len({item for item in candidate.evidence_ids if item in evidence}) * 10, 30),
            source_reliability=reliability,
            materiality=candidate.materiality_score,
            clarity=candidate.clarity_score,
            interest_relevance=relevance,
            disqualifiers=tuple(dict.fromkeys(disqualifiers)),
        )


def filter_and_rank(
    scored: Sequence[tuple[InsightCandidate, InsightScore]],
    *,
    detail_threshold: int,
    observation_threshold: int,
    limit: int,
) -> RankedInsights:
    if observation_threshold > detail_threshold:
        raise ValueError("observation_threshold 不能高于 detail_threshold")
    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    ordered = sorted(
        scored,
        key=lambda item: (-item[1].total, -item[0].materiality_score, item[0].candidate_id),
    )
    details: list[InsightCandidate] = []
    observations: list[InsightCandidate] = []
    filtered: list[InsightCandidate] = []
    for candidate, score in ordered:
        if score.total >= detail_threshold and not score.disqualifiers and len(details) < limit:
            details.append(candidate)
        elif score.total >= observation_threshold:
            observations.append(candidate)
        else:
            filtered.append(candidate)
    return RankedInsights(tuple(details), tuple(observations), tuple(filtered))


_MISSING_MARKERS = ("未披露", "无数据", "不适用", "暂无")


def sanitize_section(section: InsightSection) -> InsightSection:
    findings: list[InsightFinding] = []
    for finding in section.findings:
        claim = finding.claim.strip()
        significance = finding.significance.strip()
        if not finding.has_disclosed_content or not claim or not significance:
            continue
        if any(marker in claim and marker in significance for marker in _MISSING_MARKERS):
            continue
        findings.append(replace(
            finding,
            claim=claim,
            significance=significance,
            evidence_ids=tuple(dict.fromkeys(finding.evidence_ids)),
            highlight_spans=tuple(item for item in finding.highlight_spans if item.strip())[:2],
            risk_state="verified_risk" if finding.risk_state == "verified_risk" else "neutral",
        ))
        if len(findings) == 5:
            break
    return replace(section, findings=tuple(findings))
