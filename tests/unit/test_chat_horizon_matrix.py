import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from webapp.chat_request_resolution import Clarification, ResolvedRequest, resolve_chat_request
from webapp.chat_scope import ScopeRequest

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "market_horizon_queries.json"
_MATRIX = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_NOW = datetime.fromisoformat(_MATRIX["now"]).astimezone(ZoneInfo("Asia/Shanghai"))


@pytest.mark.parametrize("case", _MATRIX["cases"], ids=lambda case: case["id"])
def test_saved_market_horizon_queries_resolve_to_expected_scope_and_window(case):
    resolved = resolve_chat_request(case["query"], ScopeRequest.auto(), None, _NOW)

    assert resolved.intent == case["expected_intent"] if isinstance(resolved, Clarification) else resolved.route.intent == case["expected_intent"]
    if case["expected_status"] == "clarification":
        assert isinstance(resolved, Clarification)
        assert resolved.intent == case["expected_intent"]
        assert resolved.missing_field == "window"
        return

    assert isinstance(resolved, ResolvedRequest)
    if case["subject_kind"] == "broad_market":
        assert resolved.route.requires_company is False
    else:
        assert resolved.route.requires_company is True

    assert resolved.window_origin == case["window_origin"]
    assert resolved.market_window is not None
    window = resolved.market_window
    assert window.kind == case["window_kind"]
    if "window_start" in case:
        assert window.start_date.isoformat() == case["window_start"]
        assert window.end_date.isoformat() == case["window_end"]
    if "trading_days" in case:
        assert window.trading_days == case["trading_days"]
        assert window.end_date == _NOW.date()


def test_explicit_one_year_financial_trend_does_not_become_market_trend():
    result = resolve_chat_request("农业银行近一年营收趋势", ScopeRequest.auto(), None, _NOW)

    assert isinstance(result, ResolvedRequest)
    assert result.route.intent == "financial_trend"
    assert result.market_window is None


def test_orphan_medium_or_long_horizon_clarifies_subject():
    for query in ("中期行情", "长期走势"):
        result = resolve_chat_request(query, ScopeRequest.auto(), None, _NOW)
        assert isinstance(result, Clarification), query
        assert result.missing_field == "subject", query
