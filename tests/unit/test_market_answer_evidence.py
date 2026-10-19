"""Generic source-reference verification for model-interpreted market answers."""

import importlib.util
import json
from dataclasses import fields

import pytest


def _api():
    spec = importlib.util.find_spec("webapp.market_answer_evidence")
    assert spec is not None, "market answer evidence verifier has not been implemented"
    import webapp.market_answer_evidence as api
    return api


def _draft(**claim_overrides):
    claim = {
        "metric": "price",
        "value": 8.59,
        "unit": "元/股",
        "period": "2026-10-19",
        "period_kind": "point_in_time",
        "entity_scope": "company",
        "company_code": "601398",
        "source_id": "call-1",
        "value_pointer": "/0/price",
        "identity_pointer": "/0/symbol",
        "time_pointer": "/0/time",
    }
    claim.update(claim_overrides)
    return {"answer": "工商银行现价 8.59 元/股，外部参考。", "claims": [claim]}


def _market_source(**changes):
    from webapp.source_runtime import SourceResult

    values = {
        "call_id": "call-1",
        "provider": "tencent",
        "operation": "quote",
        "category": "market",
        "status": "success",
        "as_of": "2026-10-19 14:55:00",
        "payload": ({
            "symbol": "sh601398",
            "price": 8.59,
            "time": "2026-10-19 14:55:00",
        },),
        "request_context": {"symbol": "sh601398"},
    }
    values.update(changes)
    if "content" not in changes and values.get("payload") is not None:
        values["content"] = json.dumps(values["payload"], ensure_ascii=False, separators=(",", ":"))
    if "request_context" not in {item.name for item in fields(SourceResult)}:
        values.pop("request_context", None)
    return SourceResult(**values)


def test_market_answer_draft_accepts_only_the_declared_schema():
    api = _api()
    parsed = api.parse_market_answer_draft(json.dumps(_draft(), ensure_ascii=False))
    assert parsed.answer == "工商银行现价 8.59 元/股，外部参考。"
    assert len(parsed.claims) == 1


@pytest.mark.parametrize("bad_claim", [
    {**_draft()["claims"][0], "unexpected": True},
    {**_draft()["claims"][0], "value": True},
    {**_draft()["claims"][0], "value": float("nan")},
    {**_draft()["claims"][0], "value_pointer": "/0/__proto__"},
])
def test_market_answer_draft_rejects_unsafe_claim_shapes(bad_claim):
    api = _api()
    raw = json.dumps({"answer": "现价 8.59 元/股", "claims": [bad_claim]}, ensure_ascii=False)
    with pytest.raises(ValueError):
        api.parse_market_answer_draft(raw)


def test_market_claim_is_bound_to_the_current_source_value_identity_and_time():
    api = _api()
    from webapp.chat_models import Scope

    draft = api.parse_market_answer_draft(json.dumps(_draft(), ensure_ascii=False))
    result = api.validate_market_answer_claims(
        draft,
        sources=(_market_source(),),
        existing_facts=(),
        scope=Scope.company_only("601398", "工商银行"),
        window=None,
        intent="market_quote",
    )
    assert result.status == "passed"
    assert len(result.facts) == 1
    assert result.facts[0].value == 8.59
    assert result.facts[0].evidence_ids == ("call-1#/0/price",)


@pytest.mark.parametrize("source,claim_changes", [
    (_market_source(payload=({"symbol": "sh600519", "price": 8.59, "time": "2026-10-19 14:55:00"},)), {}),
    (_market_source(as_of="2026-10-16 14:55:00", payload=({"symbol": "sh601398", "price": 8.59, "time": "2026-10-16 14:55:00"},)), {}),
    (_market_source(), {"value": 8.95}),
    (_market_source(), {"unit": "元/手"}),
    (_market_source(payload=({"price": 8.59, "time": "2026-10-19 14:55:00"},), request_context={}), {}),
])
def test_market_claim_mismatch_is_rejected(source, claim_changes):
    api = _api()
    from webapp.chat_models import Scope

    draft = api.parse_market_answer_draft(json.dumps(_draft(**claim_changes), ensure_ascii=False))
    result = api.validate_market_answer_claims(
        draft,
        sources=(source,),
        existing_facts=(),
        scope=Scope.company_only("601398", "工商银行"),
        window=None,
        intent="market_quote",
    )
    assert result.status in {"partial", "blocked"}
    assert not result.facts
    assert result.rejected


def test_model_cannot_cite_payload_rows_not_visible_in_the_bounded_source_snapshot():
    api = _api()
    from webapp.chat_models import Scope

    hidden = _market_source(content='{"returned_rows":800,"sampled_bars":[]}')
    draft = api.parse_market_answer_draft(json.dumps(_draft(), ensure_ascii=False))
    result = api.validate_market_answer_claims(
        draft, sources=(hidden,), existing_facts=(),
        scope=Scope.company_only("601398", "工商银行"), window=None, intent="market_quote",
    )

    assert result.status == "blocked"
    assert result.rejected == ("source_snapshot_not_visible",)


