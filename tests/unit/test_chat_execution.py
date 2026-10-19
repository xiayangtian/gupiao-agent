import pytest


def _status(**overrides):
    from webapp.chat_execution import resolve_answer_status
    values = dict(stopped=False, failed=False, waiting_consent=False, required_missing=False,
                  had_source_failure=False, retrieval_degraded=False, verification="passed")
    values.update(overrides)
    return resolve_answer_status(**values)


@pytest.mark.parametrize("verification", ["blocked", "partial"])
def test_failed_verification_never_marks_run_completed(verification):
    assert _status(verification=verification) == "partial"


def test_status_prioritizes_stopped_then_failure_then_consent():
    assert _status(stopped=True, failed=True, waiting_consent=True) == "stopped"
    assert _status(failed=True, waiting_consent=True) == "failed"
    assert _status(waiting_consent=True) == "waiting_consent"


def test_source_failure_and_missing_verification_are_partial():
    assert _status(had_source_failure=True) == "partial"
    assert _status(verification=None) == "partial"


def test_unknown_time_source_is_partial_and_keeps_acquisition_time():
    from webapp.chat_execution import project_sources
    from webapp.chat_models import Scope
    from webapp.source_runtime import SourceResult

    result = SourceResult("s1", "fixture", "quote", "market", "success",
                          content="bounded", fetched_at="2026-09-24T10:00:00+08:00")
    projection = project_sources([result], Scope.whole_corpus())
    assert len(projection.tool_artifacts) == 1
    artifact = projection.tool_artifacts[0]
    assert artifact.status == "partial"
    assert artifact.as_of == ""
    assert artifact.fetched_at == "2026-09-24T10:00:00+08:00"
    assert artifact.source_id == "s1"
    assert projection.facts == ()


def test_web_search_projection_preserves_safe_query_summary():
    import json

    from webapp.chat_execution import project_sources
    from webapp.chat_models import Scope
    from webapp.source_runtime import SourceResult

    result = SourceResult(
        "web-1", "web", "web_search", "web", "partial", content="bounded",
        arguments_summary='{"query":"长江电力 近五日趋势"}',
    )
    projection = project_sources([result], Scope.whole_corpus())

    assert json.loads(projection.tool_artifacts[0].arguments_summary) == {"query": "长江电力 近五日趋势"}


def test_deepseek_search_projection_keeps_provider_identity_and_web_only_evidence():
    from webapp.chat_execution import project_sources
    from webapp.chat_models import Scope
    from webapp.source_runtime import SourceResult

    source = SourceResult(
        "ds-1", "deepseek_search", "web_search", "web", "partial",
        payload=({"title": "来源", "url": "https://example.com", "content": "摘要"},),
        fetched_at="2026-10-19T12:00:00+08:00",
    )
    projection = project_sources([source], Scope.whole_corpus())

    assert projection.tool_artifacts[0].provider == "deepseek_search"
    assert projection.artifacts[0].url == "https://example.com"
    assert projection.facts == ()


def test_akshare_tool_artifact_exposes_row_coverage_and_specific_limitations():
    from webapp.chat_execution import project_sources
    from webapp.chat_models import Scope
    from webapp.source_runtime import SourceCoverage, SourceResult

    summary = {
        "status": "partial", "trade_date": "2026-10-15", "as_of": "2026-10-15T15:05:00+08:00",
        "universe_rows": 5500, "unique_security_rows": 5500, "classified_rows": 5500,
        "volume_ratio_sample_count": 5100, "volume_ratio_active_rows": 5200,
        "limit_up_count": 36, "limit_down_count": None,
        "limitations": "跌停池缺失或日期校验不通过",
    }
    result = SourceResult(
        "s1", "akshare", "a_share_market_snapshot", "market", "partial",
        as_of=summary["as_of"], payload=(summary,),
        coverage=SourceCoverage(returned_rows=1, data_window="2026-10-15"),
    )

    artifact = project_sources([result], Scope.whole_corpus()).tool_artifacts[0]

    assert "快照证券 5,500/5,500 行" in artifact.result_summary
    assert "量比样本 5,100/5,200 只" in artifact.result_summary
    assert "来源涨停池 36 家、跌停池 不可用 家" in artifact.result_summary
    assert "跌停池缺失" in artifact.result_summary


def test_local_retrieval_is_not_fabricated_as_external_tool_artifact():
    from webapp.chat_execution import project_sources
    from webapp.chat_models import Scope
    from webapp.source_runtime import SourceResult

    projection = project_sources([SourceResult("local-1", "local", "retrieve", "local", "success",
        retrieval_hits=({"source": "pdf", "snippet": "evidence"},))], Scope.whole_corpus())
    assert projection.tool_artifacts == ()
    assert projection.source_summary["local_pdf"] == "已使用"


