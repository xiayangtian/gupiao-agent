import json
from financial_report_fetcher.rag.ingest import IngestionService
from financial_report_fetcher.rag.store import RagStore


def _write_pdf(tmp_path, code="600900", year="2025", kind="年报"):
    pdf = tmp_path / f"长江电力_{code}_{kind}_{year}.pdf"
    pdf.write_bytes(b"%PDF-fake")
    return pdf


def _write_analysis(tmp_path, code="600900", year="2025",
                    source_pdf="reports/长江电力_600900_年报_2025.pdf",
                    revenue=862.42, net_profit=345.03):
    p = tmp_path / f"长江电力_{code}_{year}_分析报告.json"
    p.write_text(json.dumps({
        "meta": {"company": f"长江电力（{code}）", "source_file": source_pdf},
        "dimensions": [{"id": "financial_summary", "name": "财务摘要",
                        "content": f"营业收入{int(revenue)}亿元。", "error": None}],
        "metrics": [{"year": int(year), "revenue": revenue, "net_profit": net_profit}],
    }, ensure_ascii=False), encoding="utf-8")
    return p


def _make_service(tmp_path, store, analysis_dir):
    """构造服务：manifest_path 显式指向 tmp_path，避免写入仓库根 data/rag"""
    return IngestionService(
        store,
        reports_dir=str(tmp_path),
        analysis_dir=analysis_dir,
        manifest_path=str(tmp_path / "manifest.json"),
    )


def test_ingest_all_skips_unknown_files(tmp_path, fake_embedder, monkeypatch):
    """非 PDF / 非分析报告文件不处理"""
    pdf = _write_pdf(tmp_path)
    (tmp_path / "readme.txt").write_text("x")
    monkeypatch.setattr("financial_report_fetcher.rag.chunking.extract_pdf_pages",
                        lambda p: [(1, "第一节 重要提示\n营业收入862亿元")])
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = _make_service(tmp_path, store, str(tmp_path / "analysis"))
    result = svc.ingest_all()
    assert result.ingested == 1
    assert result.total_chunks >= 1
    assert result.errors == []


def test_ingest_pdf_and_analysis_dual_source(tmp_path, fake_embedder, monkeypatch):
    """PDF 与对应分析报告双源入库；analysis chunk 含指标摘要"""
    pdf = _write_pdf(tmp_path)
    _write_analysis(tmp_path, source_pdf=str(pdf))
    monkeypatch.setattr("financial_report_fetcher.rag.chunking.extract_pdf_pages",
                        lambda p: [(1, "第一节 重要提示\n营业收入862亿元")])
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = _make_service(tmp_path, store, str(tmp_path))
    result = svc.ingest_all()
    assert result.total_chunks >= 3  # pdf(≥1) + 财务摘要 + 指标摘要
    sources = {h["source"] for h in store.query("营业收入", top_k=10)}
    assert {"pdf", "analysis"} <= sources


def test_ingest_idempotent(tmp_path, fake_embedder, monkeypatch):
    """重复摄取不增加 chunk；第二次运行全 skipped"""
    pdf = _write_pdf(tmp_path)
    monkeypatch.setattr("financial_report_fetcher.rag.chunking.extract_pdf_pages",
                        lambda p: [(1, "第一节 重要提示\n营业收入862亿元")])
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = _make_service(tmp_path, store, str(tmp_path / "analysis"))
    svc.ingest_all()
    n1 = store.count_chunks()
    result = svc.ingest_all()
    assert result.ingested == 0
    assert result.skipped == 1
    assert store.count_chunks() == n1


def test_ingest_updates_after_content_change(tmp_path, fake_embedder, monkeypatch):
    """报告内容变化后重新摄取：ingested 增加、旧内容不再出现在检索结果"""
    pdf = _write_pdf(tmp_path)
    _write_analysis(tmp_path, source_pdf=str(pdf))
    monkeypatch.setattr("financial_report_fetcher.rag.chunking.extract_pdf_pages",
                        lambda p: [(1, "第一节 重要提示\n营业收入862亿元")])
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = _make_service(tmp_path, store, str(tmp_path))
    svc.ingest_all()

    # 修改 PDF 与 analysis 内容（hash 均变化 → 触发重建）
    pdf.write_bytes(b"%PDF-fake-2")
    monkeypatch.setattr("financial_report_fetcher.rag.chunking.extract_pdf_pages",
                        lambda p: [(1, "第一节 重要提示\n营业收入900亿元")])
    _write_analysis(tmp_path, source_pdf=str(pdf), revenue=900.0)
    result = svc.ingest_all()
    assert result.ingested == 1
    assert result.skipped == 0

    texts = [h["text"] for h in store.query("营业收入900亿元", top_k=10)]
    assert any("900" in t for t in texts)
    assert not any("862" in t for t in texts)


