"""Deterministic, fail-closed verification of answer claims."""
from __future__ import annotations

import re
from typing import Sequence

from webapp.chat_models import EvidenceArtifact, Fact, FactConflict, Scope, VerificationIssue, VerificationReport

_NUMBER_RE = re.compile(
    r"(?<![\d.])(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(亿元|万元|元/股|百分比|倍|股|元)"
)
_MONEY_SCALES = {"元": 1e-8, "万元": 1e-4, "亿元": 1.0}

# blocked 回答的最小降级文案：只说「无法核验该数值」，不整段重写回答。
SAFE_UNSUPPORTED_CLAIM_TEXT = "未找到可核验的披露，不能确认该数值。"

# 论断边界：以句末标点或换行切分。
_SENTENCE_END_RE = re.compile(r"[。！？!?；;\n]")
# 分句边界：指标词只在数值所在分句内判定；千分位逗号不能切分金额（否则指标词
# 会与数值分家）。
_CLAUSE_SEP_RE = re.compile(r"(?<!\d)[，,、：:]|(?<=\d)[，,、：:](?!\d)")

# Controlled aliases make numeric support metric-specific without requiring the
# model to use an internal metric key in Chinese prose.
_METRIC_ALIASES = {
    "revenue": ("revenue", "营业收入", "营收"),
    "net_profit": ("net_profit", "净利润", "归母净利润"),
    "operating_cash_flow": ("operating_cash_flow", "经营活动现金流", "经营现金流"),
    "price": ("price", "价格", "股价"),
}


def _metric_terms(metric: str) -> tuple[str, ...]:
    normalized = metric.strip().casefold()
    for aliases in _METRIC_ALIASES.values():
        if normalized in {alias.casefold() for alias in aliases}:
            return aliases
    return (metric,)


def _mentions_metric(metric: str, claim_text: str) -> bool:
    text = (claim_text or "").casefold()
    return any(term.casefold() in text for term in _metric_terms(metric) if term)


