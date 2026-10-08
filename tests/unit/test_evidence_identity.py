from webapp.chat_models import AnswerRun, EvidenceArtifact, Fact
from webapp.evidence_identity import (
    artifact_evidence_ids,
    fact_is_backed_by_pdf,
    make_fact_id,
    pdf_evidence_index,
    validated_pdf_url,
)


def _pdf(*, page=40, pdf_url="/api/history-pdf/601288-2026.pdf#page=40"):
    return EvidenceArtifact.pdf(
        "601288:2026-06-30:semi_annual",
        "601288-2026.pdf",
        page,
        "营业收入见原文披露",
        pdf_url=pdf_url,
    )


def _verified_fact(evidence_ids):
    return Fact(
        metric="营业收入",
        value=100,
        unit="亿元",
        period="2026-06-30",
        period_kind="semi_annual_cumulative",
        entity_scope="consolidated",
        company_code="601288",
        source_type="pdf",
        evidence_ids=evidence_ids,
        verification="verified",
    )


def test_make_fact_id_is_deterministic_when_evidence_ids_are_reordered():
    first = make_fact_id(
        "营业收入", 100, "亿元", "2026-06-30", "semi_annual_cumulative",
        "consolidated", "601288", ("r#p40", "r#p41"),
    )
    second = make_fact_id(
        "营业收入", 100, "亿元", "2026-06-30", "semi_annual_cumulative",
        "consolidated", "601288", ("r#p41", "r#p40"),
    )

    assert first == second
    assert first.startswith("fact_")


def test_evidence_identity_rejects_untrusted_pdf_url_and_maps_positive_page_only():
    untrusted_artifact = _pdf(pdf_url="https://untrusted.example/report.pdf#page=40")
    verified_artifact = _pdf()
    verified_fact = _verified_fact(("601288:2026-06-30:semi_annual#p40",))
    run_with_positive_page = AnswerRun(
        content="",
        status="completed",
        facts=(verified_fact,),
        artifacts=(verified_artifact,),
    )

    assert validated_pdf_url(untrusted_artifact) is None
    assert fact_is_backed_by_pdf(verified_fact, run_with_positive_page) is True
    assert pdf_evidence_index(run_with_positive_page)["601288:2026-06-30:semi_annual#p40"] == verified_artifact


def test_derived_pdf_fact_is_backed_only_when_every_parent_resolves_to_pdf():
    from webapp.chat_facts import derive_growth

    baseline = Fact("revenue", 100, "亿元", "2024-12-31", "annual", "consolidated", "601288",
                    "pdf", ("601288:2024-12-31:annual#p1",), "verified", source_category="local_pdf")
    current = Fact("revenue", 125, "亿元", "2025-12-31", "annual", "consolidated", "601288",
                   "pdf", ("601288:2025-12-31:annual#p2",), "verified", source_category="local_pdf")
    derived = derive_growth(current, baseline, relation="yoy")
    assert derived is not None
    run = AnswerRun(
        content="", status="completed", facts=(baseline, current, derived),
        artifacts=(
            EvidenceArtifact.pdf("601288:2024-12-31:annual", "baseline.pdf", 1, "revenue"),
            EvidenceArtifact.pdf("601288:2025-12-31:annual", "current.pdf", 2, "revenue"),
        ),
    )

    assert fact_is_backed_by_pdf(derived, run)
    missing_parent = AnswerRun(content="", status="completed", facts=(derived, current), artifacts=run.artifacts)
    assert not fact_is_backed_by_pdf(derived, missing_parent)


def test_artifact_evidence_ids_exclude_untrusted_pdf_urls_and_keep_web_url_identity():
    untrusted_pdf = _pdf(pdf_url="https://untrusted.example/report.pdf#page=40")
    web = EvidenceArtifact.web(
        "https://example.com/news", "公告", "摘要", fetched_at="2026-09-10T10:00:00+08:00"
    )

    assert artifact_evidence_ids(untrusted_pdf) == (
        "601288:2026-06-30:semi_annual#p40", "601288-2026.pdf"
    )
    assert artifact_evidence_ids(web) == ("https://example.com/news",)
