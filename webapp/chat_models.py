"""Immutable, JSON-safe data contracts for trusted chat answers.

The models in this module deliberately validate values at the persistence boundary so
historic answers cannot be presented as scoped or evidenced when they are not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from numbers import Real
from typing import Any, ClassVar, Literal, Mapping, Sequence
from urllib.parse import urlparse

ScopeMode = Literal["company_only", "company_industry", "whole_corpus"]
AnswerStatus = Literal["completed", "partial", "stopped", "failed"]
FactVerification = Literal["verified", "reference", "conflict", "unavailable"]

_SCOPE_MODES = frozenset(("company_only", "company_industry", "whole_corpus"))
_ANSWER_STATUSES = frozenset(("completed", "partial", "stopped", "failed"))
_FACT_VERIFICATIONS = frozenset(("verified", "reference", "conflict", "unavailable"))


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _string(value: Any, name: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if required and not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    return tuple(_string(item, f"{name}[]") for item in _sequence(value, name))


@dataclass(frozen=True)
class CompanyRef:
    code: str
    name: str

    def __post_init__(self) -> None:
        _string(self.code, "company code")
        _string(self.name, "company name")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "name": self.name}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompanyRef":
        data = _mapping(data, "company")
        return cls(code=_string(data.get("code"), "company code"), name=_string(data.get("name"), "company name"))


@dataclass(frozen=True)
class IndustryRef:
    """Provider-resolved industry classification; never inferred from model prose."""

    name: str
    provider: str
    resolved_at: str = ""
    sample_kind: str = ""

    def __post_init__(self) -> None:
        _string(self.name, "industry name")
        _string(self.provider, "industry provider")
        _string(self.resolved_at, "industry resolved_at", required=False)
        _string(self.sample_kind, "industry sample_kind", required=False)

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "provider": self.provider,
            "resolved_at": self.resolved_at,
            "sample_kind": self.sample_kind,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IndustryRef":
        data = _mapping(data, "industry")
        return cls(
            name=_string(data.get("name"), "industry name"),
            provider=_string(data.get("provider"), "industry provider"),
            resolved_at=_string(data.get("resolved_at", ""), "industry resolved_at", required=False),
            sample_kind=_string(data.get("sample_kind", ""), "industry sample_kind", required=False),
        )


@dataclass(frozen=True)
class SourcePolicy:
    local_pdf: bool = True
    historical_analysis: bool = True
    market_data: bool = False
    web: bool = False

    def __post_init__(self) -> None:
        for name in ("local_pdf", "historical_analysis", "market_data", "web"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"source_policy.{name} must be a boolean")

    @classmethod
    def local_only(cls) -> "SourcePolicy":
        return cls()

    def to_dict(self) -> dict[str, bool]:
        return {
            "local_pdf": self.local_pdf,
            "historical_analysis": self.historical_analysis,
            "market_data": self.market_data,
            "web": self.web,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourcePolicy":
        data = _mapping(data, "source_policy")
        values = {
            name: data.get(name, default)
            for name, default in cls().to_dict().items()
        }
        return cls(**values)


@dataclass(frozen=True)
class Scope:
    mode: ScopeMode
    companies: tuple[CompanyRef, ...]
    report_ids: tuple[str, ...]
    industry: IndustryRef | None = None
    source_policy: SourcePolicy = field(default_factory=SourcePolicy.local_only)
    fallback_reason: str = ""

    def __post_init__(self) -> None:
        if self.mode not in _SCOPE_MODES:
            raise ValueError(f"scope mode must be one of {sorted(_SCOPE_MODES)}")
        if not isinstance(self.companies, tuple) or not all(isinstance(company, CompanyRef) for company in self.companies):
            raise ValueError("companies must be a tuple of CompanyRef")
        if not isinstance(self.report_ids, tuple):
            raise ValueError("report_ids must be a tuple")
        for report_id in self.report_ids:
            _string(report_id, "report_id")
        if self.industry is not None and not isinstance(self.industry, IndustryRef):
            raise ValueError("industry must be an IndustryRef or null")
        if not isinstance(self.source_policy, SourcePolicy):
            raise ValueError("source_policy must be a SourcePolicy")
        _string(self.fallback_reason, "fallback_reason", required=False)

        if self.mode == "company_only":
            if len(self.companies) != 1 or not self.report_ids:
                raise ValueError("company_only requires exactly one company and at least one report_id")
            if self.industry is not None:
                raise ValueError("company_only must not include industry")
        elif self.mode == "company_industry":
            if len(self.companies) != 1 or self.industry is None or not self.report_ids:
                raise ValueError("company_industry requires one company, an industry source, and report_ids")
        else:
            if self.report_ids:
                raise ValueError("whole_corpus requires empty report_ids")
            if self.companies or self.industry is not None:
                raise ValueError("whole_corpus must not include companies or industry")

    @classmethod
    def company_only(cls, code: str, name: str, report_ids: Sequence[str]) -> "Scope":
        return cls("company_only", (CompanyRef(code, name),), tuple(report_ids))

    @classmethod
    def whole_corpus(cls, *, source_policy: SourcePolicy | None = None) -> "Scope":
        return cls("whole_corpus", (), (), source_policy=source_policy or SourcePolicy.local_only())

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "companies": [company.to_dict() for company in self.companies],
            "report_ids": list(self.report_ids),
            "industry": self.industry.to_dict() if self.industry else None,
            "source_policy": self.source_policy.to_dict(),
            "fallback_reason": self.fallback_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Scope":
        data = _mapping(data, "scope")
        industry_data = data.get("industry")
        if industry_data is not None and not isinstance(industry_data, Mapping):
            raise ValueError("industry must be a JSON object or null")
        return cls(
            mode=_string(data.get("mode"), "scope mode"),
            companies=tuple(CompanyRef.from_dict(item) for item in _sequence(data.get("companies", []), "companies")),
            report_ids=_strings(data.get("report_ids", []), "report_ids"),
            industry=IndustryRef.from_dict(industry_data) if industry_data is not None else None,
            source_policy=SourcePolicy.from_dict(data.get("source_policy", {})),
            fallback_reason=_string(data.get("fallback_reason", ""), "fallback_reason", required=False),
        )


@dataclass(frozen=True)
class Fact:
    metric: str
    value: float
    unit: str
    period: str
    period_kind: str
    entity_scope: str
    company_code: str
    source_type: str
    evidence_ids: tuple[str, ...]
    verification: FactVerification = "unavailable"
    as_of: str = ""

    def __post_init__(self) -> None:
        for name in ("metric", "unit", "period", "period_kind", "entity_scope", "company_code", "source_type"):
            _string(getattr(self, name), name)
        if isinstance(self.value, bool) or not isinstance(self.value, Real):
            raise ValueError("value must be a number")
        if not isinstance(self.evidence_ids, tuple) or not self.evidence_ids:
            raise ValueError("evidence_ids must be a non-empty tuple")
        for evidence_id in self.evidence_ids:
            _string(evidence_id, "evidence_id")
        if self.verification not in _FACT_VERIFICATIONS:
            raise ValueError(f"verification must be one of {sorted(_FACT_VERIFICATIONS)}")
        _string(self.as_of, "as_of", required=False)
        if self.source_type in {"tool", "web"} and not self.as_of and self.verification != "unavailable":
            raise ValueError("external facts without as_of must be unavailable")

    @classmethod
    def from_tool(
        cls,
        *,
        metric: str,
        value: float,
        unit: str,
        tool: "ToolArtifact",
        period: str = "as_of",
        period_kind: str = "point_in_time",
        entity_scope: str = "unknown",
        company_code: str = "unknown",
    ) -> "Fact":
        if not isinstance(tool, ToolArtifact):
            raise ValueError("tool must be a ToolArtifact")
        return cls(
            metric=metric,
            value=value,
            unit=unit,
            period=period,
            period_kind=period_kind,
            entity_scope=entity_scope,
            company_code=company_code,
            source_type="tool",
            evidence_ids=(f"tool:{tool.provider}:{tool.tool_name}",),
            verification="reference" if tool.as_of else "unavailable",
            as_of=tool.as_of,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "period": self.period,
            "period_kind": self.period_kind,
            "entity_scope": self.entity_scope,
            "company_code": self.company_code,
            "source_type": self.source_type,
            "evidence_ids": list(self.evidence_ids),
            "verification": self.verification,
            "as_of": self.as_of,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Fact":
        data = _mapping(data, "fact")
        return cls(
            metric=_string(data.get("metric"), "metric"),
            value=data.get("value"),
            unit=_string(data.get("unit"), "unit"),
            period=_string(data.get("period"), "period"),
            period_kind=_string(data.get("period_kind"), "period_kind"),
            entity_scope=_string(data.get("entity_scope"), "entity_scope"),
            company_code=_string(data.get("company_code"), "company_code"),
            source_type=_string(data.get("source_type"), "source_type"),
            evidence_ids=_strings(data.get("evidence_ids", []), "evidence_ids"),
            verification=_string(data.get("verification", "unavailable"), "verification"),
            as_of=_string(data.get("as_of", ""), "as_of", required=False),
        )


@dataclass(frozen=True)
class EvidenceArtifact:
    source: str
    report_id: str = ""
    pdf_filename: str = ""
    page: int | None = None
    snippet: str = ""
    pdf_url: str | None = None
    availability: str = "available"
    url: str = ""
    title: str = ""
    published_at: str = ""
    fetched_at: str = ""

    _SOURCES: ClassVar[frozenset[str]] = frozenset(("pdf", "web"))

    def __post_init__(self) -> None:
        if self.source not in self._SOURCES:
            raise ValueError("evidence source must be pdf or web")
        for name in ("report_id", "pdf_filename", "snippet", "availability", "url", "title", "published_at", "fetched_at"):
            _string(getattr(self, name), name, required=False)
        if self.pdf_url is not None:
            _string(self.pdf_url, "pdf_url")
        if self.source == "pdf":
            if not self.report_id or not self.pdf_filename or not self.snippet:
                raise ValueError("pdf evidence requires report_id, pdf_filename, and snippet")
            if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page <= 0:
                raise ValueError("pdf evidence page must be a positive integer")
        else:
            if self.page is not None:
                raise ValueError("web evidence must not include page")
            if not self.url or not self.title or not self.snippet or not self.fetched_at:
                raise ValueError("web evidence requires url, title, snippet, and fetched_at")
            parsed = urlparse(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("web evidence url must use http or https")

    @classmethod
    def pdf(
        cls,
        report_id: str,
        pdf_filename: str,
        page: int,
        snippet: str,
        *,
        pdf_url: str | None = None,
        availability: str = "available",
    ) -> "EvidenceArtifact":
        return cls(
            source="pdf",
            report_id=report_id,
            pdf_filename=pdf_filename,
            page=page,
            snippet=snippet,
            pdf_url=pdf_url,
            availability=availability,
        )

    @classmethod
    def web(
        cls,
        url: str,
        title: str,
        snippet: str,
        *,
        published_at: str = "",
        fetched_at: str = "",
    ) -> "EvidenceArtifact":
        return cls(
            source="web",
            url=url,
            title=title,
            snippet=snippet,
            published_at=published_at,
            fetched_at=fetched_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "report_id": self.report_id,
            "pdf_filename": self.pdf_filename,
            "page": self.page,
            "snippet": self.snippet,
            "pdf_url": self.pdf_url,
            "availability": self.availability,
            "url": self.url,
            "title": self.title,
            "published_at": self.published_at,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceArtifact":
        data = _mapping(data, "evidence artifact")
        page = data.get("page")
        if page is not None and (isinstance(page, bool) or not isinstance(page, int)):
            raise ValueError("page must be an integer or null")
        pdf_url = data.get("pdf_url")
        if pdf_url is not None and not isinstance(pdf_url, str):
            raise ValueError("pdf_url must be a string or null")
        return cls(
            source=_string(data.get("source"), "source"),
            report_id=_string(data.get("report_id", ""), "report_id", required=False),
            pdf_filename=_string(data.get("pdf_filename", ""), "pdf_filename", required=False),
            page=page,
            snippet=_string(data.get("snippet", ""), "snippet", required=False),
            pdf_url=pdf_url,
            availability=_string(data.get("availability", "available"), "availability", required=False),
            url=_string(data.get("url", ""), "url", required=False),
            title=_string(data.get("title", ""), "title", required=False),
            published_at=_string(data.get("published_at", ""), "published_at", required=False),
            fetched_at=_string(data.get("fetched_at", ""), "fetched_at", required=False),
        )


@dataclass(frozen=True)
class ToolArtifact:
    provider: str
    tool_name: str
    as_of: str = ""
    status: str = "failed"
    arguments_summary: str = ""
    result_summary: str = ""

    def __post_init__(self) -> None:
        _string(self.provider, "tool provider")
        _string(self.tool_name, "tool_name")
        _string(self.as_of, "as_of", required=False)
        _string(self.status, "tool status")
        _string(self.arguments_summary, "arguments_summary", required=False)
        _string(self.result_summary, "result_summary", required=False)
        if self.status == "success" and not self.as_of:
            raise ValueError("successful tool artifact requires as_of")

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "tool_name": self.tool_name,
            "as_of": self.as_of,
            "status": self.status,
            "arguments_summary": self.arguments_summary,
            "result_summary": self.result_summary,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolArtifact":
        data = _mapping(data, "tool artifact")
        return cls(
            provider=_string(data.get("provider"), "tool provider"),
            tool_name=_string(data.get("tool_name"), "tool_name"),
            as_of=_string(data.get("as_of", ""), "as_of", required=False),
            status=_string(data.get("status", "failed"), "tool status"),
            arguments_summary=_string(data.get("arguments_summary", ""), "arguments_summary", required=False),
            result_summary=_string(data.get("result_summary", ""), "result_summary", required=False),
        )


@dataclass(frozen=True)
class AnswerRun:
    content: str
    status: AnswerStatus
    scope: Scope | None = None
    facts: tuple[Fact, ...] = ()
    artifacts: tuple[EvidenceArtifact, ...] = ()
    tool_artifacts: tuple[ToolArtifact, ...] = ()
    retrieval_report_ids: tuple[str, ...] = ()
    id: str = ""
    created_at: str = ""
    completed_at: str = ""
    elapsed_seconds: float | None = None
    model: str = ""
    legacy_evidence_unavailable: bool = False

    def __post_init__(self) -> None:
        _string(self.content, "content", required=False)
        if self.status not in _ANSWER_STATUSES:
            raise ValueError(f"answer status must be one of {sorted(_ANSWER_STATUSES)}")
        if self.scope is not None and not isinstance(self.scope, Scope):
            raise ValueError("scope must be a Scope or null")
        for name, model_type in (("facts", Fact), ("artifacts", EvidenceArtifact), ("tool_artifacts", ToolArtifact)):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not all(isinstance(value, model_type) for value in values):
                raise ValueError(f"{name} must be a tuple of {model_type.__name__}")
        if not isinstance(self.retrieval_report_ids, tuple):
            raise ValueError("retrieval_report_ids must be a tuple")
        for report_id in self.retrieval_report_ids:
            _string(report_id, "retrieval_report_id")
        for name in ("id", "created_at", "completed_at", "model"):
            _string(getattr(self, name), name, required=False)
        if self.elapsed_seconds is not None and (isinstance(self.elapsed_seconds, bool) or not isinstance(self.elapsed_seconds, Real)):
            raise ValueError("elapsed_seconds must be a number or null")
        if not isinstance(self.legacy_evidence_unavailable, bool):
            raise ValueError("legacy_evidence_unavailable must be a boolean")
        if self.legacy_evidence_unavailable and (self.facts or self.artifacts or self.tool_artifacts):
            raise ValueError("legacy runs must not fabricate evidence artifacts")

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "status": self.status,
            "scope": self.scope.to_dict() if self.scope else None,
            "facts": [fact.to_dict() for fact in self.facts],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "tool_artifacts": [artifact.to_dict() for artifact in self.tool_artifacts],
            "retrieval_report_ids": list(self.retrieval_report_ids),
            "id": self.id,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": self.elapsed_seconds,
            "model": self.model,
            "legacy_evidence_unavailable": self.legacy_evidence_unavailable,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnswerRun":
        data = _mapping(data, "answer run")
        scope_data = data.get("scope")
        if scope_data is not None and not isinstance(scope_data, Mapping):
            raise ValueError("scope must be a JSON object or null")
        elapsed_seconds = data.get("elapsed_seconds")
        if elapsed_seconds is not None and (isinstance(elapsed_seconds, bool) or not isinstance(elapsed_seconds, Real)):
            raise ValueError("elapsed_seconds must be a number or null")
        legacy = data.get("legacy_evidence_unavailable", False)
        if not isinstance(legacy, bool):
            raise ValueError("legacy_evidence_unavailable must be a boolean")
        return cls(
            content=_string(data.get("content", ""), "content", required=False),
            status=_string(data.get("status"), "answer status"),
            scope=Scope.from_dict(scope_data) if scope_data is not None else None,
            facts=tuple(Fact.from_dict(item) for item in _sequence(data.get("facts", []), "facts")),
            artifacts=tuple(EvidenceArtifact.from_dict(item) for item in _sequence(data.get("artifacts", []), "artifacts")),
            tool_artifacts=tuple(ToolArtifact.from_dict(item) for item in _sequence(data.get("tool_artifacts", []), "tool_artifacts")),
            retrieval_report_ids=_strings(data.get("retrieval_report_ids", []), "retrieval_report_ids"),
            id=_string(data.get("id", ""), "id", required=False),
            created_at=_string(data.get("created_at", ""), "created_at", required=False),
            completed_at=_string(data.get("completed_at", ""), "completed_at", required=False),
            elapsed_seconds=elapsed_seconds,
            model=_string(data.get("model", ""), "model", required=False),
            legacy_evidence_unavailable=legacy,
        )


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str
    run: AnswerRun | None = None

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise ValueError("chat message role must be user or assistant")
        _string(self.content, "content", required=False)
        if self.run is not None and not isinstance(self.run, AnswerRun):
            raise ValueError("run must be an AnswerRun or null")
        if self.role == "user" and self.run is not None:
            raise ValueError("user messages must not include an answer run")
        if self.run is not None and self.content != self.run.content:
            raise ValueError("assistant message content must match answer run content")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.run is not None:
            data["run"] = self.run.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChatMessage":
        data = _mapping(data, "chat message")
        role = _string(data.get("role"), "role")
        content = _string(data.get("content", ""), "content", required=False)
        run_data = data.get("run")
        if run_data is not None:
            return cls(role=role, content=content, run=AnswerRun.from_dict(_mapping(run_data, "run")))
        if role == "assistant":
            return cls(
                role=role,
                content=content,
                run=AnswerRun(
                    content=content,
                    status="completed",
                    legacy_evidence_unavailable=True,
                ),
            )
        return cls(role=role, content=content)
