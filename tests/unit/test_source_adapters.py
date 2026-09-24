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
