"""Projection of run-scoped source results into persisted answer evidence and status."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping, Sequence

from webapp.chat_evidence import EvidenceNormalizer
from webapp.chat_facts import facts_from_market_result
from webapp.chat_models import EvidenceArtifact, Fact, Scope, ToolArtifact
from webapp.chat_time import MarketWindow
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
                         source_mode: str, access: SourceAccess, control: object | None = None,
                         daily_recap: bool = False) -> SourceRuntime:
    configured = getattr(cfg, "mcp_max_tool_calls", 1)
    if isinstance(configured, bool) or not isinstance(configured, int):
        configured = 1
    policy_limit = getattr(policy, "max_calls", configured)
    if isinstance(policy_limit, bool) or not isinstance(policy_limit, int):
        policy_limit = configured
    if source_mode == "market_recap" and daily_recap:
        recap_configured = getattr(cfg, "market_recap_max_tool_calls", 15)
        if isinstance(recap_configured, bool) or not isinstance(recap_configured, int):
            recap_configured = 1
        total = min(max(0, recap_configured), 15)
        market, web = total, min(total, 2)
    elif source_mode == "market_recap":
        total = min(max(0, configured), 9)
        market, web = min(total, 7), min(total, 2)
    else:
        total = min(max(0, configured), max(0, policy_limit))
        market, web = total, total
    def authorize(call):
        if call.provider in {"mcp", "akshare"} and not use_mcp:
            return False
        return access.permits(call)
    return SourceRuntime(scope, CallBudget(total, market, web), authorize, control=control)


def execute_source(runtime: SourceRuntime, call: SourceCall, invoke, *, fetched_at: str) -> SourceResult:
    return runtime.call(call, lambda: normalize_source(call, invoke(), fetched_at=fetched_at))


def market_overview_window_compatible(window: MarketWindow | None) -> bool:
    """MCP breadth snapshots are time-matched only for today or the supported 5TD window."""
    if window is None:
        return False
    return window.label == "今日" or (
        window.kind == "rolling_trading_days" and window.trading_days == 5
    )


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


def _source_rows(source: SourceResult) -> tuple[Mapping[str, object], ...]:
    payload = source.payload
    if isinstance(payload, Mapping):
        rows = payload.get("data") or payload.get("bars") or payload.get("rows")
        if isinstance(rows, (list, tuple)):
            return tuple(row for row in rows if isinstance(row, Mapping))
    if isinstance(payload, (list, tuple)):
        return tuple(row for row in payload if isinstance(row, Mapping))
    return ()


def _source_date(source: SourceResult, row: Mapping[str, object] | None = None) -> date | None:
    raw = source.as_of or ""
    if row is not None:
        raw = str(row.get("date") or row.get("trade_date") or row.get("as_of") or row.get("time") or raw)
    try:
        return date.fromisoformat(raw[:10])
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            return None


def _date_in_window(day: date | str | None, window: MarketWindow | None) -> bool:
    if isinstance(day, str):
        parts = day.split("至", 1)
        try:
            dates = tuple(date.fromisoformat(part[:10]) for part in parts)
        except ValueError:
            return False
        return bool(dates) and all(_date_in_window(value, window) for value in dates)
    if day is None:
        return False
    if window is None:
        return True
    if window.end_date is not None and day > window.end_date:
        return False
    if window.start_date is not None and day < window.start_date:
        return False
    return True


def _row_symbol(row: Mapping[str, object]) -> str:
    value = row.get("symbol") or row.get("code") or row.get("stock_code") or row.get("ticker")
    return str(value or "").strip().lower()


def _company_symbol_matches(row: Mapping[str, object], company_code: str) -> bool:
    expected = company_code.strip().lower()
    return any(
        candidate == expected or candidate.removeprefix("sh").removeprefix("sz").removeprefix("bj") == expected
        for candidate in (_row_symbol(row), str(row.get("code") or "").strip().lower())
    )


def _source_matches_company(source: SourceResult, company_code: str, window: MarketWindow | None) -> bool:
    rows = [row for row in _source_rows(source) if _company_symbol_matches(row, company_code)]
    dated_rows = [row for row in rows if _source_date(source, row) is not None]
    return bool(
        source.category == "market" and source.status in {"success", "partial"} and rows
        and (any(_date_in_window(_source_date(source, row), window) for row in dated_rows)
             if dated_rows else _date_in_window(_source_date(source), window))
    )


def _source_window_matches(source: SourceResult, window: MarketWindow) -> bool:
    actual = source.coverage.data_window
    if window.kind == "rolling_trading_days" and window.end_date is not None and actual:
        try:
            actual_end = date.fromisoformat(actual.split("至")[-1][:10])
        except ValueError:
            return False
        return actual_end == window.end_date
    return _date_in_window(actual, window)


def _matching_index_sources(
    sources: Sequence[SourceResult], window: MarketWindow,
) -> dict[str, SourceResult]:
    expected = {"sh000001", "sz399001", "sz399006", "sh000688"}
    matches: dict[str, SourceResult] = {}
    for source in sources:
        if (source.category != "market" or source.operation not in {"kline", "a_share_index_kline"}
                or source.status not in {"success", "partial"}):
            continue
        for row in _source_rows(source):
            symbol = _row_symbol(row)
            if symbol in expected and _date_in_window(_source_date(source, row), window):
                existing = matches.get(symbol)
                if existing is None or (existing.status != "success" and source.status == "success"):
                    matches[symbol] = source
    return matches


def market_task_status(
    intent: str, scope: Scope, window: MarketWindow | None,
    sources: Sequence[SourceResult], verification: str | None,
) -> str:
    """Require dated, request-matching market evidence before reporting task completion."""
    evidence_status = "failed"
    if intent == "market_quote":
        if not scope.companies:
            return "failed"
        matching = [source for source in sources
                    if source.operation in {"quote", "get_quote", "get_realtime_quote", "get_realtime_data"}
                    and _source_matches_company(source, scope.companies[0].code, window)]
        if matching:
            evidence_status = "completed" if any(item.status == "success" for item in matching) else "partial"
    elif intent == "market_trend":
        if not scope.companies:
            return "failed"
        code = scope.companies[0].code
        matching = [source for source in sources
                    if source.operation == "kline" and _source_matches_company(source, code, window)]
        if matching:
            source = max(matching, key=lambda item: len(_source_rows(item)))
            rows = tuple(row for row in _source_rows(source)
                         if _company_symbol_matches(row, code) and _date_in_window(_source_date(source, row), window))
            dated = {_source_date(source, row) for row in rows}
            dated.discard(None)
            if window and window.kind == "rolling_trading_days":
                evidence_status = (
                    "completed" if len(dated) >= (window.trading_days or 5) and source.status == "success"
                    else "partial"
                )
            else:
                evidence_status = "completed" if source.status == "success" and dated else "partial"
    elif intent == "market_recap":
        if window is None:
            return "failed"
        indices = _matching_index_sources(sources, window)
        if indices:
            evidence_status = "partial"
            if window.label == "今日":
                snapshots = [source for source in sources if source.provider == "akshare"
                             and source.operation == "a_share_market_snapshot"
                             and source.status in {"success", "partial"}
                             and _source_window_matches(source, window)]
                overview_complete = len(indices) == 4 and any(
                    source.status == "success" for source in snapshots
                )
            else:
                overview = [source for source in sources if source.category == "market"
                            and source.operation in {"stock_zt_pool", "stock_sector_fund_flow_rank"}]
                pools = [source for source in overview if source.operation == "stock_zt_pool"]
                flows = [source for source in overview if source.operation == "stock_sector_fund_flow_rank"]
                overview_complete = (
                    len(indices) == 4 and len(pools) >= 2 and len(flows) >= 1
                    and all(source.status == "success" and _source_window_matches(source, window)
                            for source in pools + flows)
                    and (window.kind != "rolling_trading_days" or any(
                        source.coverage.query_window == f"{window.trading_days}日" for source in flows
                    ))
                )
            if overview_complete and all(source.status == "success" for source in indices.values()):
                evidence_status = "completed"
    if evidence_status == "failed":
        return "failed"
    return evidence_status if verification == "passed" else "partial"


def _source_result_summary(result: SourceResult) -> str:
    if result.provider != "akshare" or result.operation != "a_share_market_snapshot":
        return ""
    rows = result.payload if isinstance(result.payload, tuple) else ()
    if len(rows) != 1 or not isinstance(rows[0], Mapping):
        return result.error_code[:120]
    row = rows[0]

    def count(name: str) -> str:
        value = row.get(name)
        return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else "不可用"

    parts = [
        f"快照证券 {count('unique_security_rows')}/{count('universe_rows')} 行",
        f"涨跌分类 {count('classified_rows')}/{count('unique_security_rows')} 行",
        f"量比样本 {count('volume_ratio_sample_count')}/{count('volume_ratio_active_rows')} 只",
        f"来源涨停池 {count('limit_up_count')} 家、跌停池 {count('limit_down_count')} 家",
    ]
    limitation = row.get("limitations")
    if isinstance(limitation, str) and limitation.strip():
        parts.append("限制：" + limitation.strip())
    return "；".join(parts)[:400]


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
        provider = result.provider if result.provider in {"mcp", "tencent", "akshare", "baostock", "web", "local"} else "other"
        coverage_summary = result.coverage.summary()
        artifact_summary = (_source_result_summary(result) or
                            (result.content[:400] if status in {"success", "partial"} else result.error_code[:120]))
        if coverage_summary:
            artifact_summary = ("覆盖：" + coverage_summary + ("；" + artifact_summary if artifact_summary else ""))[:500]
        artifact = ToolArtifact(
            provider=provider,
            tool_name=result.operation[:120] or "source",
            as_of=result.as_of if status in {"success", "partial"} else "",
            status=status,
            arguments_summary=(result.arguments_summary if result.provider == "web" and result.operation == "web_search" else ""),
            result_summary=artifact_summary,
            fetched_at=result.fetched_at,
            source_id=result.call_id,
        )
        tools.append(artifact)
        if result.provider in {"tencent", "akshare", "baostock"}:
            market_facts = facts_from_market_result(result, artifact)
            if result.provider == "tencent" and scope.mode != "whole_corpus":
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