def _spans(text: str, separators: "re.Pattern[str]") -> tuple[tuple[int, int], ...]:
    """按分隔符切分文本，返回 (start, end) 区间并保留标点原文。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in separators.finditer(text or ""):
        spans.append((start, match.end()))
        start = match.end()
    if start < len(text or ""):
        spans.append((start, len(text)))
    return tuple(spans)


def _iter_claims(text: str):
    """产生 (数值区间, 该数值所在分句文本)。

    指标词只在数值所在的分句内判定：同值同单位、同一篇回答里出现多个指标时，
    另一处指标词不得让该数值被误判为受支持。
    """
    for sentence_start, sentence_end in _spans(text, _SENTENCE_END_RE):
        sentence = text[sentence_start:sentence_end]
        for clause_start, clause_end in _spans(sentence, _CLAUSE_SEP_RE):
            clause = sentence[clause_start:clause_end]
            for match in _NUMBER_RE.finditer(clause):
                span = (sentence_start + clause_start + match.start(),
                        sentence_start + clause_start + match.end())
                yield span, match.group(1), match.group(2), clause


def _claim_spans(text: str) -> tuple[tuple[int, int], ...]:
    """回答中全部数值论断区间。"""
    return tuple(span for span, _number, _unit, _clause in _iter_claims(text))


def _scoped_facts(facts: Sequence[Fact], scope: Scope) -> tuple[Fact, ...]:
    """只允许本范围内的事实支持论断；越界事实不能为数值背书。"""
    if scope.mode == "whole_corpus":
        return tuple(facts)
    scoped_codes = {company.code for company in scope.companies}
    return tuple(fact for fact in facts if fact.company_code in scoped_codes)


def _claim_matches(number: str, unit: str, clause: str, facts: Sequence[Fact]) -> list[Fact]:
    value = float(number.replace(",", "")) * _MONEY_SCALES.get(unit, 1.0)
    normalized_unit = "亿元" if unit in _MONEY_SCALES else unit
    return [
        fact for fact in facts
        if fact.unit == normalized_unit and abs(fact.value - value) <= 1e-6
        and _mentions_metric(fact.metric, clause)
    ]


def _unsupported_claims(
    text: str, facts: Sequence[Fact], scope: Scope,
) -> tuple[tuple[int, int], ...]:
    """返回文本中没有任何本范围事实支持的数值论断区间。"""
    scoped = _scoped_facts(facts, scope)
    return tuple(
        span for span, number, unit, clause in _iter_claims(text)
        if not _claim_matches(number, unit, clause, scoped)
    )


class ClaimVerifier:
    def verify(self, answer: str, scope: Scope, facts: Sequence[Fact], artifacts: Sequence[EvidenceArtifact], conflicts: Sequence[FactConflict]) -> VerificationReport:
        issues: list[VerificationIssue] = []
        supported: list[str] = []
        scoped_codes = {company.code for company in scope.companies}
        for fact in facts:
            if scope.mode != "whole_corpus" and fact.company_code not in scoped_codes:
                issues.append(VerificationIssue("scope_violation", "blocked", "事实超出本次问答范围。", fact.evidence_ids))
        for artifact in artifacts:
            if artifact.source == "pdf":
                invalid_url = artifact.pdf_url and not artifact.pdf_url.startswith("/api/history-pdf/")
                if artifact.page is None or artifact.page <= 0 or invalid_url:
                    issues.append(VerificationIssue("invalid_pdf_evidence", "blocked", "PDF 页码或链接不可核验。", artifact_ids=(artifact.report_id,)))
        scoped = _scoped_facts(facts, scope)
        for _span, number, unit, clause in _iter_claims(answer or ""):
            matches = _claim_matches(number, unit, clause, scoped)
            if not matches:
                issues.append(VerificationIssue("unsupported_numeric_claim", "blocked", "回答中的数值没有可核验证据。"))
                continue
            for fact in matches:
                supported.extend(fact.evidence_ids)
                if fact.verification == "reference" and "外部参考" not in answer:
                    issues.append(VerificationIssue("external_reference_ambiguity", "partial", "外部数据必须标示为外部参考。", fact.evidence_ids))
                if fact.verification == "reference" and "已确认" in answer:
                    issues.append(VerificationIssue("reference_marked_verified", "partial", "外部参考不能表述为已确认事实。", fact.evidence_ids))
        for conflict in conflicts:
            if _mentions_metric(conflict.metric, answer) and "存在口径/时间差异" not in answer:
                ids = tuple(evidence_id for fact in conflict.facts for evidence_id in fact.evidence_ids)
                issues.append(VerificationIssue("undisclosed_conflict", "partial", "相关事实存在口径/时间差异，回答必须披露。", ids))
        if any(issue.severity == "blocked" for issue in issues):
            status = "blocked"
        elif issues:
            status = "partial"
        else:
            status = "passed"
        return VerificationReport(status=status, issues=tuple(issues), supported_fact_ids=tuple(dict.fromkeys(supported)))

    def degrade_blocked(self, answer: str, scope: Scope, facts: Sequence[Fact]) -> str:
        """blocked 回答的确定性降级：只替换不受支持的数值论断。

        受支持的论断、公司身份和上下文原样保留；当整篇回答没有任何可核验论断时，
        直接用固定安全说明替代，避免输出不可核验的数字。
        """
        text = answer or ""
        unsupported = _unsupported_claims(text, facts, scope)
        if not unsupported:
            return text
        if len(unsupported) == len(_claim_spans(text)):
            return SAFE_UNSUPPORTED_CLAIM_TEXT
        parts: list[str] = []
        cursor = 0
        for start, end in unsupported:
            parts.append(text[cursor:start])
            parts.append(SAFE_UNSUPPORTED_CLAIM_TEXT)
            cursor = end
        parts.append(text[cursor:])
        return "".join(parts)