def test_same_source_id_is_projected_once_but_distinct_sources_do_not_overwrite():
    from webapp.chat_execution import project_sources
    from webapp.chat_models import Scope
    from webapp.source_runtime import SourceResult

    results = [
        SourceResult("s1", "fixture", "quote", "market", "success", "one", "2026-09-24"),
        SourceResult("s1", "fixture", "quote", "market", "failed", error_code="timeout"),
        SourceResult("s2", "fixture", "fund_flow", "market", "failed", error_code="timeout"),
    ]
    projection = project_sources(results, Scope.whole_corpus())
    assert [item.source_id for item in projection.tool_artifacts] == ["s1", "s2"]
    assert projection.had_failure


def test_old_tool_artifact_fields_round_trip_and_partial_can_omit_as_of():
    from webapp.chat_models import ToolArtifact

    old = ToolArtifact.from_dict({"provider": "fixture", "tool_name": "quote",
                                  "status": "failed", "as_of": ""})
    assert old.fetched_at == old.source_id == ""
    assert ToolArtifact.from_dict(old.to_dict()) == old
    partial = ToolArtifact("fixture", "quote", status="partial", fetched_at="2026-09-24T10:00:00+08:00")
    assert partial.as_of == ""


def test_server_frozen_market_plan_uses_one_authorized_tencent_call_without_model_tools():
    from types import SimpleNamespace
    from webapp.chat_execution import build_source_runtime
    from webapp.chat_models import Scope
    from webapp.source_adapters import SourceAccess
    from webapp.source_runtime import SourceCall, SourceResult

    access = SourceAccess(mcp_enabled=False, listed_tools=frozenset(), whitelist=frozenset(),
                          mcp_allow=lambda: False, web_enabled=False, tencent_enabled=True)
    runtime = build_source_runtime(
        scope=Scope.company_only("600519", "贵州茅台"), policy=SimpleNamespace(max_calls=1),
        cfg=SimpleNamespace(mcp_max_tool_calls=3), use_mcp=False,
        source_mode="external_market", access=access,
    )
    result = runtime.call(SourceCall("tencent", "kline", "market", {"symbol": "600519"}),
                          lambda: SourceResult("", "tencent", "kline", "market", "success",
                                               as_of="2026-09-24"))
    assert result.status == "success"
    assert runtime.budget.snapshot()["market"] == 1
    assert runtime.budget.snapshot()["web"] == 0


def test_akshare_market_snapshot_is_blocked_when_external_market_sources_are_disabled():
    from types import SimpleNamespace
    from webapp.chat_execution import build_source_runtime
    from webapp.chat_models import Scope
    from webapp.source_adapters import SourceAccess
    from webapp.source_runtime import SourceCall, SourceResult

    access = SourceAccess(
        mcp_enabled=False, listed_tools=frozenset(), whitelist=frozenset(),
        mcp_allow=lambda: False, web_enabled=False, tencent_enabled=True, akshare_enabled=True,
    )
    runtime = build_source_runtime(
        scope=Scope.whole_corpus(), policy=SimpleNamespace(max_calls=5),
        cfg=SimpleNamespace(mcp_max_tool_calls=9), use_mcp=False,
        source_mode="market_recap", access=access,
    )
    invoked = []
    result = runtime.call(
        SourceCall("akshare", "a_share_market_snapshot", "market", {"trade_date": "2026-10-15"}),
        lambda: invoked.append(True) or SourceResult("", "akshare", "a_share_market_snapshot", "market", "success",
                                                      as_of="2026-10-15T15:00:00+08:00"),
    )

    assert result.status == "unavailable" and result.error_code == "not_authorized"
    index_result = runtime.call(
        SourceCall("akshare", "a_share_index_kline", "market", {
            "symbol": "sh000001", "start_date": "2026-10-06", "end_date": "2026-10-15",
        }),
        lambda: invoked.append(True) or SourceResult("", "akshare", "a_share_index_kline", "market", "success",
                                                      as_of="2026-10-15"),
    )
    assert index_result.status == "unavailable" and index_result.error_code == "not_authorized"
    assert invoked == []
    assert runtime.budget.snapshot() == {"total": 0, "market": 0, "web": 0}


