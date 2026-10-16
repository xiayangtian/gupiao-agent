from webapp.chat_models import EvidenceArtifact, Fact, FactConflict, Scope
from webapp.chat_verifier import SAFE_UNSUPPORTED_CLAIM_TEXT, ClaimVerifier


def _scope():
    return Scope.company_only("601288", "农业银行", ["601288:2026-06-30:semi_annual"])


def _fact(code="601288", value=100):
    return Fact("营业收入", value, "亿元", "2026-06-30", "semi_annual_cumulative", "consolidated", code, "pdf", ("pdf_p40",), "verified")


def _artifact(code="601288"):
    return EvidenceArtifact.pdf(f"{code}:2026-06-30:semi_annual", "report.pdf", 40, "营业收入", pdf_url="/api/history-pdf/report.pdf#page=40")


def test_numeric_claim_without_supporting_fact_is_blocked():
    report = ClaimVerifier().verify("营收为 4108.71 亿元", _scope(), facts=(), artifacts=(), conflicts=())
    assert report.status == "blocked"
    assert report.issues[0].code == "unsupported_numeric_claim"


def test_scope_external_fact_is_blocked_even_when_number_has_artifact():
    report = ClaimVerifier().verify("长江电力营收为 100 亿元", _scope(), [_fact("600900")], [_artifact("600900")], ())
    assert report.status == "blocked"
    assert any(issue.code == "scope_violation" for issue in report.issues)


def test_conflict_requires_disclosure_in_answer():
    facts = (_fact(value=100), _fact(value=120))
    conflict = FactConflict("营业收入", facts, "同指标同期间同口径数值不一致")
    report = ClaimVerifier().verify("营业收入为 100 亿元", _scope(), facts, [_artifact()], [conflict])
    assert report.status == "partial"
    assert any(issue.code == "undisclosed_conflict" for issue in report.issues)


def test_reference_requires_external_reference_wording():
    fact = Fact("价格", 10, "元/股", "as_of", "point_in_time", "consolidated", "601288", "tool", ("tool:quote",), "reference", "2026-09-15")
    report = ClaimVerifier().verify("价格为 10 元/股", _scope(), [fact], (), ())
    assert report.status == "partial"
    assert any(issue.code == "external_reference_ambiguity" for issue in report.issues)


def test_unavailable_pdf_file_cannot_back_a_numeric_fact():
    unavailable = EvidenceArtifact.pdf(
        "601288:2026-06-30:semi_annual", "report.pdf", 40, "营业收入 100 亿元",
        availability="missing_file",
    )
    fact = _fact(value=100)
    report = ClaimVerifier().verify("营业收入为 100 亿元", _scope(), [fact], [unavailable], ())

    assert report.status == "blocked"
    assert any(issue.code == "invalid_pdf_evidence" for issue in report.issues)


def test_numeric_claim_requires_the_same_metric_not_just_the_same_value():
    report = ClaimVerifier().verify("净利润为 100 亿元", _scope(), [_fact(value=100)], [_artifact()], ())
    assert report.status == "blocked"
    assert any(issue.code == "unsupported_numeric_claim" for issue in report.issues)


def test_thousands_separated_claim_is_matched_as_one_value():
    """千分位逗号不得把金额切碎：指标词仍与整个金额同属一条论断。"""
    report = ClaimVerifier().verify("营业收入为 4,108.71 亿元。", _scope(), [_fact(value=4108.71)], [_artifact()], ())

    assert report.status == "passed"
    assert report.supported_fact_ids == ("pdf_p40",)


def test_unsupported_thousands_separated_claim_is_still_blocked():
    report = ClaimVerifier().verify("营业收入为 5,000.00 亿元。", _scope(), [_fact(value=4108.71)], [_artifact()], ())

    assert report.status == "blocked"
    assert ClaimVerifier().degrade_blocked(
        "营业收入为 5,000.00 亿元。", _scope(), [_fact(value=4108.71)],
    ) == SAFE_UNSUPPORTED_CLAIM_TEXT


def test_metric_alias_makes_relevant_conflict_require_disclosure():
    facts = (_fact(value=100), _fact(value=120))
    conflict = FactConflict("revenue", facts, "同指标同期间同口径数值不一致")
    report = ClaimVerifier().verify("营业收入为 100 亿元", _scope(), facts, [_artifact()], [conflict])
    assert report.status == "partial"
    assert any(issue.code == "undisclosed_conflict" for issue in report.issues)


