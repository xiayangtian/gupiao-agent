import json
import os

import pytest

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


def _write_quarterly_pdf(tmp_path, period):
    pdf = tmp_path / f"长江电力_600900_季报_{period}.pdf"
    pdf.write_bytes(b"%PDF-fake")
    return pdf


def _write_quarterly_analysis(tmp_path, period, source_pdf):
    analysis = tmp_path / f"长江电力_600900_{period}_分析报告.json"
    analysis.write_text(json.dumps({
        "meta": {
            "company": "长江电力（600900）",
            "period": period,
            "source_file": str(source_pdf),
        },
        "dimensions": [{
            "id": "financial_summary",
            "name": "财务摘要",
            "content": f"{period} 专属分析内容",
            "error": None,
        }],
        "metrics": [],
    }, ensure_ascii=False), encoding="utf-8")
    return analysis


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


def test_ingest_q1_and_q3_associate_their_own_analysis_files(tmp_path, fake_embedder, monkeypatch):
    """同年 Q1/Q3 摄取时，分析 JSON 只能关联对应的精确 report_id。"""
    q1_pdf = _write_quarterly_pdf(tmp_path, "2025-03-31")
    q3_pdf = _write_quarterly_pdf(tmp_path, "2025-09-30")
    _write_quarterly_analysis(tmp_path, "2025-03-31", q1_pdf)
    _write_quarterly_analysis(tmp_path, "2025-09-30", q3_pdf)
    monkeypatch.setattr(
        "financial_report_fetcher.rag.chunking.extract_pdf_pages",
        lambda p: [(1, "第一节 重要提示\n季度报告内容")],
    )
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    svc = _make_service(tmp_path, store, str(tmp_path))
    svc.ingest_all()

    q1_rid = "600900:2025-03-31:quarterly"
    q3_rid = "600900:2025-09-30:quarterly"
    q1_texts = [hit["text"] for hit in store.query("专属分析内容", top_k=10, where={"report_id": q1_rid})]
    q3_texts = [hit["text"] for hit in store.query("专属分析内容", top_k=10, where={"report_id": q3_rid})]
    assert any("2025-03-31" in text for text in q1_texts)
    assert not any("2025-09-30" in text for text in q1_texts)
    assert any("2025-09-30" in text for text in q3_texts)
    assert not any("2025-03-31" in text for text in q3_texts)


def test_list_files_rejects_ambiguous_legacy_quarter_analysis_but_keeps_annual(
    tmp_path, fake_embedder
):
    """RAG 文件清单不得把旧季报分析 JSON 猜成同年年报。"""
    annual = tmp_path / "A_600900_2025_分析报告.json"
    legacy_quarter = tmp_path / "Z_600900_2025_分析报告.json"
    annual.write_text(json.dumps({
        "meta": {"source_file": "reports/长江电力_600900_年报_2025.pdf"},
        "dimensions": [],
    }, ensure_ascii=False), encoding="utf-8")
    legacy_quarter.write_text(json.dumps({
        "meta": {"source_file": "reports/长江电力_600900_季报_2025.pdf"},
        "dimensions": [],
    }, ensure_ascii=False), encoding="utf-8")
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    service = _make_service(tmp_path, store, str(tmp_path))

    analysis_files = [
        item for item in service.list_files() if item["source"] == "analysis"
    ]

    assert [(item["report_id"], item["filename"]) for item in analysis_files] == [
        ("600900:2025-12-31:annual", annual.name),
    ]


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


def test_legacy_identity_migration_removes_only_quarterly_indexes(tmp_path, fake_embedder):
    """无版本 manifest 启动时失效季度索引，保留年报索引和所有 PDF。"""
    manifest_path = tmp_path / "manifest.json"
    annual_id = "600900:2025-12-31:annual"
    quarterly_ids = [
        "600900:2025-03-31:quarterly",
        "600900:2025-09-30:quarterly",
    ]
    manifest_path.write_text(json.dumps({
        annual_id: {"pdf_hash": "annual"},
        quarterly_ids[0]: {"pdf_hash": "old-q1"},
        quarterly_ids[1]: {"pdf_hash": "old-q3"},
    }, ensure_ascii=False), encoding="utf-8")
    old_quarterly_pdf = tmp_path / "长江电力_600900_季报_2025.pdf"
    annual_pdf = _write_pdf(tmp_path)
    old_quarterly_pdf.write_bytes(b"%PDF-old-quarterly")

    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    from financial_report_fetcher.rag.chunking import Chunk
    store.upsert([
        Chunk(annual_id, "pdf", "年报", "全文", 1, 0),
        Chunk(quarterly_ids[0], "pdf", "旧 Q1", "全文", 1, 0),
        Chunk(quarterly_ids[1], "pdf", "旧 Q3", "全文", 1, 0),
    ])

    service = IngestionService(store, manifest_path=str(manifest_path))

    assert store.count_chunks(quarterly_ids[0]) == 0
    assert store.count_chunks(quarterly_ids[1]) == 0
    assert store.count_chunks(annual_id) == 1
    assert old_quarterly_pdf.exists()
    assert annual_pdf.exists()
    saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert quarterly_ids[0] not in saved_manifest
    assert quarterly_ids[1] not in saved_manifest
    assert annual_id in saved_manifest
    assert service.status()["identity_version"] == 2
    assert service.status()["warnings"]


