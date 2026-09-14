"""公司/行业 Scope 解析：把问题与聚焦报告解析为不可变的范围合同。

本模块是范围解析的唯一入口，遵守以下安全边界：

- ``focus_report`` 优先于问题文本，``company_only`` 时报告边界就是该报告身份本身，
  即使它尚未本地索引也不扩大到同公司其他期次（fail-closed）。
- 没有 ``focus_report`` 时，只能从问题文本中显式出现且本地已索引的 6 位股票代码
  识别公司；不得从模糊文本猜测公司，也不使用自然语言名称推断。
- ``auto`` 仅在问题包含行业、同业、竞争、排名、对比等词时尝试 ``company_industry``；
  用户显式 mode 覆盖 ``auto``。
- ``company_industry`` 的候选仅来自本地 RAG 已索引报告，按目标公司的数据源行业
  分类筛选；provider 无结果、候选只剩目标公司或行业名称为空时回退 ``company_only``。
- ``whole_corpus`` 只能由无公司上下文的显式选择或无法识别公司时产生。
- 行业分类结果按 code/industry_name/provider/resolved_at 缓存到
  ``data/company_industries.json``，超过有效期后重新查询 provider。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Mapping, Sequence

from financial_report_fetcher.report_identity import build_report_id

from .chat_models import CompanyRef, IndustryRef, Scope, SourcePolicy

RequestMode = Literal["auto", "company_only", "company_industry", "whole_corpus"]

_REQUEST_MODES = frozenset(("auto", "company_only", "company_industry", "whole_corpus"))

# auto 模式下触发同业扩展的关键词（与设计文档已确认口径一致）
_INDUSTRY_KEYWORDS = ("行业", "同业", "竞争", "排名", "对比")

# 问题文本中的 6 位股票代码：前后不能是数字，避免把日期等数字串误判为代码
_TEXT_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")

DEFAULT_CACHE_PATH = "data/company_industries.json"
DEFAULT_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 行业分类最长缓存 7 天

_FALLBACK_INDUSTRY_UNAVAILABLE = "行业分类不可用，已保持本公司范围"
_FALLBACK_INDUSTRY_EMPTY = "行业分类名称为空，已保持本公司范围"
_FALLBACK_NO_PEERS = "本地未找到同行业可检索同业报告，已保持本公司范围"


def _report_code(report_id: str) -> str:
    """按 ``code:period:type`` 报告身份的前缀约定提取公司代码。"""
    return report_id.split(":", 1)[0]


@dataclass(frozen=True)
class ScopeRequest:
    """请求方声明的范围意图；``focus_report`` 为 ``{code, period}`` 跳转上下文。"""

    mode: RequestMode
    focus_report: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.mode not in _REQUEST_MODES:
            raise ValueError(f"scope request mode must be one of {sorted(_REQUEST_MODES)}")
        if self.focus_report is not None and not isinstance(self.focus_report, Mapping):
            raise ValueError("focus_report must be a JSON object or null")

    @classmethod
    def auto(cls, focus_report: Mapping[str, str] | None = None) -> "ScopeRequest":
        return cls("auto", focus_report)

    @classmethod
    def company_only(cls, focus_report: Mapping[str, str] | None = None) -> "ScopeRequest":
        return cls("company_only", focus_report)

    @classmethod
    def company_industry(cls, focus_report: Mapping[str, str] | None = None) -> "ScopeRequest":
        return cls("company_industry", focus_report)

    @classmethod
    def whole_corpus(cls) -> "ScopeRequest":
        return cls("whole_corpus", None)


class ScopeResolver:
    """把 ``ScopeRequest`` 解析为冻结的 ``Scope``；全部依赖通过构造注入。"""

    def __init__(
        self,
        report_ids_provider: Callable[[], Sequence[str]],
        company_name_provider: Callable[[str], str | None],
        industry_provider: Callable[[str], IndustryRef | None],
        *,
        cache_path: str | os.PathLike[str] | None = DEFAULT_CACHE_PATH,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._report_ids = report_ids_provider
        self._company_name = company_name_provider
        self._industry = industry_provider
        # cache_path=None 表示仅内存缓存、不落盘（测试/临时场景）
        self._cache_path = os.fspath(cache_path) if cache_path is not None else None
        self._cache_ttl = cache_ttl_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._cache = self._load_cache()

    # ── 入口 ──────────────────────────────────────────────────

    def resolve(self, question: str, request: ScopeRequest) -> Scope:
        if not isinstance(request, ScopeRequest):
            raise ValueError("request must be a ScopeRequest")

        if request.mode == "whole_corpus":
            return Scope.whole_corpus()

        report_ids = self._local_report_ids()
        code, focus_report_id = self._resolve_company(question, request.focus_report, report_ids)

        if code is None:
            return Scope.whole_corpus()

        name = self._company_name(code) or code
        mode = request.mode
        if mode == "auto":
            mode = "company_industry" if self._wants_industry(question) else "company_only"

        target_report_ids = self._target_report_ids(code, focus_report_id, report_ids)
        if mode == "company_industry":
            return self._resolve_industry_scope(code, name, target_report_ids, report_ids)
        return Scope.company_only(code, name, target_report_ids)

    # ── 公司解析 ──────────────────────────────────────────────

    def _local_report_ids(self) -> tuple[str, ...]:
        try:
            raw = self._report_ids()
        except Exception:
            return ()
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(sorted({str(item).strip() for item in raw if str(item).strip()}))

    @staticmethod
    def _local_codes(report_ids: Sequence[str]) -> frozenset[str]:
        return frozenset(_report_code(rid) for rid in report_ids)

    def _resolve_company(
        self,
        question: str,
        focus_report: Mapping[str, str] | None,
        report_ids: Sequence[str],
    ) -> tuple[str | None, str | None]:
        code, report_id = self._focus_parts(focus_report)
        if code is not None:
            return code, report_id
        return self._text_code(question, report_ids), None

    @staticmethod
    def _focus_parts(focus_report: Mapping[str, str] | None) -> tuple[str | None, str | None]:
        if not isinstance(focus_report, Mapping):
            return None, None
        code = str(focus_report.get("code") or "").strip()
        period = str(focus_report.get("period") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            return None, None
        report_id: str | None = None
        if period:
            try:
                report_id = build_report_id(code, period)
            except (ValueError, TypeError):
                report_id = None
        return code, report_id

    def _text_code(self, question: str, report_ids: Sequence[str]) -> str | None:
        unique = list(dict.fromkeys(_TEXT_CODE_RE.findall(question or "")))
        if len(unique) != 1:
            return None
        code = unique[0]
        if code not in self._local_codes(report_ids):
            return None
        return code

    @staticmethod
    def _wants_industry(question: str) -> bool:
        return any(keyword in question for keyword in _INDUSTRY_KEYWORDS)

    def _target_report_ids(
        self,
        code: str,
        focus_report_id: str | None,
        report_ids: Sequence[str],
    ) -> tuple[str, ...]:
        if focus_report_id:
            return (focus_report_id,)
        return tuple(sorted(rid for rid in report_ids if _report_code(rid) == code))

    # ── 行业扩展 ──────────────────────────────────────────────

    def _resolve_industry_scope(
        self,
        code: str,
        name: str,
        target_report_ids: tuple[str, ...],
        report_ids: Sequence[str],
    ) -> Scope:
        industry = self._resolve_industry(code)
        if industry is None:
            return self._company_only_with_reason(code, name, target_report_ids, _FALLBACK_INDUSTRY_UNAVAILABLE)
        if not industry.name.strip():
            return self._company_only_with_reason(code, name, target_report_ids, _FALLBACK_INDUSTRY_EMPTY)

        peer_codes = self._peer_codes(code, industry.name.strip(), report_ids)
        if not peer_codes:
            return self._company_only_with_reason(code, name, target_report_ids, _FALLBACK_NO_PEERS)

        peer_report_ids = tuple(sorted(rid for rid in report_ids if _report_code(rid) in peer_codes))
        combined = tuple(sorted(set(target_report_ids) | set(peer_report_ids)))
        return Scope(
            mode="company_industry",
            companies=(CompanyRef(code, name),),
            report_ids=combined,
            industry=industry,
            source_policy=SourcePolicy.local_only(),
        )

    def _peer_codes(
        self,
        target_code: str,
        industry_name: str,
        report_ids: Sequence[str],
    ) -> frozenset[str]:
        peers: set[str] = set()
        for rid in report_ids:
            peer_code = _report_code(rid)
            if peer_code == target_code:
                continue
            ref = self._resolve_industry(peer_code)
            if ref is not None and ref.name.strip() == industry_name:
                peers.add(peer_code)
        return frozenset(peers)

    @staticmethod
    def _company_only_with_reason(
        code: str,
        name: str,
        report_ids: Sequence[str],
        reason: str,
    ) -> Scope:
        return Scope(
            mode="company_only",
            companies=(CompanyRef(code, name),),
            report_ids=tuple(report_ids),
            source_policy=SourcePolicy.local_only(),
            fallback_reason=reason,
        )

    # ── 行业分类缓存 ──────────────────────────────────────────

    def _resolve_industry(self, code: str) -> IndustryRef | None:
        cached = self._cache.get(code)
        if isinstance(cached, dict) and not self._expired(cached.get("resolved_at")):
            name = str(cached.get("industry_name", "")).strip()
            provider = str(cached.get("provider", "")).strip()
            if name and provider:
                return IndustryRef(
                    name=name,
                    provider=provider,
                    resolved_at=str(cached.get("resolved_at", "")),
                    sample_kind="local_indexed",
                )

        try:
            ref = self._industry(code)
        except Exception:
            ref = None
        if not isinstance(ref, IndustryRef) or not ref.name.strip():
            return None

        resolved_at = self._now().isoformat(timespec="seconds")
        self._cache[code] = {
            "industry_name": ref.name,
            "provider": ref.provider,
            "resolved_at": resolved_at,
        }
        self._save_cache()
        return IndustryRef(
            name=ref.name,
            provider=ref.provider,
            resolved_at=resolved_at,
            sample_kind="local_indexed",
        )

    def _expired(self, resolved_at: object) -> bool:
        if not isinstance(resolved_at, str) or not resolved_at:
            return True
        try:
            parsed = datetime.fromisoformat(resolved_at)
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (now - parsed).total_seconds() > self._cache_ttl

    def _load_cache(self) -> dict[str, dict[str, str]]:
        if self._cache_path is None:
            return {}
        try:
            with open(self._cache_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            key: value
            for key, value in data.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    def _save_cache(self) -> None:
        if self._cache_path is None:
            return
        try:
            directory = os.path.dirname(os.path.abspath(self._cache_path)) or "."
            os.makedirs(directory, exist_ok=True)
            tmp = self._cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._cache_path)
        except OSError:
            pass