def test_metric_word_is_judged_per_claim_not_per_answer():
    """同值同单位、同篇多指标时，整篇出现的指标词不得让另一端成为受支持。"""
    report = ClaimVerifier().verify(
        "营业收入为 100 亿元，净利润为 100 亿元。", _scope(), [_fact(value=100)], [_artifact()], (),
    )

    assert report.status == "blocked"
    assert any(issue.code == "unsupported_numeric_claim" for issue in report.issues)
    assert report.supported_fact_ids == ("pdf_p40",)


def test_out_of_scope_fact_does_not_support_a_numeric_claim():
    report = ClaimVerifier().verify(
        "长江电力营收为 100 亿元", _scope(), [_fact("600900")], [_artifact("600900")], (),
    )

    assert any(issue.code == "unsupported_numeric_claim" for issue in report.issues)


def test_blocked_degradation_keeps_supported_claims_and_drops_only_unsupported_ones():
    facts = [_fact(value=100)]
    answer = "营业收入为 100 亿元，净利润为 999 亿元。"

    assert ClaimVerifier().verify(answer, _scope(), facts, [_artifact()], ()).status == "blocked"
    degraded = ClaimVerifier().degrade_blocked(answer, _scope(), facts)

    assert "100 亿元" in degraded
    assert "999" not in degraded
    assert SAFE_UNSUPPORTED_CLAIM_TEXT in degraded


def test_blocked_degradation_without_supported_claims_replaces_the_whole_answer():
    answer = "经营活动现金流量净额为 -621.69 亿元，主要受客户贷款及垫款净增加影响。"

    assert ClaimVerifier().degrade_blocked(answer, _scope(), ()) == SAFE_UNSUPPORTED_CLAIM_TEXT


def test_blocked_degradation_keeps_the_scoped_company_name():
    """降级只动数值论断，不改写用户能读到的公司身份与上下文。"""
    facts = [_fact(value=100)]
    answer = "农业银行营业收入为 100 亿元，今日股价为 3.2 元/股。"

    degraded = ClaimVerifier().degrade_blocked(answer, _scope(), facts)

    assert "农业银行" in degraded
    assert "营业收入为 100 亿元" in degraded
    assert "3.2" not in degraded


def test_derived_claim_requires_all_underlying_facts_in_same_run():
    from webapp.chat_facts import derive_growth

    baseline = Fact("revenue", 100, "亿元", "2024-12-31", "annual", "consolidated",
                    "601288", "pdf", ("baseline",), "verified", source_category="local_pdf")
    current = Fact("revenue", 125, "亿元", "2025-12-31", "annual", "consolidated",
                   "601288", "pdf", ("current",), "verified", source_category="local_pdf")
    derived = derive_growth(current, baseline, relation="yoy")
    verifier = ClaimVerifier()

    assert derived is not None
    assert verifier.verify("营收同比增长 25%", _scope(), [current, derived], (), (),
                           answer_intent="financial_trend").status == "blocked"
    assert verifier.verify("营收同比增长 25%", _scope(), [baseline, current, derived], (), (),
                           answer_intent="financial_trend").status == "passed"


def test_forged_derived_value_or_formula_cannot_pass_lineage_validation():
    from dataclasses import replace
    from webapp.chat_facts import derive_growth

    baseline = Fact("revenue", 100, "亿元", "2024-12-31", "annual", "consolidated",
                    "601288", "pdf", ("baseline",), "verified", source_category="local_pdf")
    current = Fact("revenue", 125, "亿元", "2025-12-31", "annual", "consolidated",
                   "601288", "pdf", ("current",), "verified", source_category="local_pdf")
    valid = derive_growth(current, baseline, relation="yoy")
    assert valid is not None
    forged = replace(valid, value=30, id="fact_forged")
    assert ClaimVerifier().verify("营收同比增长 30%", _scope(), [baseline, current, forged], (), (),
                                  answer_intent="financial_trend").status == "blocked"


