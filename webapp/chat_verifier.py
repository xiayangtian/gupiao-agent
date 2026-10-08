"""Deterministic, fail-closed verification of answer claims."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import re
from typing import Sequence

from webapp.chat_models import EvidenceArtifact, Fact, FactConflict, Scope, VerificationIssue, VerificationReport

_NUMBER_RE = re.compile(
    r"(?<![\d.])(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(元/股|亿元|万元|百分点|百分比|指数点|%|％|倍|股|点|家|元)"
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
    "revenue_growth": ("revenue_growth", "revenue_yoy_growth", "营业收入", "营收"),
    "net_profit_growth": ("net_profit_growth", "净利润", "归母净利润"),
    "gross_margin": ("gross_margin", "毛利率"),
    "roe": ("roe", "净资产收益率", "ROE"),
    "index_close": ("index_close", "指数", "上证指数", "深证成指", "创业板指"),
    "index_close_delta": ("index_close_delta", "指数", "指数点"),
    "limit_up_count": ("limit_up_count", "涨停", "涨停家数"),
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
    """只允许本范围内且派生链完整的事实支持论断。"""
    from webapp.chat_facts import derive_growth, derive_index_delta, derive_percentage_point_delta

    by_id = {fact.id: fact for fact in facts if fact.id}
    valid = []
    for fact in facts:
        if fact.derived_from_ids:
            parents = [by_id.get(identifier) for identifier in fact.derived_from_ids]
            if any(parent is None for parent in parents):
                continue
            resolved = [parent for parent in parents if parent is not None]
            evidence = {identifier for parent in resolved for identifier in parent.evidence_ids}
            parent_metrics = {parent.metric for parent in resolved}
            metric_is_delta = (
                fact.metric == "index_close_delta" and parent_metrics == {"index_close"}
                and "[index-point delta]" in fact.formula
            )
            if (fact.source_category == "unknown" or len(parent_metrics) != 1
                    or (fact.metric not in parent_metrics and not metric_is_delta)
                    or any(parent.source_category != fact.source_category
                           or parent.company_code != fact.company_code for parent in resolved)
                    or evidence != set(fact.evidence_ids)):
                continue
            current, baseline = resolved[0], resolved[1]
            if fact.unit == "百分点":
                expected = derive_percentage_point_delta(current, baseline)
            elif fact.metric == "index_close_delta":
                expected = derive_index_delta(current, baseline)
            elif fact.unit == "百分比":
                relation = "yoy" if fact.formula.endswith("[yoy]") else "qoq" if fact.formula.endswith("[qoq]") else ""
                expected = derive_growth(current, baseline, relation=relation)
            else:
                expected = None
            if expected is None or fact != expected:
                continue
        valid.append(fact)
    if scope.mode == "whole_corpus":
        return tuple(valid)
    scoped_codes = {company.code for company in scope.companies}
    return tuple(fact for fact in valid if fact.company_code in scoped_codes)


def _claim_matches(
    number: str, unit: str, clause: str, facts: Sequence[Fact], *,
    requested_period: str | None = None, window: object | None = None,
    answer_intent: str | None = None,
) -> list[Fact]:
    value = float(number.replace(",", "")) * _MONEY_SCALES.get(unit, 1.0)
    normalized_unit = "亿元" if unit in _MONEY_SCALES else ("百分比" if unit in {"%", "％"} else "点" if unit == "指数点" else unit)
    period_match = re.search(r"(20\d{2})年", clause)
    target_year = period_match.group(1) if period_match else None
    date_match = re.search(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)", clause)
    matches = [
        fact for fact in facts
        if fact.unit == normalized_unit and abs(fact.value - value) <= 1e-6
        and _mentions_metric(fact.metric, clause)
    ]
    if answer_intent in {"financial_trend", "financial_report", "report_fact"}:
        matches = [fact for fact in matches if fact.source_category == "local_pdf"]
    elif answer_intent == "market_trend":
        matches = [fact for fact in matches if fact.source_category == "market"]
    elif answer_intent == "market_quote":
        matches = [fact for fact in matches if fact.source_category in {"market", "mcp"}]
    elif answer_intent == "market_recap":
        matches = [fact for fact in matches if fact.source_category in {"market", "mcp"}]
    elif answer_intent == "general_web":
        matches = [fact for fact in matches if fact.source_category == "web"]
    if requested_period:
        date_range = re.fullmatch(r"(\d{4}-\d{2}-\d{2})至(\d{4}-\d{2}-\d{2})", requested_period)
        range_match = re.search(r"(20\d{2})\s*年?\s*(?:至|到|~|～|—|-)\s*(20\d{2})\s*年", requested_period)
        quarter_match = re.search(r"(20\d{2})\s*年?\s*(?:第\s*)?([1-4一二三四])\s*季度", requested_period)
        year = re.search(r"20\d{2}", requested_period)
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", requested_period):
            matches = [fact for fact in matches if fact.period == requested_period]
        elif date_range:
            start, end = date.fromisoformat(date_range.group(1)), date.fromisoformat(date_range.group(2))
            matches = [fact for fact in matches if _date_in_window(fact.period, start, end)]
        elif range_match:
            first, last = int(range_match.group(1)), int(range_match.group(2))
            matches = [fact for fact in matches if fact.period[:4].isdigit() and first <= int(fact.period[:4]) <= last]
        elif quarter_match:
            quarter = {"一": 1, "二": 2, "三": 3, "四": 4}.get(quarter_match.group(2), int(quarter_match.group(2)) if quarter_match.group(2).isdigit() else 0)
            matches = [fact for fact in matches if fact.period.startswith(quarter_match.group(1))
                       and fact.period_kind in {"single_quarter", "quarterly"}
                       and fact.period[5:7].isdigit() and (int(fact.period[5:7]) - 1) // 3 + 1 == quarter]
        elif year:
            matches = [fact for fact in matches if fact.period.startswith(year.group(0))]
        else:
            matches = []
    if date_match:
        matches = [fact for fact in matches if fact.period == date_match.group(1)]
    elif target_year:
        matches = [fact for fact in matches if fact.period.startswith(target_year)]
    if "归母" in clause or "母公司" in clause:
        matches = [fact for fact in matches if fact.entity_scope == "parent"]
    elif "合并" in clause:
        matches = [fact for fact in matches if fact.entity_scope == "consolidated"]
    elif len({fact.entity_scope for fact in matches}) > 1:
        matches = []
    if window is not None:
        start = getattr(window, "start_date", None)
        end = getattr(window, "end_date", None)
        if start is not None and end is not None:
            matches = [fact for fact in matches if _date_in_window(fact.period, start, end)]
        elif (start is None and end is not None
              and getattr(window, "kind", None) == "rolling_trading_days"):
            count = getattr(window, "trading_days", None) or 0
            def series_key(fact):
                return (fact.metric, fact.company_code, fact.entity_scope, fact.unit, fact.source_category)
            valid_periods: dict[tuple[str, str, str, str, str], set[str]] = defaultdict(set)
            for fact in facts:
                if (fact.period_kind == "daily" and _date_in_window(fact.period, date.min, end)):
                    valid_periods[series_key(fact)].add(fact.period)
            selected = {
                key: set(sorted(periods)[-count:]) if count > 0 else set()
                for key, periods in valid_periods.items()
            }
            matches = [fact for fact in matches if fact.period in selected.get(series_key(fact), set())]
        else:
            matches = []
    if len({fact.period for fact in matches}) > 1:
        return []
    return matches


def _date_in_window(period: str, start: object, end: object) -> bool:
    from datetime import date
    try:
        value = date.fromisoformat(period)
        return start <= value <= end
    except (TypeError, ValueError):
        return False


def _unsupported_claims(
    text: str, facts: Sequence[Fact], scope: Scope, *,
    requested_period: str | None = None, window: object | None = None,
    answer_intent: str | None = None,
) -> tuple[tuple[int, int], ...]:
    """返回文本中没有任何本范围事实支持的数值论断区间。"""
    scoped = _scoped_facts(facts, scope)
    return tuple(
        span for span, number, unit, clause in _iter_claims(text)
        if not _claim_matches(number, unit, clause, scoped, requested_period=requested_period,
                              window=window, answer_intent=answer_intent)
    )


class ClaimVerifier:
    def verify(self, answer: str, scope: Scope, facts: Sequence[Fact], artifacts: Sequence[EvidenceArtifact], conflicts: Sequence[FactConflict], *, requested_period: str | None = None, window: object | None = None, answer_intent: str | None = None) -> VerificationReport:
        issues: list[VerificationIssue] = []
        supported: list[str] = []
        scoped_codes = {company.code for company in scope.companies}
        for fact in facts:
            if scope.mode != "whole_corpus" and fact.company_code not in scoped_codes:
                issues.append(VerificationIssue("scope_violation", "blocked", "事实超出本次问答范围。", fact.evidence_ids))
        for artifact in artifacts:
            if artifact.source == "pdf":
                invalid_url = artifact.pdf_url and not artifact.pdf_url.startswith("/api/history-pdf/")
                if artifact.page is None or artifact.page <= 0 or invalid_url or artifact.availability != "available":
                    issues.append(VerificationIssue("invalid_pdf_evidence", "blocked", "PDF 页码或链接不可核验。", artifact_ids=(artifact.report_id,)))
        scoped = _scoped_facts(facts, scope)
        for _span, number, unit, clause in _iter_claims(answer or ""):
            matches = _claim_matches(number, unit, clause, scoped, requested_period=requested_period,
                                      window=window, answer_intent=answer_intent)
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

    def degrade_blocked(self, answer: str, scope: Scope, facts: Sequence[Fact], *, requested_period: str | None = None, window: object | None = None, answer_intent: str | None = None) -> str:
        """blocked 回答的确定性降级：只替换不受支持的数值论断。

        受支持的论断、公司身份和上下文原样保留；当整篇回答没有任何可核验论断时，
        直接用固定安全说明替代，避免输出不可核验的数字。
        """
        text = answer or ""
        unsupported = _unsupported_claims(text, facts, scope, requested_period=requested_period,
                                           window=window, answer_intent=answer_intent)
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
