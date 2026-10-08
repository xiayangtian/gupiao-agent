from webapp.chat_facts import FactNormalizer, detect_conflicts
from webapp.chat_models import EvidenceArtifact, Fact, Scope, ToolArtifact


def _scope():
    return Scope.company_only("601288", "农业银行", ["601288:2026-06-30:semi_annual"])


def _pdf():
    return EvidenceArtifact.pdf("601288:2026-06-30:semi_annual", "abc.pdf", 40, "营业收入")


def test_normalizes_yuan_to_yi_without_losing_original_unit():
    fact = FactNormalizer().normalize({"metric": "revenue", "value": 410871000000, "unit": "元", "period": "2026-06-30", "period_kind": "semi_annual_cumulative", "entity_scope": "consolidated", "company_code": "601288", "evidence_ids": ["pdf_p40"]}, _pdf(), _scope())
    assert fact is not None
    assert fact.value == 4108.71
    assert fact.unit == "亿元"
    assert fact.original_value == 410871000000
    assert fact.verification == "verified"


def test_rejects_comparison_between_single_quarter_and_half_year_cumulative():
    normalizer = FactNormalizer()
    raw = {"metric": "revenue", "value": 100, "unit": "亿元", "entity_scope": "consolidated", "company_code": "601288", "evidence_ids": ["pdf_p1"]}
    quarter_pdf = EvidenceArtifact.pdf("601288:2026-03-31:quarterly", "abc.pdf", 40, "营业收入")
    comparable_scope = Scope.company_only("601288", "农业银行", [
        "601288:2026-03-31:quarterly", "601288:2026-06-30:semi_annual",
    ])
    quarter = normalizer.normalize(raw | {"period": "2026-03-31", "period_kind": "quarterly_cumulative"}, quarter_pdf, comparable_scope)
    half = normalizer.normalize(raw | {"period": "2026-06-30", "period_kind": "semi_annual_cumulative"}, _pdf(), comparable_scope)
    assert quarter is not None and half is not None
    assert normalizer.comparable(quarter, half) is False


def test_same_metric_same_scope_different_values_create_conflict():
    normalizer = FactNormalizer()
    raw = {"metric": "revenue", "unit": "亿元", "period": "2026-06-30", "period_kind": "semi_annual_cumulative", "entity_scope": "consolidated", "company_code": "601288", "evidence_ids": ["pdf_p1"]}
    pdf = normalizer.normalize(raw | {"value": 100}, _pdf(), _scope())
    tool = ToolArtifact("market", "get_quote", as_of="2026-09-15T12:00:00", status="success")
    external = normalizer.normalize(raw | {"value": 120, "evidence_ids": ["tool:market:get_quote"], "provider": "market", "as_of": tool.as_of}, tool, _scope())
    assert pdf is not None and external is not None
    conflicts = detect_conflicts([pdf, external])
    assert conflicts[0].reason == "同指标同期间同口径数值不一致"
    assert external.verification == "reference"


def test_rejects_incomplete_or_web_numeric_fact():
    normalizer = FactNormalizer()
    web = EvidenceArtifact.web("https://example.test/a", "news", "100亿元", fetched_at="2026-09-15T12:00:00")
    raw = {"metric": "revenue", "value": 100, "unit": "亿元", "period": "2026-06-30", "period_kind": "semi_annual_cumulative", "entity_scope": "consolidated", "company_code": "601288", "evidence_ids": ["web:a"]}
    assert normalizer.normalize(raw, web, _scope()) is None