def test_verifier_parses_percent_percentage_points_index_points_and_counts_separately():
    percent = Fact("revenue", 25, "百分比", "2025-12-31", "annual", "consolidated", "601288", "pdf", ("r1",), "verified", source_category="local_pdf")
    pp = Fact("roe", 0.5, "百分点", "2025-12-31", "annual", "consolidated", "601288", "pdf", ("r2",), "verified", source_category="local_pdf")
    points = Fact("index_close_delta", 30, "点", "2025-10-02", "daily", "index", "sh000001", "tool", ("m1",), "reference", "2025-10-02", source_category="market")
    count = Fact("limit_up_count", 12, "家", "2025-10-02", "daily", "market", "market", "mcp", ("m2",), "reference", "2025-10-02", source_category="mcp")
    verifier = ClaimVerifier()

    assert verifier.verify("营收增长 25％", _scope(), [percent], (), ()).status == "passed"
    assert verifier.verify("ROE提高 0.5 个百分点", _scope(), [pp], (), ()).status == "passed"
    assert verifier.verify("指数上涨 30 点，外部参考", Scope.whole_corpus(), [points], (), ()).status == "passed"
    assert verifier.verify("涨停 12 家，外部参考", Scope.whole_corpus(), [count], (), ()).status == "passed"
    assert verifier.verify("指数上涨 30 点", Scope.whole_corpus(), [percent], (), ()).status == "blocked"


def test_claim_entity_scope_must_match_fact_or_be_unambiguous():
    consolidated = Fact("net_profit", 100, "亿元", "2025-12-31", "annual", "consolidated",
                        "601288", "pdf", ("consolidated",), "verified", source_category="local_pdf")
    parent = Fact("net_profit", 100, "亿元", "2025-12-31", "annual", "parent",
                  "601288", "pdf", ("parent",), "verified", source_category="local_pdf")
    verifier = ClaimVerifier()

    assert verifier.verify("归母净利润为 100 亿元", _scope(), [consolidated], (), ()).status == "blocked"
    assert verifier.verify("净利润为 100 亿元", _scope(), [consolidated, parent], (), ()).status == "blocked"
    assert verifier.verify("合并净利润为 100 亿元", _scope(), [consolidated, parent], (), ()).status == "passed"


def test_verifier_requires_requested_period_and_financial_source_category():
    local = Fact("revenue", 100, "亿元", "2025-12-31", "annual", "consolidated", "601288", "pdf", ("pdf_2025",), "verified", source_category="local_pdf")
    earlier = Fact("revenue", 100, "亿元", "2024-12-31", "annual", "consolidated", "601288", "pdf", ("pdf_2024",), "verified", source_category="local_pdf")
    external = Fact("revenue", 100, "亿元", "2025-12-31", "annual", "consolidated", "601288", "tool", ("mcp_finance",), "reference", "2025-12-31", source_category="mcp")
    verifier = ClaimVerifier()

    assert verifier.verify("营收为 100 亿元", _scope(), [local, earlier], (), ()).status == "blocked"
    assert verifier.verify("营收为 100 亿元", _scope(), [local, earlier], (), (), requested_period="2025年").status == "passed"
    assert verifier.verify("营收为 100 亿元", _scope(), [local, earlier], (), (), requested_period="2024年至2025年").status == "blocked"
    assert verifier.verify("营收为 100 亿元", _scope(), [local], (), (), requested_period="2025-01-01至2025-12-31").status == "passed"
    assert verifier.verify("2024年营收为 100 亿元", _scope(), [local], (), (), requested_period="2025年").status == "blocked"
    blocked = verifier.verify("营收为 100 亿元", _scope(), [external], (), (), requested_period="2025年", answer_intent="financial_trend")
    legacy = verifier.verify("营收为 100 亿元", _scope(), [_fact(value=100)], (), (), requested_period="2025年", answer_intent="financial_trend")
    assert blocked.status == "blocked"
    assert legacy.status == "blocked"
    assert any(issue.code == "unsupported_numeric_claim" for issue in blocked.issues)


def test_rolling_market_window_matches_only_last_actual_trading_days():
    from datetime import date, timedelta
    from webapp.chat_time import MarketWindow

    start = date(2025, 10, 1)
    facts = [Fact("price", 10 + i, "元/股", (start + timedelta(days=i)).isoformat(),
                  "daily", "security", "601288", "tool", (f"bar:{i}",), "reference",
                  (start + timedelta(days=i)).isoformat(), source_category="market") for i in range(6)]
    window = MarketWindow("近五交易日", "rolling_trading_days", None, date(2025, 10, 6), 5)
    verifier = ClaimVerifier()

    assert verifier.verify("股价为 15 元/股，外部参考", _scope(), facts, (), (),
                           window=window, answer_intent="market_trend").status == "passed"
    assert verifier.verify("股价为 10 元/股，外部参考", _scope(), facts, (), (),
                           window=window, answer_intent="market_trend").status == "blocked"