def test_ingest_pdf_persists_manifest(tmp_path, fake_embedder, monkeypatch):
    """单份 ingest_pdf 后 manifest 落盘：重启后 status 可见、增量跳过仍生效"""
    pdf = _write_pdf(tmp_path)
    monkeypatch.setattr("financial_report_fetcher.rag.chunking.extract_pdf_pages",
                        lambda p: [(1, "第一节 重要提示\n营业收入862亿元")])
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = _make_service(tmp_path, store, str(tmp_path / "analysis"))
    svc.ingest_pdf(str(pdf))
    n1 = store.count_chunks()

    # 模拟进程重启：重新构造服务，manifest 应从磁盘加载
    svc2 = _make_service(tmp_path, store, str(tmp_path / "analysis"))
    st = svc2.status()
    assert "600900:2025-12-31:annual" in st["reports"]
    # hash 一致 → 再次单份摄取跳过，不新增 chunk
    svc2.ingest_pdf(str(pdf))
    assert store.count_chunks() == n1


def test_status_reports_manifest(tmp_path, fake_embedder, monkeypatch):
    pdf = _write_pdf(tmp_path)
    monkeypatch.setattr("financial_report_fetcher.rag.chunking.extract_pdf_pages",
                        lambda p: [(1, "第一节 重要提示\n营业收入862亿元")])
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = _make_service(tmp_path, store, str(tmp_path / "analysis"))
    svc.ingest_all()
    st = svc.status()
    assert st["total_chunks"] >= 1
    assert "600900:2025-12-31:annual" in st["reports"]


def test_ingest_file_pdf_only(tmp_path, fake_embedder, monkeypatch):
    """文件级摄取：只摄 PDF 不连带分析报告"""
    pdf = _write_pdf(tmp_path)
    monkeypatch.setattr("financial_report_fetcher.rag.chunking.extract_pdf_pages",
                        lambda p: [(1, "第一节 重要提示\n营业收入862亿元")])
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = IngestionService(store, reports_dir=str(tmp_path),
                           analysis_dir=str(tmp_path / "analysis"),
                           manifest_path=str(tmp_path / "rag" / "manifest.json"))
    svc.ingest_file("600900:2025-12-31:annual", "pdf", file_path=str(pdf))
    assert store.count_chunks() >= 1
    # 再次摄取幂等
    svc.ingest_file("600900:2025-12-31:annual", "pdf", file_path=str(pdf))
    n = store.count_chunks()
    svc.ingest_file("600900:2025-12-31:annual", "pdf", file_path=str(pdf))
    assert store.count_chunks() == n


def test_delete_file_index_restores_unadded(tmp_path, fake_embedder, monkeypatch):
    """删除单文件索引后 list_files 状态恢复未添加"""
    pdf = _write_pdf(tmp_path)
    monkeypatch.setattr("financial_report_fetcher.rag.chunking.extract_pdf_pages",
                        lambda p: [(1, "第一节 重要提示\n营业收入862亿元")])
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = IngestionService(store, reports_dir=str(tmp_path),
                           analysis_dir=str(tmp_path / "analysis"),
                           manifest_path=str(tmp_path / "rag" / "manifest.json"))
    svc.ingest_file("600900:2025-12-31:annual", "pdf", file_path=str(pdf))
    entries = {e["source"]: e for e in svc.list_files() if e["report_id"] == "600900:2025-12-31:annual"}
    assert entries["pdf"]["added"] is True
    svc.delete_file_index("600900:2025-12-31:annual", "pdf")
    assert store.count_chunks() == 0
    entries = {e["source"]: e for e in svc.list_files() if e["report_id"] == "600900:2025-12-31:annual"}
    assert entries["pdf"]["added"] is False


