import pytest


def test_error_or_empty_payload_is_never_success():
    from webapp.source_runtime import SourceCall
    from webapp.source_adapters import normalize_source

    for raw in ("Error: denied", {"error": "timeout"}, {"success": False}, [], {"results": []}):
        result = normalize_source(
            SourceCall("fixture", "quote", "market", {}), raw,
            fetched_at="2026-09-24T10:00:00+08:00",
        )
        assert result.status in {"failed", "unavailable"}
        assert result.content == ""


def test_valid_rows_are_bounded_and_unknown_time_stays_unknown():
    from webapp.source_runtime import SourceCall
    from webapp.source_adapters import normalize_source

    result = normalize_source(
        SourceCall("fixture", "quote", "market", {}),
        {"data": [{"name": "示例", "price": 10.5}]},
        fetched_at="2026-09-24T10:00:00+08:00",
    )
    assert result.status == "partial"
    assert result.as_of == ""
    assert result.fetched_at == "2026-09-24T10:00:00+08:00"
    assert result.payload == ({"name": "示例", "price": 10.5},)


def test_authorization_requires_source_capability_and_switch():
    from webapp.source_adapters import SourceAccess
    from webapp.source_runtime import SourceCall

    access = SourceAccess(
        mcp_enabled=True, listed_tools=frozenset({"get_quote"}),
        whitelist=frozenset(), mcp_allow=lambda: True,
        web_enabled=False, tencent_enabled=True,
    )
    assert access.permits(SourceCall("mcp", "get_quote", "market", {}))
    assert not access.permits(SourceCall("mcp", "other", "market", {}))
    assert not access.permits(SourceCall("web", "search", "web", {}))
    assert access.permits(SourceCall("tencent", "quote", "market", {}))
    assert not access.permits(SourceCall("akshare", "a_share_market_snapshot", "market", {"trade_date": "2026-10-15"}))


def test_deepseek_search_requires_explicit_keyed_capability_and_web_category():
    from webapp.source_adapters import SourceAccess
    from webapp.source_runtime import SourceCall

    access = SourceAccess(
        mcp_enabled=False, listed_tools=frozenset(), whitelist=frozenset(),
        mcp_allow=lambda: False, web_enabled=False, tencent_enabled=False,
        deepseek_enabled=True,
    )
    assert access.permits(SourceCall("deepseek_search", "web_search", "web", {"query": "x"}))
    assert not access.permits(SourceCall("deepseek_search", "web_search", "market", {"query": "x"}))
    assert not SourceAccess(
        mcp_enabled=False, listed_tools=frozenset(), whitelist=frozenset(),
        mcp_allow=lambda: False, web_enabled=False, tencent_enabled=False,
    ).permits(SourceCall("deepseek_search", "web_search", "web", {"query": "x"}))


def test_akshare_snapshot_requires_exact_server_operation_and_iso_date():
    from webapp.source_adapters import SourceAccess
    from webapp.source_runtime import SourceCall

    access = SourceAccess(
        mcp_enabled=False, listed_tools=frozenset(), whitelist=frozenset(),
        mcp_allow=lambda: False, web_enabled=False, tencent_enabled=False,
        akshare_enabled=True,
    )
    assert access.permits(SourceCall("akshare", "a_share_market_snapshot", "market", {"trade_date": "2026-10-15"}))
    assert not access.permits(SourceCall("akshare", "other", "market", {"trade_date": "2026-10-15"}))
    assert not access.permits(SourceCall("akshare", "a_share_market_snapshot", "market", {"trade_date": "2026-10-15", "code": "600000"}))
    assert not access.permits(SourceCall("akshare", "a_share_market_snapshot", "market", {"trade_date": "2026-02-30"}))


def test_akshare_index_kline_requires_whitelisted_symbol_and_narrow_server_dates():
    from webapp.source_adapters import SourceAccess
    from webapp.source_runtime import SourceCall

    access = SourceAccess(
        mcp_enabled=False, listed_tools=frozenset(), whitelist=frozenset(),
        mcp_allow=lambda: False, web_enabled=False, tencent_enabled=False, akshare_enabled=True,
    )
    valid = SourceCall("akshare", "a_share_index_kline", "market", {
        "symbol": "sh000001", "start_date": "2026-10-06", "end_date": "2026-10-16",
    })
    assert access.permits(valid)
    assert not access.permits(SourceCall("akshare", "a_share_index_kline", "market", {
        "symbol": "600000", "start_date": "2026-10-06", "end_date": "2026-10-16",
    }))
    assert not access.permits(SourceCall("akshare", "a_share_index_kline", "market", {
        "symbol": "sh000001", "start_date": "2026-09-01", "end_date": "2026-10-16",
    }))
    assert not access.permits(SourceCall("akshare", "a_share_index_kline", "market", {
        "symbol": "sh000001", "start_date": "2026-10-17", "end_date": "2026-10-16",
    }))
    assert not access.permits(SourceCall("akshare", "a_share_index_kline", "market", {
        "symbol": "sh000001", "start_date": "2026-10-06", "end_date": "2026-10-16", "period": "minute",
    }))