def test_legacy_identity_migration_supports_basename_only_manifest_path(
        tmp_path, fake_embedder, monkeypatch):
    """basename-only manifest 路径也必须完成旧季度索引迁移。"""
    monkeypatch.chdir(tmp_path)
    quarterly_id = "600900:2025-03-31:quarterly"
    annual_id = "600900:2025-12-31:annual"
    (tmp_path / "manifest.json").write_text(json.dumps({
        quarterly_id: {"pdf_hash": "old-q1"},
        annual_id: {"pdf_hash": "annual"},
    }, ensure_ascii=False), encoding="utf-8")
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    from financial_report_fetcher.rag.chunking import Chunk
    store.upsert([
        Chunk(quarterly_id, "pdf", "旧 Q1", "全文", 1, 0),
        Chunk(annual_id, "pdf", "年报", "全文", 1, 0),
    ])

    service = IngestionService(store, manifest_path="manifest.json")

    assert store.count_chunks(quarterly_id) == 0
    assert store.count_chunks(annual_id) == 1
    assert service.status()["identity_version"] == 2
    assert quarterly_id not in json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )


def test_current_identity_migration_keeps_exact_quarterly_indexes(tmp_path, fake_embedder):
    """版本 2 的精确 Q1/Q3 身份已可信，初始化不可删除其向量。"""
    manifest_path = tmp_path / "manifest.json"
    quarterly_ids = [
        "600900:2025-03-31:quarterly",
        "600900:2025-09-30:quarterly",
    ]
    manifest_path.write_text(json.dumps({
        "__identity_version__": 2,
        quarterly_ids[0]: {"pdf_hash": "q1"},
        quarterly_ids[1]: {"pdf_hash": "q3"},
    }, ensure_ascii=False), encoding="utf-8")
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    from financial_report_fetcher.rag.chunking import Chunk
    store.upsert([
        Chunk(quarterly_ids[0], "pdf", "Q1", "全文", 1, 0),
        Chunk(quarterly_ids[1], "pdf", "Q3", "全文", 1, 0),
    ])

    service = IngestionService(store, manifest_path=str(manifest_path))

    assert store.count_chunks(quarterly_ids[0]) == 1
    assert store.count_chunks(quarterly_ids[1]) == 1
    assert service.status()["identity_version"] == 2


def test_corrupt_manifest_fails_closed_without_deleting_quarterly_vectors(
    tmp_path, fake_embedder
):
    """损坏 manifest 不能被当成空旧版本并触发季度向量删除。"""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{not-valid-json", encoding="utf-8")
    quarterly_ids = [
        "600900:2025-03-31:quarterly",
        "600900:2025-09-30:quarterly",
    ]
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    from financial_report_fetcher.rag.chunking import Chunk
    store.upsert([
        Chunk(quarterly_ids[0], "pdf", "Q1", "全文", 1, 0),
        Chunk(quarterly_ids[1], "pdf", "Q3", "全文", 1, 0),
    ])

    error = None
    try:
        IngestionService(store, manifest_path=str(manifest_path))
    except RuntimeError as exc:
        error = exc

    assert [store.count_chunks(rid) for rid in quarterly_ids] == [1, 1]
    assert error is not None
    assert "manifest" in str(error).lower()


def test_manifest_upgrade_replaces_complete_file_from_same_directory(
    tmp_path, fake_embedder, monkeypatch
):
    """替换发生前旧 manifest 保持完整，且替换源临时文件与目标同目录。"""
    manifest_path = tmp_path / "manifest.json"
    old_manifest = {"600900:2025-12-31:annual": {"pdf_hash": "old"}}
    manifest_path.write_text(json.dumps(old_manifest), encoding="utf-8")
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    real_replace = os.replace
    replacements = []

    def observe_replace(source, target):
        assert os.path.dirname(source) == os.path.dirname(target) == str(tmp_path)
        assert json.loads(manifest_path.read_text(encoding="utf-8")) == old_manifest
        replacements.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(
        "financial_report_fetcher.rag.ingest.os.replace", observe_replace
    )

    service = IngestionService(store, manifest_path=str(manifest_path))

    assert len(replacements) == 1
    assert service.status()["identity_version"] == 2
    assert json.loads(manifest_path.read_text(encoding="utf-8"))[
        "__identity_version__"
    ] == 2


def test_manifest_replace_failure_preserves_old_file_and_cleans_temp(
    tmp_path, fake_embedder, monkeypatch
):
    """原子替换失败时不得破坏旧 manifest，也不得遗留临时文件。"""
    manifest_path = tmp_path / "manifest.json"
    old_text = json.dumps({"600900:2025-12-31:annual": {"pdf_hash": "old"}})
    manifest_path.write_text(old_text, encoding="utf-8")
    store = RagStore(str(tmp_path / "rag"), fake_embedder)
    names_before = {path.name for path in tmp_path.iterdir()}

    def fail_replace(source, target):
        raise OSError("replace blocked")

    monkeypatch.setattr("financial_report_fetcher.rag.ingest.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace blocked"):
        IngestionService(store, manifest_path=str(manifest_path))

    assert manifest_path.read_text(encoding="utf-8") == old_text
    assert {path.name for path in tmp_path.iterdir()} == names_before


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
