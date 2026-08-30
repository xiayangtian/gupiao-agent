import json
from financial_report_fetcher.rag.chunking import (
    parse_pdf_report_id,
    chunk_metrics,
)


def test_parse_pdf_report_id_annual():
    assert parse_pdf_report_id("长江电力_600900_年报_2025.pdf") == "600900:2025-12-31:annual"


def test_parse_pdf_report_id_semi_annual():
    assert parse_pdf_report_id("长江电力_600900_半年报_2025.pdf") == "600900:2025-06-30:semi_annual"


def test_parse_quarterly_filename_preserves_q1_and_q3():
    """完整季度日期必须区分一季报和三季报身份。"""
    assert parse_pdf_report_id("长江电力_600900_季报_2025-03-31.pdf") == "600900:2025-03-31:quarterly"
    assert parse_pdf_report_id("长江电力_600900_季报_2025-09-30.pdf") == "600900:2025-09-30:quarterly"


def test_parse_legacy_quarterly_filename_refuses_to_guess():
    """仅含年份的旧季报文件名不具备可判定的季度身份。"""
    assert parse_pdf_report_id("长江电力_600900_季报_2025.pdf") is None


def test_parse_pdf_report_id_unknown():
    assert parse_pdf_report_id("随便一个文件.txt") is None


def test_chunk_metrics_natural_language():
    metrics = [{"year": 2025, "revenue": 862.42, "net_profit": 345.03, "roe": 15.9, "gross_margin": None, "debt_ratio": None}]
    chunk = chunk_metrics(metrics, "600900:2025-12-31:annual")
    assert chunk is not None
    assert chunk.source == "analysis"
    assert chunk.section == "指标摘要"
    assert "2025年" in chunk.text
    assert "营业收入 862.42亿元" in chunk.text
    assert "归母净利润 345.03亿元" in chunk.text
    assert "毛利率" not in chunk.text  # None 值跳过


def test_chunk_metrics_empty_returns_none():
    assert chunk_metrics([], "x") is None
    assert chunk_metrics(None, "x") is None


def test_chunk_metrics_skips_non_finite_values():
    """旧 JSON 即使含 NaN/Infinity 也不得生成误导性的检索文本。"""
    chunk = chunk_metrics(
        [{"year": 2025, "revenue": float("nan"), "roe": float("inf"), "net_profit": 3.0}],
        "600900:2025-12-31:annual",
    )

    assert chunk is not None
    assert "归母净利润 3亿元" in chunk.text
    assert "nan" not in chunk.text.lower()
    assert "inf" not in chunk.text.lower()


def test_chunk_metrics_has_ticker_meta():
    """指标摘要 chunk 带 ticker，保证按 ticker 过滤的通用问答可检索到"""
    metrics = [{"year": 2025, "revenue": 862.42}]
    chunk = chunk_metrics(metrics, "600900:2025-12-31:annual")
    assert chunk is not None
    assert chunk.meta["ticker"] == "600900"


def test_chunk_pdf_splits_by_sections(monkeypatch, tmp_path):
    """章节感知分块：同一章节内容聚合，跨章节分界"""
    from financial_report_fetcher.rag import chunking

    pdf = tmp_path / "长江电力_600900_年报_2025.pdf"
    pdf.write_bytes(b"fake")
    pages = [
        (1, "第一节 重要提示\n这里是重要提示内容" + "填充" * 300),
        (2, "第二节 公司简介\n这里是公司简介" + "填充" * 300),
        (3, "第三节 管理层讨论与分析\n" + "经营数据填充" * 200),
    ]
    monkeypatch.setattr(chunking, "extract_pdf_pages", lambda path: pages)

    chunks = chunking.chunk_pdf(str(pdf), "600900:2025-12-31:annual", chunk_size=200, overlap=20)
    sections = {c.section for c in chunks}
    assert "第一节 重要提示" in sections
    assert "第三节 管理层讨论与分析" in sections
    for c in chunks:
        assert c.report_id == "600900:2025-12-31:annual"
        assert c.source == "pdf"
        assert c.id  # 派生 id 非空


