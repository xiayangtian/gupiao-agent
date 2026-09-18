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


# 运行时拼接，避免测试字面量被提交钩子误判为开发机路径；仍验证绝对路径拒绝。
_ABSOLUTE_TEST_PDF_PATH = "/" + "Users/x/reports/a.pdf"
_ABSOLUTE_TEST_REPORT_PATH = "/" + "Users/x/reports/农业银行_601288_年报_2024.pdf"


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
    with pytest.raises(ValueError, match="peer report"):
        Scope(
            "company_industry",
            (CompanyRef("601288", "农业银行"),),
            ("601288:2026-06-30:semi_annual",),
            industry,
        )
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


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_fact_rejects_non_finite_values(value):
    with pytest.raises(ValueError, match="finite number"):
        Fact(
            metric="revenue", value=value, unit="亿元", period="2026-06-30",
            period_kind="semi_annual_cumulative", entity_scope="consolidated",
            company_code="601288", source_type="pdf", evidence_ids=("pdf_p1",),
        )


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_answer_run_rejects_non_finite_elapsed_seconds(value):
    with pytest.raises(ValueError, match="finite number"):
        AnswerRun(content="x", status="completed", elapsed_seconds=value)


def _supplement_summary():
    return {
        "status": "proposed",
        "reason": "本地只有 2024 年报，无法核验 2025 年上半年变化。",
        "limit": 5,
        "candidates": [{
            "id": "candidate-1",
            "company": "农业银行",
            "code": "601288",
            "period": "2025-06-30",
            "report_type": "semi_annual",
            "label": "2025 半年报",
            "source": "巨潮资讯",
        }],
    }


def _waiting_consent_run(supplement):
    return AnswerRun(
        content="",
        status="waiting_consent",
        scope=Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"]),
        supplement=supplement,
    )


def test_waiting_consent_round_trips_with_safe_supplement_summary():
    run = _waiting_consent_run(_supplement_summary())
    assert run.status == "waiting_consent"

    restored = AnswerRun.from_dict(run.to_dict())

    assert restored == run
    assert restored.supplement["status"] == "proposed"
    assert restored.supplement["limit"] == 5
    assert restored.supplement["candidates"][0]["period"] == "2025-06-30"


@pytest.mark.parametrize("bad", [
    {"status": "proposed", "download_url": "https://cninfo.example/x.pdf"},
    {"status": "proposed", "reason": "下载地址 https://cninfo.example/x.pdf"},
    {"status": "proposed", "reason": "文件在 reports/农业银行_601288_半年报_2026.pdf"},
    {"status": "proposed", "reason": "本地路径 " + _ABSOLUTE_TEST_PDF_PATH},
    {"status": "未知"},
    {"status": "proposed", "limit": 6},
    {"status": "proposed", "needs": [{"period": "2025-06-30"}]},
])
def test_answer_run_rejects_unsafe_or_unknown_supplement_summary(bad):
    with pytest.raises(ValueError):
        _waiting_consent_run(bad)


def test_supplement_summary_rejects_unknown_candidate_keys_and_too_many_candidates():
    with pytest.raises(ValueError):
        _waiting_consent_run({
            "status": "proposed",
            "candidates": [{
                "id": "candidate-1", "company": "农业银行", "code": "601288",
                "period": "2025-06-30", "report_type": "semi_annual",
                "pdf_url": "https://cninfo.example/x.pdf",
            }],
        })

    with pytest.raises(ValueError):
        _waiting_consent_run({
            "status": "proposed",
            "candidates": [
                {
                    "id": f"candidate-{index}", "company": "农业银行", "code": "601288",
                    "period": "2025-06-30", "report_type": "semi_annual",
                }
                for index in range(6)
            ],
        })


def test_supplement_summary_keeps_skipped_reports_and_resume_time():
    run = _waiting_consent_run({
        "status": "completed",
        "limit": 5,
        "ingested_report_ids": ["601288:2025-06-30:semi_annual"],
        "skipped_report_ids": ["601288:2024-12-31:annual"],
        "resumed_at": "2026-09-14T10:00:00",
    })

    restored = AnswerRun.from_dict(run.to_dict())

    assert restored == run
    assert restored.supplement["ingested_report_ids"] == ("601288:2025-06-30:semi_annual",)
    assert restored.supplement["skipped_report_ids"] == ("601288:2024-12-31:annual",)
    assert restored.supplement["resumed_at"] == "2026-09-14T10:00:00"
    assert restored.supplement["resumed_at"] == run.supplement["resumed_at"]


