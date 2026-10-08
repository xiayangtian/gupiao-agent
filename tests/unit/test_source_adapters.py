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
