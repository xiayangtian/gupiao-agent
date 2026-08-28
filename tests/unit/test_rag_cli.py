"""CLI rag 子命令测试：ingest / status（chat 为交互式，不覆盖）。

遵循全局约束：全部使用 fake 组件，不初始化真实 ChromaDB / embedding / 网络。
"""

import pytest
from types import SimpleNamespace

from financial_report_fetcher import __main__ as main


def _enable_rag(monkeypatch):
    """让 _build_rag_components 走「已启用」路径，且全部使用 fake 组件。

    简报测试代码只替换了 IngestionService；而仓库 config.yaml 无 rag 段
    （RagConfig.load() 默认 disabled），cmd_rag 会先打印「RAG 未启用」并
    sys.exit(1)，导致断言走不到 ingest/status 分支；同时真实 RagStore 会
    初始化 ChromaDB，违反「测试用 fake 组件」的全局约束。这里统一补齐：
    RagConfig.load → enabled 配置；LocalEmbedder / RagStore → fake 类。
    """
    cfg = SimpleNamespace(
        enabled=True,
        store_path="data/rag",
        chunk_size=800,
        chunk_overlap=100,
        top_k=8,
        embedding_model="fake-model",
        auto_ingest=True,
    )

    class FakeLocalEmbedder:
        def __init__(self, *a, **kw):
            pass

    class FakeRagStore:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(main.RagConfig, "load", lambda: cfg)
    monkeypatch.setattr(main, "LocalEmbedder", FakeLocalEmbedder)
    monkeypatch.setattr(main, "RagStore", FakeRagStore)


def test_cmd_rag_ingest_all(monkeypatch, tmp_path, fake_embedder):
    """rag ingest --all 调用 IngestionService.ingest_all 并打印结果"""
    calls = {}

    class FakeSvc:
        def __init__(self, *a, **kw):
            pass

        def ingest_all(self, force=False):
            calls["force"] = force
            from financial_report_fetcher.rag.ingest import IngestResult
            return IngestResult(ingested=1, skipped=0, total_chunks=10)

    _enable_rag(monkeypatch)
    monkeypatch.setattr(main, "IngestionService", FakeSvc)
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(a))
    args = main.argparse.Namespace(command="rag", rag_action="ingest", all=True,
                                   pdf=None, force=False)
    main.cmd_rag(args)
    assert calls == {"force": False}
    assert any("10" in str(x) for x in out)


def test_cmd_rag_ingest_pdf_requires_file(monkeypatch, capsys):
    """rag ingest --pdf 指向不存在的文件时给出错误并退出码非 0"""
    _enable_rag(monkeypatch)

    class FakeSvc:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(main, "IngestionService", FakeSvc)
    args = main.argparse.Namespace(command="rag", rag_action="ingest", all=False,
                                   pdf="/nonexistent/x.pdf", force=False)
    with pytest.raises(SystemExit):
        main.cmd_rag(args)
    assert "不存在" in capsys.readouterr().out


def test_cmd_rag_status(monkeypatch, capsys):
    """rag status 输出报告数与 chunk 数"""
    class FakeSvc:
        def __init__(self, *a, **kw):
            pass

        def status(self):
            return {"reports": {"600900:2025-12-31:annual": {"chunks": 5}},
                    "total_chunks": 5}

    _enable_rag(monkeypatch)
    monkeypatch.setattr(main, "IngestionService", FakeSvc)
    args = main.argparse.Namespace(command="rag", rag_action="status")
    main.cmd_rag(args)
    assert "5" in capsys.readouterr().out