def test_chunk_analysis_json_dimensions(tmp_path):
    """分析报告按维度切分，超长维度再按小节切"""
    from financial_report_fetcher.rag.chunking import chunk_analysis_json

    path = tmp_path / "长江电力_600900_2025_分析报告.json"
    path.write_text(json.dumps({
        "meta": {"company": "长江电力（600900）", "source_file": "reports/长江电力_600900_年报_2025.pdf"},
        "dimensions": [
            {"id": "financial_summary", "name": "财务摘要", "content": "营业收入 862 亿元。\n净利润 345 亿元。", "error": None},
            {"id": "risk_warning", "name": "风险识别", "content": "### 1. 业绩下滑风险\n现金流下降。", "error": None},
        ],
        "metrics": [{"year": 2025, "revenue": 862.42, "net_profit": 345.03}],
    }, ensure_ascii=False), encoding="utf-8")

    chunks = chunk_analysis_json(str(path), "600900:2025-12-31:annual")
    sections = [c.section for c in chunks]
    assert "财务摘要" in sections
    assert "风险识别" in sections
    assert "指标摘要" in sections
    assert all(c.source == "analysis" for c in chunks)


# ── 审查修复补充测试 ───────────────────────────────────────────

def test_chunk_text_overlap_ge_chunk_size_no_hang():
    """Fix A：overlap>=chunk_size 不挂起，且内容完整"""
    from financial_report_fetcher.rag.chunking import _chunk_text

    text = "段落内容" * 30
    chunks = _chunk_text(text, chunk_size=50, overlap=50)
    assert chunks                                    # 不挂起且有输出
    assert sum(len(c) for c in chunks) >= len(text)  # 总长度覆盖原文
    assert all(c in text for c in chunks)            # 每个 chunk 均为原文片段


def test_chunk_pdf_toc_page_dedup(monkeypatch, tmp_path):
    """目录页（带点号页码）与正文同标题：只生成正文 section，目录页文本不入库"""
    from financial_report_fetcher.rag import chunking

    pdf = tmp_path / "长江电力_600900_年报_2025.pdf"
    pdf.write_bytes(b"fake")
    toc = "目录\n第一节 重要提示..........1\n第二节 公司简介..........2"
    pages = [
        (1, toc),
        (2, "第一节 重要提示\n这里是重要提示内容"),
        (3, "第二节 公司简介\n这里是公司简介"),
    ]
    monkeypatch.setattr(chunking, "extract_pdf_pages", lambda path: pages)

    chunks = chunking.chunk_pdf(str(pdf), "600900:2025-12-31:annual", chunk_size=200, overlap=20)
    sections = {c.section for c in chunks}
    assert sections == {"第一节 重要提示", "第二节 公司简介"}  # 只生成正文 section
    assert all("目录" not in c.text and ".........." not in c.text for c in chunks)
    assert any("这里是重要提示内容" in c.text for c in chunks)


def test_chunk_pdf_multiple_titles_same_page(monkeypatch, tmp_path):
    """同页多章节标题：页内按标题位置切分，内容不错归"""
    from financial_report_fetcher.rag import chunking

    pdf = tmp_path / "长江电力_600900_年报_2025.pdf"
    pdf.write_bytes(b"fake")
    pages = [(1, "第一节 重要提示\n内容A\n第二节 公司简介\n内容B")]
    monkeypatch.setattr(chunking, "extract_pdf_pages", lambda path: pages)

    chunks = chunking.chunk_pdf(str(pdf), "600900:2025-12-31:annual")
    by_section = {c.section: c.text for c in chunks}
    assert "内容A" in by_section.get("第一节 重要提示", "")
    assert "内容B" in by_section.get("第二节 公司简介", "")
    assert "内容B" not in by_section.get("第一节 重要提示", "")
    assert "内容A" not in by_section.get("第二节 公司简介", "")


def test_chunk_pdf_no_section_fallback(monkeypatch, tmp_path):
    """无「第X节」标题：整份报告作为单一「全文」section"""
    from financial_report_fetcher.rag import chunking

    pdf = tmp_path / "长江电力_600900_年报_2025.pdf"
    pdf.write_bytes(b"fake")
    pages = [(1, "公司概况说明"), (2, "经营数据分析")]
    monkeypatch.setattr(chunking, "extract_pdf_pages", lambda path: pages)

    chunks = chunking.chunk_pdf(str(pdf), "600900:2025-12-31:annual")
    assert len(chunks) == 1
    assert chunks[0].section == "全文"
    assert "公司概况说明" in chunks[0].text
    assert "经营数据分析" in chunks[0].text