def test_list_files_classifies(tmp_path, fake_embedder):
    """文件清单按报告类型分类，分析报告条目归属其报告期"""
    _write_pdf(tmp_path)
    _write_analysis(tmp_path)
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = IngestionService(store, reports_dir=str(tmp_path),
                           analysis_dir=str(tmp_path),
                           manifest_path=str(tmp_path / "rag" / "manifest.json"))
    files = svc.list_files()
    by_key = {(f["report_id"], f["source"]): f for f in files}
    assert ("600900:2025-12-31:annual", "pdf") in by_key
    assert ("600900:2025-12-31:annual", "analysis") in by_key
    assert by_key[("600900:2025-12-31:annual", "pdf")]["type_label"] == "年报"
    assert by_key[("600900:2025-12-31:annual", "analysis")]["added"] is False


def test_auto_ingest_pdf_skips_when_disabled(tmp_path, fake_embedder, monkeypatch):
    """auto_ingest=False 时不自动摄取"""
    pdf = _write_pdf(tmp_path)
    monkeypatch.setattr("financial_report_fetcher.rag.chunking.extract_pdf_pages",
                        lambda p: [(1, "第一节 重要提示\n营业收入862亿元")])
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = IngestionService(store, reports_dir=str(tmp_path),
                           analysis_dir=str(tmp_path / "analysis"),
                           manifest_path=str(tmp_path / "rag" / "manifest.json"),
                           auto_ingest=False)
    svc.auto_ingest_pdf(str(pdf))
    assert store.count_chunks() == 0


def test_ingest_file_migrates_legacy_manifest(tmp_path, fake_embedder, monkeypatch):
    """Task 4 旧 manifest（仅 chunks）在 ingest_file 后补齐分 source 字段，状态与库一致"""
    rid = "600900:2025-12-31:annual"
    pdf = _write_pdf(tmp_path)
    monkeypatch.setattr("financial_report_fetcher.rag.chunking.extract_pdf_pages",
                        lambda p: [(1, "第一节 重要提示\n营业收入862亿元")])

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        rid: {"pdf_hash": "legacy", "analysis_hash": None,
              "chunks": 5, "updated_at": "2026-01-01T00:00:00"},
    }, ensure_ascii=False), encoding="utf-8")

    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = IngestionService(store, reports_dir=str(tmp_path),
                           analysis_dir=str(tmp_path / "analysis"),
                           manifest_path=str(manifest_path))
    svc.ingest_file(rid, "pdf", file_path=str(pdf))

    assert svc.status()["reports"][rid]["chunks"] == store.count_chunks(rid)
    assert svc.status()["reports"][rid]["chunks"] >= 1


def test_auto_ingest_report_persists_manifest(tmp_path, fake_embedder, monkeypatch):
    """auto_ingest_report 后 manifest 落盘：模拟重启后 status 可见，再次摄取不重复"""
    pdf = _write_pdf(tmp_path)
    _write_analysis(tmp_path, source_pdf=str(pdf))
    monkeypatch.setattr("financial_report_fetcher.rag.chunking.extract_pdf_pages",
                        lambda p: [(1, "第一节 重要提示\n营业收入862亿元")])
    rid = "600900:2025-12-31:annual"
    manifest_path = tmp_path / "manifest.json"
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = IngestionService(store, reports_dir=str(tmp_path),
                           analysis_dir=str(tmp_path),
                           manifest_path=str(manifest_path),
                           auto_ingest=True)
    svc.auto_ingest_report(str(pdf))

    # manifest 已落盘（否则进程重启后记录丢失）
    assert manifest_path.exists()
    assert rid in json.loads(manifest_path.read_text(encoding="utf-8"))

    # 模拟重启：重新构造 service（同一 sqlite 与 manifest），status 仍可见该报告
    store2 = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc2 = IngestionService(store2, reports_dir=str(tmp_path),
                            analysis_dir=str(tmp_path),
                            manifest_path=str(manifest_path),
                            auto_ingest=True)
    assert rid in svc2.status()["reports"]
    assert svc2.status()["reports"][rid]["chunks"] >= 1

    # 再次自动摄取：内容未变化，幂等跳过，不重复入库
    n = store2.count_chunks(rid)
    svc2.auto_ingest_report(str(pdf))
    assert store2.count_chunks(rid) == n