def test_extracts_pdf_facts_only_from_literal_in_scope_metric_and_page_evidence():
    artifact = EvidenceArtifact.pdf(
        "601288:2025-06-30:semi_annual", "report.pdf", 40,
        "营业收入 4,108.71 亿元，归母净利润 1,234.5 万元。",
    )
    scope = Scope.company_only("601288", "农业银行", ["601288:2025-06-30:semi_annual"])

    facts = FactNormalizer().facts_from_pdf_evidence(artifact, scope)

    assert len(facts) == 2
    assert facts[0].metric == "revenue"
    assert facts[0].value == 4108.71
    assert facts[0].source_category == "local_pdf"
    assert facts[0].evidence_ids[0] == "601288:2025-06-30:semi_annual#p40"
    assert facts[1].metric == "net_profit"
    assert facts[1].entity_scope == "parent"
    assert FactNormalizer().facts_from_pdf_evidence(artifact, Scope.company_only("600900", "长江电力", ())) == ()
    missing = EvidenceArtifact.pdf(
        "601288:2025-06-30:semi_annual", "report.pdf", 40,
        "营业收入 4,108.71 亿元", availability="missing_file",
    )
    assert FactNormalizer().facts_from_pdf_evidence(missing, scope) == ()


def test_quarterly_pdf_snippet_is_not_assumed_to_be_single_quarter():
    artifact = EvidenceArtifact.pdf(
        "601288:2025-09-30:quarterly", "q3.pdf", 4, "营业收入 100 亿元",
    )
    scope = Scope.company_only("601288", "农业银行", [artifact.report_id])

    facts = FactNormalizer().facts_from_pdf_evidence(artifact, scope)

    assert len(facts) == 1
    assert facts[0].period_kind == "quarterly_cumulative"
    assert FactNormalizer().normalize({
        "metric": "revenue", "value": 100, "unit": "亿元", "period": "2025-09-30",
        "period_kind": "single_quarter", "entity_scope": "unknown", "company_code": "601288",
        "evidence_ids": ["untrusted"],
    }, artifact, scope) is None


def test_normalizes_percentage_units_without_converting_to_percentage_points():
    raw = {"metric": "revenue_growth", "value": 2.5, "unit": "%", "period": "2026-06-30",
           "period_kind": "semi_annual_cumulative", "entity_scope": "consolidated", "company_code": "601288",
           "evidence_ids": ["r#p1"]}
    fact = FactNormalizer().normalize(raw, _pdf(), _scope())

    assert fact is not None
    assert fact.value == 2.5
    assert fact.unit == "百分比"
    assert fact.original_unit == "%"
    assert fact.source_category == "local_pdf"


def _fact(metric, value, period, *, unit="亿元", category="local_pdf", code="601288", period_kind="annual"):
    return Fact(
        metric, value, unit, period, period_kind, "consolidated", code, "pdf" if category == "local_pdf" else "tool",
        (f"{category}:{period}:{metric}",), "verified", "2025-12-31T00:00:00+08:00" if category != "local_pdf" else "",
        source_category=category,
    )


def test_derive_yoy_growth_with_traceable_inputs():
    from webapp.chat_facts import derive_growth

    baseline = _fact("revenue", 100, "2024-12-31")
    current = _fact("revenue", 125, "2025-12-31")

    derived = derive_growth(current, baseline, relation="yoy")

    assert derived is not None
    assert derived.value == 25
    assert derived.unit == "百分比"
    assert derived.derived_from_ids == (current.id, baseline.id)
    assert set(derived.evidence_ids) == set(current.evidence_ids + baseline.evidence_ids)
    assert derived.formula


def test_derive_percentage_point_and_index_deltas_with_distinct_units():
    from webapp.chat_facts import derive_index_delta, derive_percentage_point_delta

    previous_ratio = _fact("roe", 2.5, "2024-12-31", unit="百分比")
    current_ratio = _fact("roe", 3.0, "2025-12-31", unit="百分比")
    old_index = _fact("index_close", 3000, "2025-10-01", unit="点", category="market", code="sh000001", period_kind="daily")
    new_index = _fact("index_close", 3030, "2025-10-02", unit="点", category="market", code="sh000001", period_kind="daily")

    assert derive_percentage_point_delta(current_ratio, previous_ratio).value == 0.5
    assert derive_percentage_point_delta(current_ratio, previous_ratio).unit == "百分点"
    assert derive_index_delta(new_index, old_index).value == 30
    assert derive_index_delta(new_index, old_index).unit == "点"