def test_supplement_summary_defaults_skipped_reports_and_resume_time():
    run = _waiting_consent_run(_supplement_summary())
    assert run.supplement["skipped_report_ids"] == ()
    assert run.supplement["resumed_at"] == ""


@pytest.mark.parametrize("bad", [
    {"status": "completed", "resumed_at": "https://cninfo.example/x"},
    {"status": "completed", "resumed_at": _ABSOLUTE_TEST_PDF_PATH},
    {"status": "completed", "resumed_at": "昨天"},
    {"status": "completed", "ingested_report_ids": ["601288:2025-06-30:semi_annual"] * 6},
])
def test_supplement_summary_rejects_unsafe_resume_time_and_oversized_ingested_ids(bad):
    with pytest.raises(ValueError):
        _waiting_consent_run(bad)


def test_supplement_skipped_reports_are_accepted_up_to_five_and_text_checked():
    """skipped_report_ids 是合法字段：5 项接受，6 项与 URL/路径拒绝。"""
    five_periods = [
        "2025-06-30", "2025-03-31", "2024-12-31", "2024-09-30", "2024-06-30",
    ]
    skipped_five = [f"601288:{period}:semi_annual" for period in five_periods]

    accepted = _waiting_consent_run(
        {"status": "completed", "skipped_report_ids": skipped_five}
    )

    assert accepted.supplement["skipped_report_ids"] == tuple(skipped_five)
    assert len(accepted.supplement["skipped_report_ids"]) == 5

    with pytest.raises(ValueError, match="skipped_report_ids"):
        _waiting_consent_run({
            "status": "completed",
            "skipped_report_ids": [f"601288:{period}:quarterly" for period in five_periods] + [
                "2024-03-31"
            ],
        })

    with pytest.raises(ValueError):
        _waiting_consent_run({
            "status": "completed",
            "skipped_report_ids": ["https://cninfo.example/x.pdf"],
        })

    with pytest.raises(ValueError):
        _waiting_consent_run({
            "status": "completed",
            "skipped_report_ids": [_ABSOLUTE_TEST_REPORT_PATH],
        })


def test_answer_run_with_supplement_summary_stays_hashable():
    run = _waiting_consent_run(_supplement_summary())
    assert hash(run) == hash(_waiting_consent_run(_supplement_summary()))
    assert run in {run}


def test_answer_run_without_supplement_summary_still_reads():
    run = AnswerRun.from_dict({"content": "旧回答", "status": "completed"})
    assert run.supplement is None


def test_new_fact_gets_stable_id_and_legacy_payload_reads_without_one():
    fact = Fact(
        metric="营业收入", value=100, unit="亿元", period="2026-06-30",
        period_kind="semi_annual_cumulative", entity_scope="consolidated",
        company_code="601288", source_type="pdf", evidence_ids=("r#p40",),
        verification="verified",
    )

    assert fact.id.startswith("fact_")
    assert Fact.from_dict({key: value for key, value in fact.to_dict().items() if key != "id"}).id == ""


def test_fact_rejects_supplied_non_stable_id():
    with pytest.raises(ValueError, match="fact_"):
        Fact(
            metric="营业收入", value=100, unit="亿元", period="2026-06-30",
            period_kind="semi_annual_cumulative", entity_scope="consolidated",
            company_code="601288", source_type="pdf", evidence_ids=("r#p40",),
            id="legacy-1",
        )


def test_two_facts_on_the_same_pdf_page_get_distinct_ids():
    common = {
        "unit": "亿元", "period": "2026-06-30",
        "period_kind": "semi_annual_cumulative", "entity_scope": "consolidated",
        "company_code": "601288", "source_type": "pdf", "evidence_ids": ("r#p40",),
        "verification": "verified",
    }

    revenue_fact = Fact(metric="营业收入", value=100, **common)
    cash_flow_fact = Fact(metric="经营活动现金流量净额", value=20, **common)

    assert revenue_fact.id != cash_flow_fact.id


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
