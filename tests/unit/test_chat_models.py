import pytest

from dataclasses import FrozenInstanceError

from webapp.chat_models import (
    AnswerRun,
    ChatMessage,
    CompanyRef,
    EvidenceArtifact,
    Fact,
    IndustryRef,
    Scope,
    SourcePolicy,
    ToolArtifact,
)


def test_scope_company_only_requires_one_company_and_report_ids():
    with pytest.raises(ValueError, match="company_only"):
        Scope(mode="company_only", companies=(), report_ids=())


def test_answer_run_round_trip_keeps_pdf_and_tool_artifacts():
    run = AnswerRun(
        content="经营现金流净额为 12 亿元。",
        status="completed",
        scope=Scope.company_only(
            "601288", "农业银行", ["601288:2026-06-30:semi_annual"]
        ),
        artifacts=(
            EvidenceArtifact.pdf(
                "601288:2026-06-30:semi_annual",
                "农业银行_601288_半年报_2026.pdf",
                40,
                "经营活动现金流量净额",
            ),
        ),
        tool_artifacts=(
            ToolArtifact(
                provider="market",
                tool_name="quote",
                as_of="2026-09-10T10:00:00+08:00",
                status="success",
            ),
        ),
    )
    assert AnswerRun.from_dict(run.to_dict()) == run


def test_legacy_chat_message_is_readable_but_marks_evidence_unavailable():
    message = ChatMessage.from_dict({"role": "assistant", "content": "旧回答"})
    assert message.run is not None
    assert message.run.legacy_evidence_unavailable is True
    assert message.run.artifacts == ()


def test_scope_variants_validate_industry_and_whole_corpus_boundaries():
    industry = IndustryRef(name="银行业", provider="company-profile", sample_kind="local_indexed")
    scope = Scope(
        mode="company_industry",
        companies=(CompanyRef("601288", "农业银行"),),
        report_ids=("601288:2026-06-30:semi_annual", "600000:2026-06-30:semi_annual"),
        industry=industry,
        source_policy=SourcePolicy.local_only(),
    )
    assert Scope.from_dict(scope.to_dict()) == scope
    assert Scope.whole_corpus().report_ids == ()
    with pytest.raises(ValueError, match="company_industry"):
        Scope("company_industry", (CompanyRef("601288", "农业银行"),), (), industry)
    with pytest.raises(ValueError, match="whole_corpus"):
        Scope("whole_corpus", (), ("601288:2026-06-30:semi_annual",))


def test_contracts_validate_artifact_fact_and_status_values():
    with pytest.raises(ValueError, match="positive integer"):
        EvidenceArtifact.pdf("report", "report.pdf", 0, "摘录")
    with pytest.raises(ValueError, match="requires as_of"):
        ToolArtifact(provider="market", tool_name="quote", status="success")
    with pytest.raises(ValueError, match="answer status"):
        AnswerRun(content="x", status="running")
    with pytest.raises(ValueError, match="verification"):
        Fact(
            metric="revenue", value=1.0, unit="亿元", period="2026-06-30",
            period_kind="semi_annual_cumulative", entity_scope="consolidated",
            company_code="601288", source_type="pdf", evidence_ids=("pdf_p1",),
            verification="unknown",
        )


def test_all_contracts_are_json_round_trippable_and_frozen():
    fact = Fact(
        metric="revenue", value=1.0, unit="亿元", period="2026-06-30",
        period_kind="semi_annual_cumulative", entity_scope="consolidated",
        company_code="601288", source_type="pdf", evidence_ids=("pdf_p1",),
        verification="verified",
    )
    web = EvidenceArtifact.web(
        "https://example.com/news", "新闻", "摘要", fetched_at="2026-09-10T10:00:00+08:00"
    )
    assert Fact.from_dict(fact.to_dict()) == fact
    assert EvidenceArtifact.from_dict(web.to_dict()) == web
    assert CompanyRef.from_dict(CompanyRef("601288", "农业银行").to_dict()) == CompanyRef("601288", "农业银行")
    with pytest.raises(FrozenInstanceError):
        fact.metric = "profit"