def test_market_claim_cannot_cross_run_missing_path_or_requested_window():

    api = _api()
    from datetime import date
    from types import SimpleNamespace
    from webapp.chat_models import Scope

    scope = Scope.company_only("601398", "工商银行")
    for claim_changes, window in (
        ({"source_id": "other-run"}, None),
        ({"value_pointer": "/0/not_present"}, None),
        ({}, SimpleNamespace(start_date=date(2026, 10, 20), end_date=date(2026, 10, 21))),
    ):
        draft = api.parse_market_answer_draft(json.dumps(_draft(**claim_changes), ensure_ascii=False))
        result = api.validate_market_answer_claims(
            draft, sources=(_market_source(),), existing_facts=(), scope=scope,
            window=window, intent="market_quote",
        )
        assert result.status == "blocked"
        assert result.rejected


def test_model_selected_pointers_support_a_different_payload_shape_without_provider_mapping():

    api = _api()
    from webapp.chat_models import Scope

    source = _market_source(payload=({
        "ticker": "601398",
        "last_price": 8.59,
        "quote_time": "2026-10-19 14:55:00",
    },))
    draft_data = _draft(value_pointer="/0/last_price", identity_pointer="/0/ticker", time_pointer="/0/quote_time")
    draft = api.parse_market_answer_draft(json.dumps(draft_data, ensure_ascii=False))
    result = api.validate_market_answer_claims(
        draft,
        sources=(source,),
        existing_facts=(),
        scope=Scope.company_only("601398", "工商银行"),
        window=None,
        intent="market_quote",
    )
    assert result.status == "passed"
    assert result.facts[0].value == 8.59


def test_raw_provider_change_percentage_is_not_accepted_as_recalculated_evidence():
    api = _api()
    from webapp.chat_models import Scope

    source = _market_source(payload=({
        "symbol": "sh601398", "price": 8.59, "prev_close": 8.50,
        "change_pct": 1.06, "time": "2026-10-19 14:55:00",
    },))
    claim = _draft(
        metric="price_change_pct", value=1.06, unit="%",
        value_pointer="/0/change_pct", formula="",
    )
    draft = api.parse_market_answer_draft(json.dumps(claim, ensure_ascii=False))
    result = api.validate_market_answer_claims(
        draft, sources=(source,), existing_facts=(),
        scope=Scope.company_only("601398", "工商银行"), window=None, intent="market_quote",
    )

    assert result.status == "blocked"
    assert not result.facts
    assert "derived_metric_requires_server_fact" in result.rejected


def test_derived_market_fact_requires_its_current_run_lineage():
    api = _api()
    from webapp.chat_models import Fact, Scope

    current = Fact("price", 8.59, "元/股", "2026-10-19", "point_in_time", "security", "601398",
                   "tool", ("call-1#/0/price",), "reference", "2026-10-19 14:55:00", source_category="market")
    previous = Fact("previous_close", 8.50, "元/股", "2026-10-16", "point_in_time", "security", "601398",
                    "tool", ("call-1#/0/prev_close",), "reference", "2026-10-19 14:55:00", source_category="market")
    derived = Fact("price_change_pct", 1.0588235, "百分比", "2026-10-19", "interval", "security", "601398",
                   "derived", ("market-formula",), "verified", source_category="market",
                   derived_from_ids=(current.id, previous.id), formula="price_change_pct")
    claim = _draft(
        metric="price_change_pct", value=derived.value, unit=derived.unit,
        period_kind="interval", entity_scope="security", source_id="", value_pointer="", identity_pointer="", time_pointer="",
        fact_id=derived.id, formula="price_change_pct",
    )
    draft = api.parse_market_answer_draft(json.dumps(claim, ensure_ascii=False))
    scope = Scope.company_only("601398", "工商银行")

    missing_lineage = api.validate_market_answer_claims(
        draft, sources=(), existing_facts=(derived,), scope=scope, window=None, intent="market_quote",
    )
    complete_lineage = api.validate_market_answer_claims(
        draft, sources=(), existing_facts=(current, previous, derived),
        scope=scope, window=None, intent="market_quote",
    )

    assert missing_lineage.status == "blocked"
    assert complete_lineage.status == "passed"
    assert complete_lineage.facts == (derived,)


def test_json_pointer_follows_rfc6901_escaping_for_slashes_and_tildes():

    api = _api()

    assert api._resolve_pointer({"a/b": {"~2": 8.59}}, "/a~1b/~02") == 8.59
    with pytest.raises(ValueError):
        api._resolve_pointer({"a": 1}, "/a~2")


def test_invalid_json_and_unavailable_sources_fail_closed():

    api = _api()
    from webapp.chat_models import Scope

    with pytest.raises(ValueError):
        api.parse_market_answer_draft("not json")
    draft = api.parse_market_answer_draft(json.dumps(_draft(), ensure_ascii=False))
    result = api.validate_market_answer_claims(
        draft,
        sources=(_market_source(status="failed", payload=None),),
        existing_facts=(),
        scope=Scope.company_only("601398", "工商银行"),
        window=None,
        intent="market_quote",
    )
    assert result.status == "blocked"
    assert not result.facts
