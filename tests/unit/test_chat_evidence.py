import json

from webapp.chat_evidence import EvidenceNormalizer, pdf_page_url
from webapp.chat_models import ToolArtifact


REPORT_ID = "601288:2026-06-30:semi_annual"
PDF_FILENAME = "农业银行_601288_半年报_2026.pdf"


def _write_analysis(analysis_dir, source_file):
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "农业银行_601288_2026-06-30_分析报告.json").write_text(
        json.dumps(
            {"meta": {"company": "农业银行（601288）", "period": "2026-06-30", "source_file": source_file}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_report(reports_dir):
    reports_dir.mkdir()
    (reports_dir / PDF_FILENAME).write_bytes(b"%PDF-1.4")


def test_pdf_artifact_uses_history_pdf_page_url(tmp_path):
    reports_dir = tmp_path / "reports"
    _write_report(reports_dir)

    artifact = EvidenceNormalizer().normalize_rag_citations(
        [{"report_id": REPORT_ID, "source": "pdf", "page": 40, "snippet": "经营活动现金流"}],
        analysis_dir=str(tmp_path / "analysis"),
        reports_dir=str(reports_dir),
        jump_version=7,
    )[0]

    assert artifact.pdf_filename == PDF_FILENAME
    assert artifact.pdf_url.endswith("?jump=7#page=40")
    assert artifact.availability == "available"


def test_missing_pdf_file_keeps_artifact_but_omits_pdf_url(tmp_path):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    _write_analysis(tmp_path / "analysis", f"reports/{PDF_FILENAME}")

    artifact = EvidenceNormalizer().normalize_rag_citations(
        [{"report_id": REPORT_ID, "source": "pdf", "page": 40, "snippet": "x"}],
        analysis_dir=str(tmp_path / "analysis"),
        reports_dir=str(reports_dir),
        jump_version=7,
    )[0]

    assert artifact.pdf_url is None
    assert artifact.availability == "missing_file"


def test_rag_citations_without_pdf_page_evidence_are_skipped(tmp_path):
    reports_dir = tmp_path / "reports"
    _write_report(reports_dir)

    artifacts = EvidenceNormalizer().normalize_rag_citations(
        [
            {"report_id": REPORT_ID, "source": "analysis", "page": 40, "snippet": "x"},
            {"report_id": REPORT_ID, "source": "pdf", "page": 0, "snippet": "x"},
            {"report_id": REPORT_ID, "source": "pdf", "page": None, "snippet": "x"},
            {"report_id": REPORT_ID, "source": "pdf", "page": 40, "snippet": "  "},
            {"report_id": "600000:2026-06-30:semi_annual", "source": "pdf", "page": 40, "snippet": "x"},
        ],
        analysis_dir=str(tmp_path / "analysis"),
        reports_dir=str(reports_dir),
        jump_version=7,
    )

    assert artifacts == ()


def test_pdf_page_url_quotes_filename_and_rejects_path_separators():
    assert pdf_page_url(PDF_FILENAME, 40, 7) == (
        "/api/history-pdf/%E5%86%9C%E4%B8%9A%E9%93%B6%E8%A1%8C_601288_"
        "%E5%8D%8A%E5%B9%B4%E6%8A%A5_2026.pdf?jump=7#page=40"
    )
    assert pdf_page_url("../private.pdf", 1, 7) is None
    assert pdf_page_url(r"..\private.pdf", 1, 7) is None


def test_external_fact_without_as_of_cannot_be_verified():
    from webapp.chat_models import Fact

    fact = Fact.from_tool(
        metric="price",
        value=3.2,
        unit="元",
        tool=ToolArtifact(provider="market", tool_name="quote", as_of=""),
    )

    assert fact.verification == "unavailable"


def test_unknown_tool_text_stays_reference_artifact_without_fact():
    facts = EvidenceNormalizer().facts_from_structured_tool_payload(
        "不可解析的自由文本",
        ToolArtifact(
            provider="market",
            tool_name="quote",
            as_of="2026-09-10T10:00:00+08:00",
            status="success",
        ),
    )

    assert facts == ()


def test_web_artifacts_only_keep_valid_urls_and_source_times():
    artifacts = EvidenceNormalizer().normalize_web_sources(
        [
            {
                "url": "https://example.com/news",
                "title": "市场新闻",
                "content": "摘要",
                "published_date": "2026-09-10T09:00:00+08:00",
            },
            {"url": "javascript:alert(1)", "title": "unsafe", "content": "x"},
            {"url": "https://example.com/no-content", "title": "无正文", "content": ""},
            {"url": "", "title": "无链接", "content": "x"},
        ],
        "2026-09-10T10:00:00+08:00",
    )

    assert len(artifacts) == 2
    assert artifacts[0].published_at == "2026-09-10T09:00:00+08:00"
    assert artifacts[0].fetched_at == "2026-09-10T10:00:00+08:00"
    assert artifacts[1].snippet == "无正文"
    assert all(artifact.source == "web" for artifact in artifacts)


def test_web_artifacts_require_fetch_time():
    assert EvidenceNormalizer().normalize_web_sources(
        [{"url": "https://example.com/news", "title": "新闻", "content": "摘要"}],
        "  ",
    ) == ()


def test_tool_artifact_redacts_secret_arguments_and_limits_result_summary():
    artifact = EvidenceNormalizer().normalize_tool_event(
        "quote",
        {"code": "601288", "page": 1, "api_key": "do-not-save", "query": "Bearer token=do-not-save 行情", "unknown": "drop-me"},
        "token=do-not-save " + ("detail " * 200),
        provider="market",
        as_of="2026-09-10T10:00:00+08:00",
        ok=True,
    )

    assert json.loads(artifact.arguments_summary) == {
        "code": "601288",
        "page": 1,
        "query": "Bearer token=[已隐藏] 行情",
    }
    assert artifact.status == "success"
    assert "do-not-save" not in artifact.result_summary
    assert len(artifact.result_summary) <= 500


def test_failed_tool_event_keeps_failure_status_without_as_of():
    artifact = EvidenceNormalizer().normalize_tool_event(
        "quote",
        {"code": "601288"},
        "工具调用失败：请求超时",
        provider="market",
        as_of="",
        ok=False,
    )

    assert artifact.status == "failed"
    assert artifact.as_of == ""


def test_structured_tool_payload_requires_complete_timed_controlled_fields():
    normalizer = EvidenceNormalizer()
    artifact = ToolArtifact(
        provider="market",
        tool_name="quote",
        as_of="2026-09-10T10:00:00+08:00",
        status="success",
    )

    facts = normalizer.facts_from_structured_tool_payload(
        json.dumps({
            "metric": "price",
            "value": 3.2,
            "unit": "元",
            "period": "2026-09-10",
            "company_code": "601288",
            "as_of": "2026-09-10T10:00:00+08:00",
        }),
        artifact,
    )

    assert len(facts) == 1
    assert facts[0].verification == "reference"
    assert facts[0].source_type == "tool"
    assert facts[0].as_of == "2026-09-10T10:00:00+08:00"
    assert normalizer.facts_from_structured_tool_payload(
        '{"metric": "price", "value": 3.2}', artifact
    ) == ()
    assert normalizer.facts_from_structured_tool_payload(
        json.dumps({
            "metric": "price",
            "value": "3.2",
            "unit": "元",
            "period": "2026-09-10",
            "company_code": "601288",
            "as_of": "2026-09-10T10:00:00+08:00",
        }),
        artifact,
    ) == ()
    assert normalizer.facts_from_structured_tool_payload(
        json.dumps({
            "metric": "price",
            "value": 3.2,
            "unit": "元",
            "period": "2026-09-10",
            "company_code": "601288",
            "as_of": "",
        }),
        artifact,
    ) == ()
    assert normalizer.facts_from_structured_tool_payload(
        json.dumps({
            "metric": "price",
            "value": 3.2,
            "unit": "元",
            "period": "2026-09-10",
            "company_code": "601288",
            "as_of": "2026-09-10T10:00:00+08:00",
        }),
        ToolArtifact(provider="market", tool_name="quote"),
    ) == ()
