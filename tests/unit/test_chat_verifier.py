from webapp.chat_models import EvidenceArtifact, Fact, FactConflict, Scope
from webapp.chat_verifier import ClaimVerifier


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


def test_metric_alias_makes_relevant_conflict_require_disclosure():
    facts = (_fact(value=100), _fact(value=120))
    conflict = FactConflict("revenue", facts, "同指标同期间同口径数值不一致")
    report = ClaimVerifier().verify("营业收入为 100 亿元", _scope(), facts, [_artifact()], [conflict])
    assert report.status == "partial"
    assert any(issue.code == "undisclosed_conflict" for issue in report.issues)
