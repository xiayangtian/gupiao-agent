"""StockMCPClient 环境与缓存适配测试（不连接真实服务器）"""

import os

from financial_report_fetcher.market.mcp_client import (
    _is_writable_dir,
    _pick_uv_cache_dir,
)


def test_is_writable_dir_true_for_writable(tmp_path):
    assert _is_writable_dir(str(tmp_path)) is True


def test_is_writable_dir_false_for_file_path(tmp_path):
    """已存在的普通文件不是可写目录（makedirs exist_ok 会失败）"""
    f = tmp_path / "afile"
    f.write_text("x")
    assert _is_writable_dir(str(f)) is False


def test_pick_uv_cache_dir_keeps_writable(tmp_path):
    """默认缓存可写时返回 None（不覆盖 UV_CACHE_DIR）"""
    assert _pick_uv_cache_dir(str(tmp_path), str(tmp_path / "alt")) is None


def test_pick_uv_cache_dir_falls_back_to_alt(tmp_path):
    """默认缓存不可写（为普通文件）时指向可写替代目录"""
    bad = tmp_path / "afile"
    bad.write_text("x")
    alt = tmp_path / "alt-uv-cache"
    picked = _pick_uv_cache_dir(str(bad), str(alt))
    assert picked == str(alt)


def test_build_server_env_inherits_and_falls_back(monkeypatch, tmp_path):
    """子进程环境继承 os.environ，且默认 uv 缓存不可写时注入 UV_CACHE_DIR"""
    import tempfile

    from financial_report_fetcher.market import mcp_client as mc

    monkeypatch.setenv("AI_API_KEY", "sk-xxx")
    monkeypatch.setattr(mc, "_pick_uv_cache_dir", lambda cache, alt: str(alt))
    monkeypatch.setattr(mc, "tempfile", type("T", (), {"gettempdir": staticmethod(lambda: str(tmp_path))}))

    env = mc._build_server_env()
    assert env["AI_API_KEY"] == "sk-xxx"
    assert (env.get("UV_CACHE_DIR") or "").startswith(str(tmp_path))


def test_build_server_env_keeps_explicit_uv_cache_dir(monkeypatch):
    """用户已显式设置 UV_CACHE_DIR 时不覆盖"""
    from financial_report_fetcher.market import mcp_client as mc

    monkeypatch.setenv("UV_CACHE_DIR", "/custom/cache")
    env = mc._build_server_env()
    assert env["UV_CACHE_DIR"] == "/custom/cache"


def test_find_local_venv_command_when_exists(monkeypatch, tmp_path):
    """本地持久 venv 存在时返回其启动命令"""
    from financial_report_fetcher.market import mcp_client as mc

    root = tmp_path / "stockmcp-venv"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "python").write_text("")
    monkeypatch.setattr(mc, "_stockmcp_venv_root", lambda: str(root))
    assert mc._find_local_venv_command() == [
        str(root / "bin" / "python"), "-m", "china_stock_mcp",
    ]


def test_find_local_venv_command_none_when_missing(monkeypatch, tmp_path):
    from financial_report_fetcher.market import mcp_client as mc

    monkeypatch.setattr(mc, "_stockmcp_venv_root", lambda: str(tmp_path / "missing"))
    assert mc._find_local_venv_command() is None


def test_resolve_server_command_prefers_env(monkeypatch):
    from financial_report_fetcher.market import mcp_client as mc

    monkeypatch.setenv("CHINA_STOCK_MCP_CMD", "/opt/venv/bin/python -m china_stock_mcp")
    monkeypatch.setattr(mc, "_find_local_venv_command", lambda: ["/local", "-m", "china_stock_mcp"])
    assert mc._resolve_server_command() == ["/opt/venv/bin/python", "-m", "china_stock_mcp"]


def test_resolve_server_command_falls_back_to_local_venv(monkeypatch):
    from financial_report_fetcher.market import mcp_client as mc

    monkeypatch.delenv("CHINA_STOCK_MCP_CMD", raising=False)
    monkeypatch.setattr(mc, "_find_local_venv_command", lambda: ["/local/bin/python", "-m", "china_stock_mcp"])
    assert mc._resolve_server_command() == ["/local/bin/python", "-m", "china_stock_mcp"]


def test_resolve_server_command_default_uvx(monkeypatch):
    from financial_report_fetcher.market import mcp_client as mc

    monkeypatch.delenv("CHINA_STOCK_MCP_CMD", raising=False)
    monkeypatch.setattr(mc, "_find_local_venv_command", lambda: None)
    assert mc._resolve_server_command() == ["uvx", "china-stock-mcp"]