def test_akshare_summary_status_and_coverage_survive_source_normalization():
    from webapp.source_adapters import normalize_source
    from webapp.source_runtime import SourceCall

    result = normalize_source(
        SourceCall("akshare", "a_share_market_snapshot", "market", {"trade_date": "2026-10-15"}),
        {"status": "partial", "trade_date": "2026-10-15", "as_of": "2026-10-15T15:00:00+08:00",
         "advancing_count": None, "universe_rows": 5000, "limitations": "coverage incomplete"},
        fetched_at="2026-10-15T15:00:00+08:00",
    )
    assert result.status == "partial"
    assert result.as_of == "2026-10-15T15:00:00+08:00"
    assert result.coverage.data_window == "2026-10-15"
    assert result.payload[0]["universe_rows"] == 5000


def test_akshare_index_bars_normalize_with_requested_date_coverage():
    from webapp.source_adapters import normalize_source
    from webapp.source_runtime import SourceCall

    call = SourceCall("akshare", "a_share_index_kline", "market", {
        "symbol": "sh000001", "start_date": "2026-10-06", "end_date": "2026-10-16",
    })
    result = normalize_source(call, {
        "as_of": "2026-10-16",
        "bars": [
            {"date": "2026-10-15", "close": 3707.48, "symbol": "sh000001", "kline_period": "day"},
            {"date": "2026-10-16", "close": 3692.45, "symbol": "sh000001", "kline_period": "day"},
        ],
    }, fetched_at="2026-10-16T14:30:00+08:00")

    assert result.status == "success"
    assert result.as_of == "2026-10-16"
    assert result.coverage.returned_rows == 2
    assert result.coverage.query_window == "2026-10-06至2026-10-16"
    assert result.coverage.data_window == "2026-10-15至2026-10-16"
    assert result.payload[-1]["close"] == 3692.45


def test_unsupported_text_is_never_promoted_to_data():
    from webapp.source_runtime import SourceCall
    from webapp.source_adapters import normalize_source

    result = normalize_source(
        SourceCall("fixture", "quote", "market", {}), "plain provider chatter",
        fetched_at="2026-09-24T10:00:00+08:00",
    )
    assert result.status == "unavailable"
    assert result.error_code == "unsupported_payload"
    assert result.content == ""


def test_market_mcp_coverage_keeps_only_allowlisted_counts_and_windows():
    from webapp.source_runtime import SourceCall, SourceCoverage
    from webapp.source_adapters import normalize_source

    call = SourceCall("mcp", "stock_zt_pool", "market", {"pool_type": "涨停", "limit": 50})
    result = normalize_source(call, {
        "data": [{"date": "2026-10-05", "symbol": "600001"}],
        "total_rows": 120,
        "private_provider_metadata": "must not persist",
    })

    assert isinstance(result.coverage, SourceCoverage)
    assert result.coverage.returned_rows == 1
    assert result.coverage.limit == 50
    assert result.coverage.total_rows == 120
    assert result.coverage.query_window is None
    assert result.coverage.data_window == "2026-10-05"
    assert "private_provider_metadata" not in str(result.payload)
    assert "total_rows" not in str(result.payload)


def test_market_mcp_coverage_does_not_infer_unknown_totals():
    from webapp.source_runtime import SourceCall
    from webapp.source_adapters import normalize_source

    result = normalize_source(
        SourceCall("mcp", "stock_sector_fund_flow_rank", "market", {"days": "5日", "cate": "行业资金流"}),
        {"data": [{"date": "2026-10-05", "sector": "电力"}]},
    )

    assert result.coverage.total_rows is None
    assert result.coverage.query_window == "5日"
    assert result.coverage.data_window == "2026-10-05"


def test_tencent_kline_coverage_records_requested_granularity_and_window():
    from webapp.source_runtime import SourceCall
    from webapp.source_adapters import normalize_source

    result = normalize_source(
        SourceCall("tencent", "kline", "market", {
            "symbol": "600900", "period": "week", "count": 250, "window": "过去五年",
        }),
        {"as_of": "2026-10-02", "bars": [
            {"symbol": "600900", "date": "2026-09-25", "close": 10.5},
        ]},
    )

    assert result.coverage.limit == 250
    assert result.coverage.query_window == "过去五年"
    assert result.coverage.data_window == "2026-09-25"
    assert "来源查询期 过去五年" in result.coverage.summary()