def test_market_recap_runtime_budget_is_9_total_7_market_2_web_and_config_capped():
    from types import SimpleNamespace

    from webapp.chat_execution import build_source_runtime
    from webapp.chat_models import Scope
    from webapp.source_adapters import SourceAccess

    access = SourceAccess(
        mcp_enabled=True, listed_tools=frozenset({"stock_zt_pool", "stock_sector_fund_flow_rank"}),
        whitelist=frozenset(), mcp_allow=lambda: True,
        web_enabled=True, tencent_enabled=True,
    )
    policy = SimpleNamespace(max_calls=2)
    runtime = build_source_runtime(
        scope=Scope.whole_corpus(), policy=policy,
        cfg=SimpleNamespace(mcp_max_tool_calls=20), use_mcp=True,
        source_mode="market_recap", access=access,
    )

    assert all(runtime.budget.reserve("market") for _ in range(7))
    assert not runtime.budget.reserve("market")
    assert all(runtime.budget.reserve("web") for _ in range(2))
    assert not runtime.budget.reserve("web")
    assert runtime.budget.snapshot() == {"total": 9, "market": 7, "web": 2}

    capped = build_source_runtime(
        scope=Scope.whole_corpus(), policy=policy,
        cfg=SimpleNamespace(mcp_max_tool_calls=6), use_mcp=True,
        source_mode="market_recap", access=access,
    )
    assert all(capped.budget.reserve("market") for _ in range(6))
    assert not capped.budget.reserve("market")
    assert capped.budget.snapshot() == {"total": 6, "market": 6, "web": 0}


def test_tencent_kline_projection_creates_dated_market_facts_with_source_identity():
    from webapp.chat_execution import project_sources
    from webapp.chat_models import Scope
    from webapp.source_runtime import SourceCoverage, SourceResult

    result = SourceResult(
        "source-7", "tencent", "kline", "market", "success", as_of="2025-10-02",
        payload=({"symbol": "600900", "date": "2025-10-02", "close": 10.5},),
        coverage=SourceCoverage(query_window="2025-10-02", data_window="2025-10-02"),
    )

    projection = project_sources([result], Scope.company_only("600900", "长江电力"))

    assert len(projection.facts) == 1
    assert projection.facts[0].source_category == "market"
    assert projection.facts[0].period == "2025-10-02"
    assert projection.facts[0].evidence_ids == ("source-7:600900:2025-10-02",)
    assert projection.tool_artifacts[0].source_id == "source-7"


def test_market_task_requires_matching_dated_bars_and_never_trusts_empty_verification():
    from datetime import date

    from webapp.chat_execution import market_task_status
    from webapp.chat_models import Scope
    from webapp.chat_time import MarketWindow
    from webapp.source_runtime import SourceCoverage, SourceResult

    scope = Scope.company_only("600660", "福耀玻璃")
    window = MarketWindow("近五交易日", "rolling_trading_days", None, date(2026, 10, 9), 5)
    rows = tuple({"symbol": "600660", "date": f"2026-10-0{day}", "close": 10 + day}
                 for day in range(5, 10))
    complete = SourceResult("k1", "tencent", "kline", "market", "success",
        as_of="2026-10-09", payload=rows,
        coverage=SourceCoverage(returned_rows=5, data_window="2026-10-05至2026-10-09"))
    partial = SourceResult("k2", "tencent", "kline", "market", "partial",
        as_of="2026-10-09", payload=rows[:2],
        coverage=SourceCoverage(returned_rows=2, data_window="2026-10-05至2026-10-06"))
    wrong_company = SourceResult("k3", "tencent", "kline", "market", "success",
        as_of="2026-10-09", payload=tuple({**row, "symbol": "601288"} for row in rows),
        coverage=SourceCoverage(returned_rows=5, data_window="2026-10-05至2026-10-09"))

    assert market_task_status("market_trend", scope, window, [complete], "passed") == "completed"
    assert market_task_status("market_trend", scope, window, [partial], "passed") == "partial"
    assert market_task_status("market_trend", scope, window, [wrong_company], "passed") == "failed"
    assert market_task_status("market_trend", scope, window, [], "passed") == "failed"


def test_market_quote_requires_matching_company_and_timestamp():
    from datetime import date

    from webapp.chat_execution import market_task_status
    from webapp.chat_models import Scope
    from webapp.chat_time import MarketWindow
    from webapp.source_runtime import SourceResult

    scope = Scope.company_only("600660", "福耀玻璃")
    window = MarketWindow("今日", "explicit", date(2026, 10, 9), date(2026, 10, 9), None)
    quote = SourceResult("q1", "tencent", "quote", "market", "success",
        as_of="2026-10-09 15:00:00", payload=({"symbol": "sh600660", "code": "600660",
                                                  "time": "2026-10-09 15:00:00", "price": 10},))
    stale = SourceResult("q2", "tencent", "quote", "market", "success",
        as_of="2026-10-08 15:00:00", payload=({"symbol": "sh600660", "time": "2026-10-08 15:00:00"},))
    foreign = SourceResult("q3", "tencent", "quote", "market", "success",
        as_of="2026-10-09 15:00:00", payload=({"symbol": "sh601288", "time": "2026-10-09 15:00:00"},))

    assert market_task_status("market_quote", scope, window, [quote], "passed") == "completed"
    assert market_task_status("market_quote", scope, window, [stale], "passed") == "failed"
    assert market_task_status("market_quote", scope, window, [foreign], "passed") == "failed"


