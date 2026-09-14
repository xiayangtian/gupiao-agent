"""智能问答财报补充的授权数据契约与内存状态机。

本模块只管理候选和一次性授权；不执行下载、摄取或任何 HTTP 请求。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from hashlib import sha256
import os
from secrets import token_urlsafe
from typing import Any, Callable, Literal, Mapping, Sequence

from financial_report_fetcher.downloader import ReportDownloader
from financial_report_fetcher.models import DownloadStatus, ReportMeta, ReportType
from financial_report_fetcher.report_identity import build_report_filename, build_report_id
from webapp.chat_models import Scope

SupplementStatus = Literal[
    "proposed", "approved", "declined", "downloading", "ingesting",
    "resuming", "completed", "expired", "failed",
]

_STATUSES = frozenset((
    "proposed", "approved", "declined", "downloading", "ingesting",
    "resuming", "completed", "expired", "failed",
))
_TRANSITIONS = {
    "proposed": frozenset(("expired", "failed")),
    "approved": frozenset(("downloading", "failed")),
    "downloading": frozenset(("ingesting", "failed")),
    "ingesting": frozenset(("resuming", "failed")),
    "resuming": frozenset(("completed", "failed")),
    "declined": frozenset(),
    "completed": frozenset(),
    "expired": frozenset(),
    "failed": frozenset(),
}
_MAX_CANDIDATES = 5


def _non_empty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 不能为空")
    return value.strip()


def _to_iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    value = _non_empty(value, "时间")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("时间必须是 ISO 8601 格式") from exc
    return value


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class SupplementCandidate:
    """服务端解析出的可授权报告身份；故意不含 URL 或本地路径。"""

    id: str
    report_id: str
    code: str
    company: str
    period: str
    report_type: str
    source: str

    def __post_init__(self) -> None:
        for name in ("id", "report_id", "code", "company", "period", "report_type", "source"):
            _non_empty(getattr(self, name), name)

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "report_id": self.report_id,
            "code": self.code,
            "company": self.company,
            "period": self.period,
            "report_type": self.report_type,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SupplementCandidate":
        if not isinstance(data, Mapping):
            raise ValueError("候选必须是 JSON 对象")
        return cls(**{name: _non_empty(data.get(name), name) for name in (
            "id", "report_id", "code", "company", "period", "report_type", "source",
        )})


@dataclass(frozen=True)
class SupplementRequest:
    """单次提问绑定的补充授权请求。"""

    id: str
    session_id: str
    question_digest: str
    scope: Scope
    candidates: tuple[SupplementCandidate, ...]
    status: SupplementStatus
    selected_ids: tuple[str, ...]
    created_at: str
    expires_at: str
    consumed_at: str = ""

    def __post_init__(self) -> None:
        _non_empty(self.id, "请求 ID")
        _non_empty(self.session_id, "会话 ID")
        _non_empty(self.question_digest, "问题摘要")
        if not isinstance(self.scope, Scope):
            raise ValueError("scope 必须是 Scope")
        if self.status not in _STATUSES:
            raise ValueError("补充请求状态非法")
        if not isinstance(self.candidates, tuple) or not self.candidates:
            raise ValueError("候选不能为空")
        if len(self.candidates) > _MAX_CANDIDATES:
            raise ValueError("候选最多 5 份")
        if not all(isinstance(candidate, SupplementCandidate) for candidate in self.candidates):
            raise ValueError("候选类型非法")
        candidate_ids = tuple(candidate.id for candidate in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("候选 ID 必须唯一")
        if not isinstance(self.selected_ids, tuple):
            raise ValueError("选择项必须是元组")
        if len(self.selected_ids) > _MAX_CANDIDATES:
            raise ValueError("最多 5 份")
        if len(self.selected_ids) != len(set(self.selected_ids)):
            raise ValueError("选择项不能重复")
        if any(candidate_id not in candidate_ids for candidate_id in self.selected_ids):
            raise ValueError("选择项不属于候选")
        self._validate_times()

    def _validate_times(self) -> None:
        created = _as_datetime(_to_iso(self.created_at))
        expires = _as_datetime(_to_iso(self.expires_at))
        if expires <= created:
            raise ValueError("过期时间必须晚于创建时间")
        if self.consumed_at:
            _to_iso(self.consumed_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "question_digest": self.question_digest,
            "scope": self.scope.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "status": self.status,
            "selected_ids": list(self.selected_ids),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "consumed_at": self.consumed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SupplementRequest":
        if not isinstance(data, Mapping):
            raise ValueError("补充请求必须是 JSON 对象")
        candidates = data.get("candidates")
        selected_ids = data.get("selected_ids", [])
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            raise ValueError("候选必须是 JSON 数组")
        if not isinstance(selected_ids, Sequence) or isinstance(selected_ids, (str, bytes)):
            raise ValueError("选择项必须是 JSON 数组")
        scope = data.get("scope")
        if not isinstance(scope, Mapping):
            raise ValueError("scope 必须是 JSON 对象")
        return cls(
            id=_non_empty(data.get("id"), "请求 ID"),
            session_id=_non_empty(data.get("session_id"), "会话 ID"),
            question_digest=_non_empty(data.get("question_digest"), "问题摘要"),
            scope=Scope.from_dict(scope),
            candidates=tuple(SupplementCandidate.from_dict(item) for item in candidates),
            status=_non_empty(data.get("status"), "状态"),  # type: ignore[arg-type]
            selected_ids=tuple(_non_empty(item, "选择项") for item in selected_ids),
            created_at=_to_iso(data.get("created_at")),
            expires_at=_to_iso(data.get("expires_at")),
            consumed_at=_to_iso(data.get("consumed_at", "")) if data.get("consumed_at") else "",
        )


class SupplementRegistry:
    """进程内补充请求表；后续由会话存储层持久化。"""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime | str] | None = None,
        id_factory: Callable[[], str] | None = None,
        ttl_seconds: int = 900,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("授权有效期必须大于 0")
        self._clock = clock or datetime.now
        self._id_factory = id_factory or (lambda: token_urlsafe(18))
        self._ttl_seconds = ttl_seconds
        self._requests: dict[str, SupplementRequest] = {}

    def _now(self) -> str:
        return _to_iso(self._clock())

    @staticmethod
    def _digest(question: str) -> str:
        return sha256(_non_empty(question, "问题").encode("utf-8")).hexdigest()

    def create(
        self,
        session_id: str,
        question: str,
        scope: Scope,
        candidates: Sequence[SupplementCandidate],
    ) -> SupplementRequest:
        now = self._now()
        expires_at = (_as_datetime(now) + timedelta(seconds=self._ttl_seconds)).isoformat(timespec="seconds")
        request_id = _non_empty(self._id_factory(), "请求 ID")
        if request_id in self._requests:
            raise ValueError("请求 ID 已存在")
        request = SupplementRequest(
            id=request_id,
            session_id=_non_empty(session_id, "会话 ID"),
            question_digest=self._digest(question),
            scope=scope,
            candidates=tuple(candidates),
            status="proposed",
            selected_ids=(),
            created_at=now,
            expires_at=expires_at,
        )
        self._requests[request.id] = request
        return request

    def get(self, request_id: str) -> SupplementRequest | None:
        return self._requests.get(request_id)

    def _owned_proposed(self, request_id: str, session_id: str) -> SupplementRequest:
        request = self.get(request_id)
        if request is None:
            raise KeyError("补充请求不存在")
        if request.session_id != _non_empty(session_id, "会话 ID"):
            raise PermissionError("补充请求不属于当前会话")
        if request.consumed_at:
            raise ValueError("补充请求已消费")
        if _as_datetime(self._now()) >= _as_datetime(request.expires_at):
            expired = replace(request, status="expired")
            self._requests[request.id] = expired
            raise ValueError("补充请求已过期")
        if request.status == "expired":
            raise ValueError("补充请求已过期")
        if request.status != "proposed":
            raise ValueError("补充请求不是待授权状态")
        return request

    def approve(
        self,
        request_id: str,
        session_id: str,
        candidate_ids: Sequence[str],
    ) -> SupplementRequest:
        request = self._owned_proposed(request_id, session_id)
        if not isinstance(candidate_ids, Sequence) or isinstance(candidate_ids, (str, bytes)):
            raise ValueError("选择项必须是数组")
        selected_ids = tuple(_non_empty(candidate_id, "选择项") for candidate_id in candidate_ids)
        if not selected_ids:
            raise ValueError("至少选择 1 份")
        if len(selected_ids) > _MAX_CANDIDATES:
            raise ValueError("最多 5 份")
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("选择项不能重复")
        allowed = {candidate.id for candidate in request.candidates}
        if any(candidate_id not in allowed for candidate_id in selected_ids):
            raise ValueError("选择项不属于候选")
        approved = replace(
            request,
            status="approved",
            selected_ids=selected_ids,
            consumed_at=self._now(),
        )
        self._requests[request.id] = approved
        return approved

    def decline(self, request_id: str, session_id: str) -> SupplementRequest:
        request = self._owned_proposed(request_id, session_id)
        declined = replace(request, status="declined")
        self._requests[request.id] = declined
        return declined

    def mark_consumed(self, request_id: str) -> SupplementRequest:
        request = self.get(request_id)
        if request is None:
            raise KeyError("补充请求不存在")
        if request.consumed_at:
            return request
        consumed = replace(request, consumed_at=self._now())
        self._requests[request.id] = consumed
        return consumed

    def transition(self, request_id: str, status: SupplementStatus) -> SupplementRequest:
        request = self.get(request_id)
        if request is None:
            raise KeyError("补充请求不存在")
        if status not in _STATUSES:
            raise ValueError("补充请求状态非法")
        if status in ("approved", "declined"):
            raise ValueError("approved/declined 必须通过 approve()/decline() 授权")
        if status not in _TRANSITIONS[request.status]:
            raise ValueError("补充请求状态转换非法")
        transitioned = replace(request, status=status)
        self._requests[request.id] = transitioned
        return transitioned


@dataclass(frozen=True)
class ReportNeed:
    """模型提出的披露需求；只保留期间和类型，不接受 URL、代码或文件路径。"""

    period: str
    report_type: str

    def __post_init__(self) -> None:
        _non_empty(self.period, "报告期")
        if self.report_type not in {item.value for item in ReportType}:
            raise ValueError("报告类型非法")
        try:
            date.fromisoformat(self.period)
        except ValueError as exc:
            raise ValueError("报告期必须是 ISO 日期") from exc

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | "ReportNeed") -> "ReportNeed":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("报告需求必须是 JSON 对象")
        return cls(
            period=_non_empty(value.get("period"), "报告期"),
            report_type=_non_empty(value.get("report_type"), "报告类型"),
        )


class SupplementCandidateResolver:
    """将模型的受控披露需求解析为当前单公司范围内的下载候选。"""

    def __init__(self, *, id_factory: Callable[[int], str] | None = None) -> None:
        self._id_factory = id_factory or (lambda index: f"candidate-{token_urlsafe(12)}-{index}")

    @staticmethod
    def _report_id(report: ReportMeta) -> str:
        return build_report_id(report.company_id, report.period, report.report_type)

    @staticmethod
    def _rank(report: ReportMeta, needs: Sequence[ReportNeed]) -> tuple[int, int, int, str]:
        exact = any(
            need.period == report.period.isoformat() and need.report_type == report.report_type.value
            for need in needs
        )
        if exact:
            tier = 0
        elif report.report_type in (ReportType.ANNUAL, ReportType.SEMI_ANNUAL):
            tier = 1
        else:
            tier = 2
        # 披露日是同优先级候选的真实来源排序；历史元数据缺失时排在末尾，
        # 再用报告期保证结果稳定，不从期间伪造披露日。
        disclosure = report.disclosure_date or date.min
        return tier, -disclosure.toordinal(), -report.period.toordinal(), report.report_type.value

    def resolve(
        self,
        scope: Scope,
        needs: Sequence[Mapping[str, Any] | ReportNeed],
        reports: Sequence[ReportMeta],
        local_pdf_exists: Callable[[ReportMeta], bool],
        indexed_report_ids: Callable[[], Sequence[str] | set[str]],
    ) -> list[SupplementCandidate]:
        if scope.mode != "company_only":
            raise ValueError("仅 company_only 范围允许补充财报")
        company = scope.companies[0]
        normalized_needs = tuple(ReportNeed.from_value(need) for need in needs[:_MAX_CANDIDATES])
        indexed = set(indexed_report_ids())
        accepted: list[ReportMeta] = []
        seen_report_ids: set[str] = set()
        for report in reports:
            if not isinstance(report, ReportMeta) or report.company_id != company.code:
                continue
            report_id = self._report_id(report)
            if report_id in seen_report_ids or report_id in indexed:
                continue
            seen_report_ids.add(report_id)
            if not report.download_url or local_pdf_exists(report):
                continue
            accepted.append(report)

        candidates: list[SupplementCandidate] = []
        for index, report in enumerate(sorted(accepted, key=lambda item: self._rank(item, normalized_needs))[:_MAX_CANDIDATES], start=1):
            candidates.append(SupplementCandidate(
                id=_non_empty(self._id_factory(index), "候选 ID"),
                report_id=self._report_id(report),
                code=company.code,
                company=company.name,
                period=report.period.isoformat(),
                report_type=report.report_type.value,
                source="巨潮资讯",
            ))
        return candidates


@dataclass(frozen=True)
class SupplementOutcome:
    """下载和摄取的最小安全摘要，不保留 URL、路径或原始异常。"""

    downloaded_report_ids: tuple[str, ...] = ()
    ingested_report_ids: tuple[str, ...] = ()
    skipped_report_ids: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    # (候选 ID, 受控原因类别)，供调用方说明失败而不泄露 URL、路径或异常文本。
    failure_reasons: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, list[Any]]:
        return {
            "downloaded_report_ids": list(self.downloaded_report_ids),
            "ingested_report_ids": list(self.ingested_report_ids),
            "skipped_report_ids": list(self.skipped_report_ids),
            "failed": list(self.failed),
            "failure_reasons": [
                {"candidate_id": candidate_id, "reason": reason}
                for candidate_id, reason in self.failure_reasons
            ],
        }


class SupplementExecutor:
    """仅在已授权请求上顺序下载并摄取；单项失败不影响其他候选。"""

    def __init__(
        self,
        downloader: ReportDownloader,
        ingestion_service: Any,
        storage_dir: str,
        reports_by_candidate: Mapping[str, ReportMeta],
    ) -> None:
        self._downloader = downloader
        self._ingestion_service = ingestion_service
        self._storage_dir = _non_empty(storage_dir, "报告目录")
        self._reports_by_candidate = dict(reports_by_candidate)

    @staticmethod
    def _valid_pdf(path: str) -> bool:
        try:
            with open(path, "rb") as file:
                return file.read(5) == b"%PDF-"
        except OSError:
            return False

    def run(self, request: SupplementRequest) -> SupplementOutcome:
        if not isinstance(request, SupplementRequest) or request.status != "approved" or not request.consumed_at:
            raise PermissionError("仅已批准且已消费的补充请求可以下载")
        if request.scope.mode != "company_only":
            raise PermissionError("仅 company_only 范围允许下载")

        company_code = request.scope.companies[0].code
        downloaded: list[str] = []
        ingested: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        failure_reasons: list[tuple[str, str]] = []
        candidate_by_id = {candidate.id: candidate for candidate in request.candidates}

        def _failed(candidate_id: str, reason: str) -> None:
            failed.append(candidate_id)
            failure_reasons.append((candidate_id, reason))

        for candidate_id in request.selected_ids:
            candidate = candidate_by_id[candidate_id]
            report = self._reports_by_candidate.get(candidate_id)
            if report is None or candidate.code != company_code or report.company_id != company_code:
                _failed(candidate_id, "unavailable_metadata")
                continue
            report_id = build_report_id(report.company_id, report.period, report.report_type)
            if report_id != candidate.report_id or not report.download_url:
                _failed(candidate_id, "unavailable_metadata")
                continue
            try:
                status = self._downloader.download_one(report, self._storage_dir)
            except Exception:
                _failed(candidate_id, "download_failed")
                continue
            if status != DownloadStatus.SUCCESS and status != DownloadStatus.SKIPPED:
                _failed(candidate_id, "download_failed")
                continue

            path = os.path.join(self._storage_dir, build_report_filename(report))
            if not self._valid_pdf(path):
                _failed(candidate_id, "invalid_pdf")
                continue
            if status == DownloadStatus.SUCCESS:
                downloaded.append(report_id)
            else:
                skipped.append(report_id)
            try:
                ingested_ok = self._ingestion_service.auto_ingest_pdf(path)
            except Exception:
                ingested_ok = False
            if ingested_ok is not True:
                _failed(candidate_id, "ingest_failed")
                continue
            ingested.append(report_id)

        return SupplementOutcome(
            downloaded_report_ids=tuple(downloaded),
            ingested_report_ids=tuple(ingested),
            skipped_report_ids=tuple(skipped),
            failed=tuple(failed),
            failure_reasons=tuple(failure_reasons),
        )
