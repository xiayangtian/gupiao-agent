"""渐进式财报分析 v3 结果对象、兼容读取与原子持久化。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .evidence.models import (
    EntityScope,
    SourceLocator,
    SourceType,
    VerificationState,
)
from .insights import InsightCandidate, InsightFinding, InsightScore, InsightSection


@dataclass(frozen=True)
class QuickConclusion:
    conclusion_id: str
    claim: str
    key_data: str
    significance: str
    evidence_ids: tuple[str, ...]
    verification_state: VerificationState

    def to_dict(self) -> dict[str, Any]:
        return {
            "conclusion_id": self.conclusion_id,
            "claim": self.claim,
            "key_data": self.key_data,
            "significance": self.significance,
            "evidence_ids": list(self.evidence_ids),
            "verification_state": self.verification_state.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QuickConclusion":
        return cls(
            str(data["conclusion_id"]), str(data["claim"]), str(data.get("key_data", "")),
            str(data.get("significance", "")), tuple(data.get("evidence_ids", ())),
            VerificationState(data["verification_state"]),
        )


@dataclass(frozen=True)
class QuickCorrection:
    conclusion_id: str
    before: str
    after: str
    reason: str
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "evidence_ids": list(self.evidence_ids)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QuickCorrection":
        return cls(
            str(data["conclusion_id"]), str(data["before"]), str(data["after"]),
            str(data["reason"]), tuple(data.get("evidence_ids", ())),
        )


@dataclass
class QuickResult:
    conclusions: list[QuickConclusion] = field(default_factory=list)
    observations: list[InsightCandidate] = field(default_factory=list)
    corrections: list[QuickCorrection] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conclusions": [item.to_dict() for item in self.conclusions],
            "observations": [_candidate_to_dict(item) for item in self.observations],
            "corrections": [item.to_dict() for item in self.corrections],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QuickResult":
        return cls(
            conclusions=[QuickConclusion.from_dict(item) for item in data.get("conclusions", ())],
            observations=[_candidate_from_dict(item) for item in data.get("observations", ())],
            corrections=[QuickCorrection.from_dict(item) for item in data.get("corrections", ())],
        )


@dataclass(frozen=True)
class EvidenceReference:
    entity_scope: EntityScope
    period: str
    value: str | None
    unit: str | None
    source_type: SourceType
    source_locator: SourceLocator
    verification_state: VerificationState

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_scope": self.entity_scope.value,
            "period": self.period,
            "value": self.value,
            "unit": self.unit,
            "source_type": self.source_type.value,
            "source_locator": self.source_locator.to_dict(),
            "verification_state": self.verification_state.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceReference":
        return cls(
            EntityScope(data["entity_scope"]), str(data["period"]), data.get("value"),
            data.get("unit"), SourceType(data["source_type"]),
            SourceLocator.from_dict(data["source_locator"]),
            VerificationState(data["verification_state"]),
        )


@dataclass(frozen=True)
class FilteredTopic:
    candidate_id: str
    title: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class EvidenceSummary:
    total: int = 0
    verified: int = 0
    single_source: int = 0
    conflicts: int = 0
    unknown_scope: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class AnalysisError:
    stage: str
    item_id: str | None
    code: str
    message: str
    retryable: bool

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class AnalysisDocument:
    schema_version: int
    analysis_id: str
    report_id: str
    interests: list[str]
    stage: str
    quick: QuickResult | None
    sections: list[InsightSection]
    observations: list[InsightCandidate]
    filtered_topics: list[FilteredTopic]
    evidence_catalog: dict[str, EvidenceReference]
    evidence_summary: EvidenceSummary
    errors: list[AnalysisError]
    created_at: str
    updated_at: str
    company_code: str = ""
    company_name: str = ""
    period: str = ""
    source_file: str = ""

    @property
    def legacy(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "analysis_id": self.analysis_id,
            "report_id": self.report_id,
            "interests": list(self.interests),
            "stage": self.stage,
            "quick": None if self.quick is None else self.quick.to_dict(),
            "sections": [_section_to_dict(item) for item in self.sections],
            "observations": [_candidate_to_dict(item) for item in self.observations],
            "filtered_topics": [item.to_dict() for item in self.filtered_topics],
            "evidence_catalog": {
                key: value.to_dict() for key, value in self.evidence_catalog.items()
            },
            "evidence_summary": self.evidence_summary.to_dict(),
            "errors": [item.to_dict() for item in self.errors],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "meta": {
                "company": self.company_name,
                "company_code": self.company_code,
                "period": self.period,
                "source_file": self.source_file,
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnalysisDocument":
        if int(data.get("schema_version", 0)) != 3:
            raise ValueError("不是受支持的 v3 分析文档")
        meta = data.get("meta") if isinstance(data.get("meta"), Mapping) else {}
        summary = data.get("evidence_summary") or {}
        return cls(
            schema_version=3,
            analysis_id=str(data["analysis_id"]),
            report_id=str(data["report_id"]),
            interests=list(data.get("interests", ())),
            stage=str(data.get("stage", "completed")),
            quick=None if data.get("quick") is None else QuickResult.from_dict(data["quick"]),
            sections=[_section_from_dict(item) for item in data.get("sections", ())],
            observations=[_candidate_from_dict(item) for item in data.get("observations", ())],
            filtered_topics=[FilteredTopic(**item) for item in data.get("filtered_topics", ())],
            evidence_catalog={
                str(key): EvidenceReference.from_dict(value)
                for key, value in data.get("evidence_catalog", {}).items()
            },
            evidence_summary=EvidenceSummary(**summary),
            errors=[AnalysisError(**item) for item in data.get("errors", ())],
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            company_code=str(meta.get("company_code", "")),
            company_name=str(meta.get("company", "")),
            period=str(meta.get("period", "")),
            source_file=str(meta.get("source_file", "")),
        )


@dataclass(frozen=True)
class LegacyAnalysisDocument:
    raw: dict[str, Any]
    legacy: bool = True

    def to_dict(self) -> dict[str, Any]:
        return self.raw


def _candidate_to_dict(candidate: InsightCandidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id, "title": candidate.title,
        "summary": candidate.summary, "interest_tags": list(candidate.interest_tags),
        "evidence_ids": list(candidate.evidence_ids),
        "materiality_score": candidate.materiality_score,
        "clarity_score": candidate.clarity_score,
    }


def _candidate_from_dict(data: Mapping[str, Any]) -> InsightCandidate:
    return InsightCandidate(
        str(data["candidate_id"]), str(data["title"]), str(data.get("summary", "")),
        tuple(data.get("interest_tags", ())), tuple(data.get("evidence_ids", ())),
        int(data.get("materiality_score", 0)), int(data.get("clarity_score", 0)),
    )


def _score_to_dict(score: InsightScore) -> dict[str, Any]:
    return {
        "evidence_sufficiency": score.evidence_sufficiency,
        "source_reliability": score.source_reliability,
        "materiality": score.materiality,
        "clarity": score.clarity,
        "interest_relevance": score.interest_relevance,
        "disqualifiers": list(score.disqualifiers),
    }


def _section_to_dict(section: InsightSection) -> dict[str, Any]:
    return {
        "section_id": section.section_id, "title": section.title, "summary": section.summary,
        "findings": [
            {
                "claim": item.claim, "significance": item.significance,
                "evidence_ids": list(item.evidence_ids),
                "highlight_spans": list(item.highlight_spans),
                "risk_state": item.risk_state,
                "has_disclosed_content": item.has_disclosed_content,
            }
            for item in section.findings
        ],
        "score": _score_to_dict(section.score),
        "verification_state": section.verification_state.value,
    }


def _section_from_dict(data: Mapping[str, Any]) -> InsightSection:
    score = dict(data["score"])
    score["disqualifiers"] = tuple(score.get("disqualifiers", ()))
    return InsightSection(
        str(data["section_id"]), str(data["title"]), str(data.get("summary", "")),
        tuple(InsightFinding(
            str(item["claim"]), str(item.get("significance", "")),
            tuple(item.get("evidence_ids", ())), tuple(item.get("highlight_spans", ())),
            str(item.get("risk_state", "neutral")), bool(item.get("has_disclosed_content", True)),
        ) for item in data.get("findings", ())),
        InsightScore(**score), VerificationState(data["verification_state"]),
    )


def load_analysis_document(path: str | os.PathLike[str]) -> AnalysisDocument | LegacyAnalysisDocument:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("分析文件顶层必须是对象")
    if "schema_version" not in data or int(data.get("schema_version", 0)) < 3:
        return LegacyAnalysisDocument(data)
    return AnalysisDocument.from_dict(data)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _to_markdown(document: AnalysisDocument) -> str:
    lines = [f"# {document.company_name or document.report_id} 财报分析", ""]
    if document.quick:
        lines.extend(["## 快速结论", ""])
        for item in document.quick.conclusions:
            lines.append(f"- **{item.claim}**：{item.key_data}。{item.significance}")
        lines.append("")
    for section in document.sections:
        lines.extend([f"## {section.title}", "", section.summary, ""])
        for finding in section.findings:
            lines.append(f"- {finding.claim}：{finding.significance}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_analysis_document(
    document: AnalysisDocument,
    json_path: str | os.PathLike[str],
    markdown_path: str | os.PathLike[str],
) -> None:
    _atomic_write(
        Path(json_path),
        json.dumps(document.to_dict(), ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_write(Path(markdown_path), _to_markdown(document))