def test_market_overview_is_advertised_only_for_matching_today_or_five_trading_day_windows():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from webapp.chat_execution import market_overview_window_compatible
    from webapp.chat_time import resolve_market_window

    now = datetime(2026, 10, 9, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert market_overview_window_compatible(resolve_market_window("今日A股复盘", now))
    assert market_overview_window_compatible(resolve_market_window("A股近5个交易日整体表现", now))
    for query in ("大盘半年内主要指数走势", "A股近一年走势", "大盘过去18个月表现", "A股长期走势", "大盘过去5年走势"):
        assert not market_overview_window_compatible(resolve_market_window(query, now))


def test_daily_market_recap_requires_four_matching_indices_and_full_market_snapshot():
    from datetime import date

    from webapp.chat_execution import market_task_status
    from webapp.chat_models import Scope
    from webapp.chat_time import MarketWindow
    from webapp.source_runtime import SourceCoverage, SourceResult

    scope = Scope.whole_corpus()
    window = MarketWindow("今日", "explicit", date(2026, 10, 9), date(2026, 10, 9), None)
    symbols = ("sh000001", "sz399001", "sz399006", "sh000688")
    indices = [SourceResult(f"i{index}", "tencent", "kline", "market", "success",
        as_of="2026-10-09", payload=({"symbol": symbol, "date": "2026-10-09", "close": 1},),
        coverage=SourceCoverage(returned_rows=1, data_window="2026-10-09"))
        for index, symbol in enumerate(symbols)]
    snapshot = SourceResult(
        "b1", "akshare", "a_share_market_snapshot", "market", "success", as_of="2026-10-09T15:00:00+08:00",
        payload=({"trade_date": "2026-10-09"},),
        coverage=SourceCoverage(returned_rows=1, data_window="2026-10-09"),
    )
    partial_snapshot = SourceResult(
        "b2", "akshare", "a_share_market_snapshot", "market", "partial", as_of="2026-10-09T15:00:00+08:00",
        payload=({"trade_date": "2026-10-09"},),
        coverage=SourceCoverage(returned_rows=1, data_window="2026-10-09"),
    )

    assert market_task_status("market_recap", scope, window, indices, "passed") == "partial"
    assert market_task_status("market_recap", scope, window, indices + [snapshot], "passed") == "completed"
    assert market_task_status("market_recap", scope, window, indices + [partial_snapshot], "passed") == "partial"
    assert market_task_status("market_recap", scope, window, indices + [snapshot], "partial") == "partial"


def test_daily_recap_accepts_akshare_index_facts_with_previous_bar_for_comparison():
    from datetime import date
    from webapp.chat_execution import market_task_status
    from webapp.chat_models import Scope
    from webapp.chat_time import MarketWindow
    from webapp.source_runtime import SourceCoverage, SourceResult

    window = MarketWindow("今日", "explicit", date(2026, 10, 16), date(2026, 10, 16), None)
    symbols = ("sh000001", "sz399001", "sz399006", "sh000688")
    indices = [SourceResult(
        f"ak-{index}", "akshare", "a_share_index_kline", "market", "success",
        as_of="2026-10-16", payload=(
            {"symbol": symbol, "date": "2026-10-15", "close": 100},
            {"symbol": symbol, "date": "2026-10-16", "close": 101},
        ), coverage=SourceCoverage(returned_rows=2, data_window="2026-10-15至2026-10-16"),
    ) for index, symbol in enumerate(symbols)]
    snapshot = SourceResult(
        "breadth", "akshare", "a_share_market_snapshot", "market", "success",
        as_of="2026-10-16T15:00:00+08:00", payload=({"trade_date": "2026-10-16"},),
        coverage=SourceCoverage(returned_rows=1, data_window="2026-10-16"),
    )

    assert market_task_status("market_recap", Scope.whole_corpus(), window, indices + [snapshot], "passed") == "completed"


def test_source_coverage_is_projected_into_bounded_tool_artifact_summary():
    from webapp.chat_execution import project_sources
    from webapp.chat_models import Scope
    from webapp.source_runtime import SourceCoverage, SourceResult

    source = SourceResult(
        "s1", "mcp", "stock_zt_pool", "market", "partial", content="x" * 1000,
        as_of="2026-10-05", coverage=SourceCoverage(
            returned_rows=50, limit=50, query_window=None, data_window="2026-10-05",
        ),
    )

    projection = project_sources([source], Scope.whole_corpus())

    summary = projection.tool_artifacts[0].result_summary
    assert len(summary) <= 500
    assert "50 条" in summary
    assert "总量未知" in summary
    assert "2026-10-05" in summary