def test_yoy_accepts_same_quarter_and_qoq_accepts_only_adjacent_quarters():
    from webapp.chat_facts import derive_growth

    q1_last_year = _fact("revenue", 100, "2024-03-31", period_kind="single_quarter")
    q1_this_year = _fact("revenue", 125, "2025-03-31", period_kind="single_quarter")
    q2_this_year = _fact("revenue", 150, "2025-06-30", period_kind="single_quarter")
    q3_this_year = _fact("revenue", 200, "2025-09-30", period_kind="single_quarter")

    assert derive_growth(q1_this_year, q1_last_year, relation="yoy").value == 25
    assert derive_growth(q2_this_year, q1_this_year, relation="qoq").value == 20
    assert derive_growth(q3_this_year, q1_this_year, relation="qoq") is None


def test_derivations_reject_zero_base_incompatible_sources_units_entities_and_periods():
    from webapp.chat_facts import derive_growth, derive_index_delta, derive_percentage_point_delta

    current = _fact("revenue", 125, "2025-12-31")
    zero = _fact("revenue", 0, "2024-12-31")
    other_source = _fact("revenue", 100, "2024-12-31", category="mcp")
    other_company = _fact("revenue", 100, "2024-12-31", code="600900")
    other_unit = _fact("revenue", 100, "2024-12-31", unit="元")
    incompatible = _fact("revenue", 100, "2024-09-30", period_kind="quarterly")

    assert derive_growth(current, zero, relation="yoy") is None
    assert derive_growth(zero, zero, relation="yoy") is None
    assert derive_growth(current, other_source, relation="yoy") is None
    assert derive_growth(current, other_company, relation="yoy") is None
    assert derive_growth(current, other_unit, relation="yoy") is None
    assert derive_growth(current, incompatible, relation="yoy") is None
    assert derive_percentage_point_delta(current, other_unit) is None
    assert derive_index_delta(current, incompatible) is None


def test_derive_available_facts_only_emits_requested_financial_or_index_comparisons():
    from webapp.chat_facts import derive_available_facts

    baseline = _fact("revenue", 100, "2024-12-31")
    current = _fact("revenue", 125, "2025-12-31")
    old_index = _fact("index_close", 3000, "2025-10-01", unit="点", category="market", code="sh000001", period_kind="daily")
    new_index = _fact("index_close", 3030, "2025-10-02", unit="点", category="market", code="sh000001", period_kind="daily")

    assert derive_available_facts([baseline, current]) == ()
    yoy = derive_available_facts([baseline, current], relations=("yoy",))
    index = derive_available_facts([old_index, new_index], index_delta=True)
    assert len(yoy) == 1 and yoy[0].value == 25 and yoy[0].derived_from_ids
    assert len(index) == 1 and index[0].metric == "index_close_delta" and index[0].value == 30


def test_tencent_kline_rows_become_market_facts_only_with_bounded_dated_payload():
    from webapp.chat_facts import facts_from_market_result
    from webapp.source_runtime import SourceCoverage, SourceResult

    result = SourceResult(
        "source-7", "tencent", "kline", "market", "success", as_of="2025-10-02",
        payload=({"symbol": "600900", "date": "2025-10-02", "close": 10.5},
                 {"symbol": "600900", "date": "missing", "close": 11.0}),
        coverage=SourceCoverage(data_window="2025-10-02"),
    )
    artifact = ToolArtifact("tencent", "kline", as_of="2025-10-02", status="success", source_id="source-7")

    facts = facts_from_market_result(result, artifact)

    assert len(facts) == 1
    assert facts[0].source_category == "market"
    assert facts[0].metric == "price"
    assert facts[0].period == "2025-10-02"
    assert facts[0].evidence_ids
    assert facts_from_market_result(result, ToolArtifact("mcp", "kline", as_of="2025-10-02", status="success")) == ()
