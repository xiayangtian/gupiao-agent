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