def test_tencent_kline_keeps_up_to_800_facts_but_bounds_prompt_content():
    from datetime import date, timedelta
    from webapp.source_runtime import SourceCall
    from webapp.source_adapters import normalize_source

    start = date(2025, 1, 1)
    bars = [{"symbol": "600900", "date": (start + timedelta(days=index)).isoformat(),
             "close": 10.0 + index / 100} for index in range(120)]
    result = normalize_source(SourceCall("tencent", "kline", "market", {
        "symbol": "600900", "period": "day", "count": 120, "window": "近120自然日",
    }), {"as_of": bars[-1]["date"], "bars": bars})

    assert len(result.payload) == 120
    assert result.coverage.returned_rows == 120
    assert result.coverage.data_window == "2025-01-01至2025-04-30"
    assert len(result.content) < 1000
    assert '"returned_rows":120' in result.content


def test_market_pool_at_limit_does_not_claim_total_completeness():
    from webapp.source_runtime import SourceCall
    from webapp.source_adapters import normalize_source

    result = normalize_source(
        SourceCall("mcp", "stock_zt_pool", "market", {"pool_type": "涨停", "limit": 50}),
        {"data": [{"symbol": f"600{i:03d}", "date": "2026-10-05"} for i in range(51)]},
    )

    assert result.coverage.returned_rows == 50
    assert result.coverage.limit == 50
    assert result.coverage.total_rows is None
    assert "已达到返回上限，总量未知" in result.coverage.summary()


def test_tencent_quote_nested_bid_ask_keeps_only_scalar_market_fields():
    from webapp.source_runtime import SourceCall
    from webapp.source_adapters import normalize_source

    result = normalize_source(SourceCall("tencent", "quote", "market", {"symbol": "600900"}), {
        "data": [{
            "symbol": "sh600900", "code": "600900", "name": "长江电力",
            "price": 27.3, "change": 0.3, "change_pct": 1.11,
            "time": "2026-10-19 14:10:00",
            "bid_ask": {"bid1": {"price": 27.3, "volume": 1200}},
        }],
    })

    assert result.status == "success"
    assert result.payload[0]["price"] == 27.3
    assert result.payload[0]["change_pct"] == 1.11
    assert "bid_ask" not in result.payload[0]


def test_source_result_carries_only_safe_server_market_request_context():
    from webapp.source_adapters import normalize_source
    from webapp.source_runtime import SourceCall

    market = normalize_source(SourceCall("tencent", "quote", "market", {
        "symbol": "sh601398", "request_token": "must-not-be-retained",
    }), {"data": [{"symbol": "sh601398", "price": 8.59, "time": "2026-10-19 14:55:00"}]})
    web = normalize_source(SourceCall("web", "search", "web", {
        "query": "长江电力 private-query",
    }), {"results": [{"url": "https://example.test/a", "title": "headline"}]})

    assert getattr(market, "request_context", None) == {"symbol": "sh601398"}
    assert getattr(web, "request_context", None) == {}
    assert "private-query" not in str(getattr(web, "request_context", {}))


def test_baostock_access_is_opt_in_and_restricted_to_fixed_daily_index_calls():

    from webapp.source_adapters import SourceAccess
    from webapp.source_runtime import SourceCall

    common = dict(
        mcp_enabled=False, listed_tools=frozenset(), whitelist=frozenset(),
        mcp_allow=lambda: False, web_enabled=False, tencent_enabled=False,
        akshare_enabled=True,
    )
    call = SourceCall("baostock", "a_share_index_kline", "market", {
        "symbol": "sh000001", "start_date": "2026-10-15", "end_date": "2026-10-16",
    })
    assert not SourceAccess(**common).permits(call)

    access = SourceAccess(**common, baostock_enabled=True)
    assert access.permits(call)
    assert not access.permits(SourceCall("baostock", "a_share_index_kline", "web", call.arguments))
    assert not access.permits(SourceCall("baostock", "a_share_market_snapshot", "market", call.arguments))
    assert not access.permits(SourceCall("baostock", "a_share_index_kline", "market", {
        "symbol": "600900", "start_date": "2026-10-15", "end_date": "2026-10-16",
    }))
    assert not access.permits(SourceCall("baostock", "a_share_index_kline", "market", {
        "symbol": "sh000001", "start_date": "2026-10-01", "end_date": "2026-10-16",
    }))
    assert not access.permits(SourceCall("baostock", "a_share_index_kline", "market", {
        **call.arguments, "adjust": "qfq",
    }))