def test_cmd_download_auto_ingests_new_pdfs(monkeypatch, tmp_path):
    """下载完成后仅对新增 PDF 自动摄取（差集）"""
    ingested = []

    class FakeSvc:
        def auto_ingest_pdf(self, path):
            ingested.append(path)

    # 下载前已存在的旧 PDF（不应被摄取）
    (tmp_path / "旧公司_000001_年报_2024.pdf").write_bytes(b"%PDF")

    class FakeDownloader:
        def download_all(self, reports, storage_dir):
            # 模拟本次新下载一份 PDF
            (tmp_path / "长江电力_600900_年报_2025.pdf").write_bytes(b"%PDF")
            from financial_report_fetcher.models import DownloadSummary
            return DownloadSummary(success=1)

    class FakeDS:
        def build_known_companies(self):
            return {}

        def resolve_company(self, ticker, name):
            return None

        def fetch_reports(self, **kw):
            return []

    monkeypatch.setattr(main, "_build_rag_components",
                        lambda: (type("Cfg", (), {"auto_ingest": True})(), FakeSvc(), None))
    monkeypatch.setattr(main, "CNINFODatasource", lambda: FakeDS())
    monkeypatch.setattr(main, "ReportFetcher",
                        lambda **kw: type("F", (), {"fetch": lambda self, c: []})())
    monkeypatch.setattr(main, "ReportDownloader", lambda: FakeDownloader())
    monkeypatch.setattr(main, "ConfigLoader",
                        lambda: type("C", (), {"load": lambda self, p: type("Cfg2", (), {
                            "storage_dir": str(tmp_path), "companies": [],
                            "report_types": [], "start_date": None, "end_date": None})()})())

    args = main.argparse.Namespace(command="download", config="x.yaml")
    main.cmd_download(args)
    assert len(ingested) == 1
    assert ingested[0].endswith("长江电力_600900_年报_2025.pdf")


def test_cmd_analyze_auto_ingests_report(monkeypatch, tmp_path):
    """分析前后自动摄取（前置就绪 + 完成后连带分析报告）"""
    ingested = []
    dims_calls = []

    class FakeSvc:
        def auto_ingest_report(self, path):
            ingested.append(path)

    pdf = tmp_path / "长江电力_600900_年报_2025.pdf"
    pdf.write_bytes(b"%PDF")

    class FakeReport:
        def save(self, output_dir):
            return str(tmp_path / "analysis" / "xxx.md")

    class FakeAnalyzer:
        DEFAULT_DIMENSIONS = ["financial_summary", "risk_warning", "business_highlights",
                              "profit_quality", "cashflow"]

        def __init__(self, client, rag_analysis=None):
            self.rag_analysis = rag_analysis

        def analyze(self, pdf, model=None, dimensions=None):
            dims_calls.append(dimensions)
            return FakeReport()

    monkeypatch.setattr(main, "_build_rag_components",
                        lambda: (type("Cfg", (), {"auto_ingest": True, "analysis_dimensions": []})(),
                                 FakeSvc(), None))
    monkeypatch.setattr(main, "AIClient", lambda **kw: object())
    monkeypatch.setattr(main, "ReportAnalyzer", FakeAnalyzer)

    args = main.argparse.Namespace(command="analyze", pdf=str(pdf), all=False,
                                   dir="reports", output=None, model=None)
    main.cmd_analyze(args)
    # 前置 + 后置各摄取一次（幂等）
    assert ingested == [str(pdf), str(pdf)]
    # 空配置 → 内置默认 5 个维度
    assert dims_calls == [["financial_summary", "risk_warning", "business_highlights",
                           "profit_quality", "cashflow"]]


def test_cmd_download_skips_auto_ingest_when_disabled(monkeypatch, tmp_path):
    """rag.auto_ingest=false 时下载后不触发自动摄取"""
    ingested = []

    class FakeSvc:
        def auto_ingest_pdf(self, path):
            ingested.append(path)

    class FakeDownloader:
        def download_all(self, reports, storage_dir):
            (tmp_path / "长江电力_600900_年报_2025.pdf").write_bytes(b"%PDF")
            from financial_report_fetcher.models import DownloadSummary
            return DownloadSummary(success=1)

    class FakeDS:
        def build_known_companies(self):
            return {}

        def resolve_company(self, ticker, name):
            return None

        def fetch_reports(self, **kw):
            return []

    monkeypatch.setattr(main, "_build_rag_components",
                        lambda: (type("Cfg", (), {"auto_ingest": False})(), FakeSvc(), None))
    monkeypatch.setattr(main, "CNINFODatasource", lambda: FakeDS())
    monkeypatch.setattr(main, "ReportFetcher",
                        lambda **kw: type("F", (), {"fetch": lambda self, c: []})())
    monkeypatch.setattr(main, "ReportDownloader", lambda: FakeDownloader())
    monkeypatch.setattr(main, "ConfigLoader",
                        lambda: type("C", (), {"load": lambda self, p: type("Cfg2", (), {
                            "storage_dir": str(tmp_path), "companies": [],
                            "report_types": [], "start_date": None, "end_date": None})()})())

    args = main.argparse.Namespace(command="download", config="x.yaml")
    main.cmd_download(args)
    assert ingested == []