def test_market_recap_verifier_binds_named_index_claim_to_its_source_symbol():
    from webapp.chat_models import Scope

    wrong_index = Fact("index_close", 3500, "点", "2026-10-09", "daily", "index", "sz399001",
                       "tool", ("index:sz399001",), "reference", "2026-10-09", source_category="market")
    report = ClaimVerifier().verify(
        "上证指数在2026-10-09收盘点位为 3500 点，外部参考。",
        Scope.whole_corpus(), [wrong_index], (), (), answer_intent="market_recap",
    )

    assert report.status == "blocked"
    assert any(issue.code == "unsupported_numeric_claim" for issue in report.issues)


def test_verifier_accepts_a_share_breadth_and_weighted_ratio_only_with_matching_market_facts():
    facts = [
        Fact("market_advancing_count", 3000, "家", "2026-10-15", "daily", "market", "ALL_A",
             "tool", ("snapshot:breadth",), "reference", "2026-10-15T15:00:00+08:00", source_category="market"),
        Fact("market_volume_ratio", 1.25, "倍", "2026-10-15", "daily", "market", "ALL_A",
             "tool", ("snapshot:ratio",), "reference", "2026-10-15T15:00:00+08:00", source_category="market"),
    ]

    valid = ClaimVerifier().verify(
        "上涨家数 3,000 家，全 A 加权量比 1.25 倍，外部参考。",
        Scope.whole_corpus(), facts, (), (), answer_intent="market_recap",
    )
    wrong = ClaimVerifier().verify(
        "上涨家数 3,001 家，全 A 加权量比 1.25 倍，外部参考。",
        Scope.whole_corpus(), facts, (), (), answer_intent="market_recap",
    )

    assert valid.status == "passed"
    assert wrong.status == "blocked"


def test_market_long_horizon_verifier_requires_the_selected_kline_grain():
    from datetime import date
    from webapp.chat_time import MarketWindow

    weekly = Fact("price", 10.5, "元/股", "2025-09-30", "weekly", "security", "601288",
                  "tool", ("weekly:2025-09-30",), "reference", "2025-09-30", source_category="market")
    daily = Fact("price", 10.5, "元/股", "2025-09-30", "daily", "security", "601288",
                 "tool", ("daily:2025-09-30",), "reference", "2025-09-30", source_category="market")
    window = MarketWindow("过去5年", "explicit", date(2020, 10, 1), date(2025, 10, 1), None)
    verifier = ClaimVerifier()
    answer = "股价为 10.5 元/股，外部参考。"

    assert verifier.verify(answer, _scope(), [weekly], (), (), window=window,
                           answer_intent="market_trend").status == "passed"
    assert verifier.verify(answer, _scope(), [daily], (), (), window=window,
                           answer_intent="market_trend").status == "blocked"


def test_market_trend_facts_must_match_requested_window_and_degradation_contract():
    from datetime import date
    from webapp.chat_time import MarketWindow

    inside = Fact("price", 10.5, "元/股", "2025-10-02", "daily", "security", "601288", "tool", ("kline:2025-10-02",), "reference", "2025-10-02", source_category="market")
    outside = Fact("price", 10.5, "元/股", "2025-10-08", "daily", "security", "601288", "tool", ("kline:2025-10-08",), "reference", "2025-10-08", source_category="market")
    window = MarketWindow("上周", "explicit", date(2025, 10, 1), date(2025, 10, 3), None)
    verifier = ClaimVerifier()

    assert verifier.verify("股价为 10.5 元/股，外部参考", _scope(), [inside], (), (), window=window, answer_intent="market_trend").status == "passed"
    answer = "股价为 10.5 元/股"
    report = verifier.verify(answer, _scope(), [outside], (), (), window=window, answer_intent="market_trend")
    assert report.status == "blocked"
    assert verifier.degrade_blocked(answer, _scope(), [outside], window=window, answer_intent="market_trend") == SAFE_UNSUPPORTED_CLAIM_TEXT
