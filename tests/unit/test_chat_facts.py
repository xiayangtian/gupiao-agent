from webapp.chat_facts import FactNormalizer, detect_conflicts
from webapp.chat_models import EvidenceArtifact, Scope, ToolArtifact


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
    quarter = normalizer.normalize(raw | {"period": "2026-03-31", "period_kind": "single_quarter"}, _pdf(), _scope())
    half = normalizer.normalize(raw | {"period": "2026-06-30", "period_kind": "semi_annual_cumulative"}, _pdf(), _scope())
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