def test_cmd_analyze_single_uses_config_dims_and_rag(monkeypatch, tmp_path):
    """单份分析：注入 RagAnalysis、默认维度取自配置、前置摄取先于分析"""
    import os as _os
    from types import SimpleNamespace

    pdf = tmp_path / "贵州茅台_600519_年报_2024.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    cfg = SimpleNamespace(
        enabled=True, store_path=str(tmp_path), chunk_size=800, chunk_overlap=100,
        top_k=8, embedding_model="fake-model", auto_ingest=True,
        enhanced_analysis=True, analysis_dimensions=["financial_summary", "profit_quality"],
    )
    events = []

    class FakeStore:
        pass

    class FakeSvc:
        def auto_ingest_report(self, p):
            events.append(("ingest", _os.path.basename(p)))

    class FakeRagAnalysis:
        def __init__(self, store, top_k=8, reranker=None, rerank_candidates=30,
                     rerank_score_threshold=0.5, rerank_margin_threshold=0.05):
            # cfg 未开启 rerank → reranker 应为 None，候选数用默认 30
            events.append(("rag", top_k, reranker, rerank_candidates))

    class FakeReport:
        source_file = str(pdf)

        def save(self, out):
            return _os.path.join(out, "x.md")

    class FakeAnalyzer:
        def __init__(self, client, rag_analysis=None):
            self.rag_analysis = rag_analysis

        def analyze(self, path, model=None, dimensions=None):
            events.append(("analyze", dimensions))
            return FakeReport()

        def analyze_all_in_directory(self, *a, **k):
            return []

    monkeypatch.setattr(main, "_build_rag_components", lambda: (cfg, FakeSvc(), FakeStore()))
    monkeypatch.setattr(main, "RagAnalysis", FakeRagAnalysis)
    monkeypatch.setattr(main, "ReportAnalyzer", FakeAnalyzer)
    monkeypatch.setattr(main, "AIClient", lambda **kw: object())
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(a))

    args = SimpleNamespace(pdf=str(pdf), model=None, all=False, dir="reports", output=None)
    main.cmd_analyze(args)

    assert ("rag", 8, None, 30) in events, f"应注入 RagAnalysis（reranker=None）：{events}"
    assert ("analyze", ["financial_summary", "profit_quality"]) in events, f"默认维度应来自配置：{events}"
    ingest_idx = events.index(("ingest", pdf.name))
    analyze_idx = events.index(("analyze", ["financial_summary", "profit_quality"]))
    assert ingest_idx < analyze_idx, f"前置摄取应先于分析：{events}"


def test_cmd_analyze_all_uses_builtin_defaults_when_no_config(monkeypatch, tmp_path):
    """无 RAG 配置时（组件全 None）：不注入、不摄取，默认维度用内置 5 个"""
    from types import SimpleNamespace

    pdf = tmp_path / "贵州茅台_600519_年报_2024.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    events = []

    class FakeReport:
        source_file = str(pdf)
        company = "测试公司"
        report_year = 2024

        def save(self, out):
            return _os.path.join(out, "x.md")

    class FakeAnalyzer:
        instances = []

        def __init__(self, client, rag_analysis=None):
            self.rag_analysis = rag_analysis
            FakeAnalyzer.instances.append(self)

        def analyze(self, path, model=None, dimensions=None):
            events.append(("analyze", dimensions))
            return FakeReport()

        def analyze_all_in_directory(self, report_dir, output_dir=None, dimensions=None):
            events.append(("all", dimensions))
            return []

    monkeypatch.setattr(main, "_build_rag_components", lambda: (None, None, None))
    monkeypatch.setattr(main, "ReportAnalyzer", FakeAnalyzer)
    monkeypatch.setattr(main, "AIClient", lambda **kw: object())
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)

    args = SimpleNamespace(pdf=None, model=None, all=True, dir=str(tmp_path), output=None)
    main.cmd_analyze(args)

    assert ("all", None) in events
    # 未启用 RAG：fake analyzer 构造时 rag_analysis 应为 None
    assert FakeAnalyzer.instances[-1].rag_analysis is None
    # 无配置时 analyze_all 的 dimensions 传 None（analyzer 内部回退 DEFAULT_DIMENSIONS）
