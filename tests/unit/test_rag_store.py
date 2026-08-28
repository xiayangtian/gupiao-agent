import pytest
from financial_report_fetcher.rag.chunking import Chunk
from financial_report_fetcher.rag.store import RagStore


def _chunk(text, rid="600900:2025-12-31:annual", section="第一节", page=1, idx=0):
    return Chunk(report_id=rid, source="pdf", text=text, section=section,
                 page=page, chunk_index=idx)


def test_upsert_and_query(tmp_path, fake_embedder):
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("长江电力2025年营业收入862亿元"), _chunk("长江电力ROE为15.9%")])
    hits = store.query("营业收入是多少", top_k=2)
    assert len(hits) == 2
    assert all("report_id" in h for h in hits)
    assert hits[0]["report_id"] == "600900:2025-12-31:annual"


def test_upsert_idempotent(tmp_path, fake_embedder):
    store = RagStore(str(tmp_path), fake_embedder)
    chunks = [_chunk("营业收入862亿元")]
    store.upsert(chunks)
    store.upsert(chunks)  # 相同内容再次摄取
    assert store.count_chunks() == 1


def test_query_where_filter(tmp_path, fake_embedder):
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([
        _chunk("A公司营收", rid="600900:2025-12-31:annual"),
        _chunk("B公司营收", rid="600519:2025-12-31:annual"),
    ])
    hits = store.query("营收", top_k=5, where={"report_id": "600900:2025-12-31:annual"})
    assert len(hits) == 1
    assert hits[0]["report_id"] == "600900:2025-12-31:annual"


def test_delete_report(tmp_path, fake_embedder):
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("A", rid="600900:2025-12-31:annual"), _chunk("B", rid="600519:2025-12-31:annual")])
    store.delete_report("600900:2025-12-31:annual")
    assert store.count_chunks() == 1
    assert store.list_report_ids() == ["600519:2025-12-31:annual"]


def test_rag_config_load(tmp_path, monkeypatch):
    """读不到配置时默认 disabled；auto_ingest 默认开启"""
    from financial_report_fetcher.rag.config import RagConfig
    monkeypatch.setattr("financial_report_fetcher.rag.config._CONFIG_PATHS", [str(tmp_path / "none.yaml")])
    cfg = RagConfig.load()
    assert cfg.enabled is False
    assert cfg.store_path == "data/rag"
    assert cfg.top_k == 8
    assert cfg.auto_ingest is True


def test_rag_config_auto_ingest_false(tmp_path, monkeypatch):
    """config.yaml 显式 rag.auto_ingest: false 时关闭自动摄取"""
    from financial_report_fetcher.rag.config import RagConfig
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "rag:\n"
        "  enabled: true\n"
        "  auto_ingest: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("financial_report_fetcher.rag.config._CONFIG_PATHS", [str(yaml_path)])
    cfg = RagConfig.load()
    assert cfg.enabled is True
    assert cfg.auto_ingest is False


def test_rag_config_enhanced_analysis_defaults(tmp_path, monkeypatch):
    """默认 enhanced_analysis=True、analysis_dimensions 为空列表"""
    from financial_report_fetcher.rag.config import RagConfig

    monkeypatch.setattr("financial_report_fetcher.rag.config._CONFIG_PATHS", [str(tmp_path / "none.yaml")])
    cfg = RagConfig.load()
    assert cfg.enhanced_analysis is True
    assert cfg.analysis_dimensions == []


def test_rag_config_enhanced_analysis_and_dimensions(tmp_path, monkeypatch):
    """显式 enhanced_analysis: false 关闭；analysis_dimensions 保持列表"""
    from financial_report_fetcher.rag.config import RagConfig

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "rag:\n"
        "  enabled: true\n"
        "  enhanced_analysis: false\n"
        "  analysis_dimensions:\n"
        "    - financial_summary\n"
        "    - risk_warning\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("financial_report_fetcher.rag.config._CONFIG_PATHS", [str(yaml_path)])
    cfg = RagConfig.load()
    assert cfg.enabled is True
    assert cfg.enhanced_analysis is False
    assert cfg.analysis_dimensions == ["financial_summary", "risk_warning"]


def test_rag_config_string_false_parsed_as_false(tmp_path, monkeypatch):
    """字符串 "false" 不应被 bool() 误判为 True（enabled/auto_ingest/enhanced_analysis 同修复）"""
    from financial_report_fetcher.rag.config import RagConfig

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "rag:\n"
        "  enabled: \"false\"\n"
        "  auto_ingest: \"false\"\n"
        "  enhanced_analysis: \"false\"\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("financial_report_fetcher.rag.config._CONFIG_PATHS", [str(yaml_path)])
    cfg = RagConfig.load()
    assert cfg.enabled is False
    assert cfg.auto_ingest is False
    assert cfg.enhanced_analysis is False


def test_rag_config_mcp_defaults(tmp_path, monkeypatch):
    """MCP 问答配置默认值：开启、超时 30s、最多 3 轮、白名单空"""
    from financial_report_fetcher.rag.config import RagConfig

    monkeypatch.setattr("financial_report_fetcher.rag.config._CONFIG_PATHS", [str(tmp_path / "none.yaml")])
    cfg = RagConfig.load()
    assert cfg.mcp_tools is True
    assert cfg.mcp_tool_timeout == 30
    assert cfg.mcp_max_tool_rounds == 3
    assert cfg.mcp_tool_whitelist == []


def test_rag_config_mcp_tools_parse(tmp_path, monkeypatch):
    """显式 mcp 配置解析（含字符串 false 防御）"""
    from financial_report_fetcher.rag.config import RagConfig

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "rag:\n"
        "  enabled: true\n"
        "  mcp_tools: \"false\"\n"
        "  mcp_tool_timeout: 20\n"
        "  mcp_max_tool_rounds: 2\n"
        "  mcp_tool_whitelist:\n"
        "    - get_realtime_quote\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("financial_report_fetcher.rag.config._CONFIG_PATHS", [str(yaml_path)])
    cfg = RagConfig.load()
    assert cfg.mcp_tools is False
    assert cfg.mcp_tool_timeout == 20
    assert cfg.mcp_max_tool_rounds == 2
    assert cfg.mcp_tool_whitelist == ["get_realtime_quote"]
