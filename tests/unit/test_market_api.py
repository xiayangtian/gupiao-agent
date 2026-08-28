"""
tests/unit/test_market_api.py

新增市场数据 API 端点测试（monkeypatch market 组件，不联网）。
"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import webapp.server as server


def _patch_market(monkeypatch, quotes=None, bars=None, mcp_text='{"ok": true}', mcp_error=None):
    """替换 server 模块中的市场单例为 fake。"""
    fake_quote = MagicMock()
    fake_quote.realtime.return_value = quotes or [{"symbol": "sh600519", "name": "贵州茅台", "price": 1346.5}]
    fake_quote.index.return_value = quotes or []
    fake_quote.kline.return_value = bars or [{"date": "2026-08-11", "close": 1346.5}]
    monkeypatch.setattr(server, "tencent_quote", fake_quote)

    fake_mcp = MagicMock()
    if mcp_error is not None:
        fake_mcp.call_tool.side_effect = mcp_error
    else:
        fake_mcp.call_tool.return_value = mcp_text
    monkeypatch.setattr(server, "stock_mcp", fake_mcp)
    return fake_quote, fake_mcp


def test_quote_endpoint(monkeypatch):
    _patch_market(monkeypatch)
    client = TestClient(server.app)
    resp = client.get("/api/quote", params={"symbols": "600519,000001"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "tencent"
    assert data["quotes"][0]["name"] == "贵州茅台"


def test_quote_endpoint_empty_symbols(monkeypatch):
    _patch_market(monkeypatch)
    client = TestClient(server.app)
    resp = client.get("/api/quote", params={"symbols": ""})
    assert resp.status_code == 400


def test_kline_endpoint(monkeypatch):
    _patch_market(monkeypatch, bars=[{"date": "2026-08-11", "close": 1346.5}])
    client = TestClient(server.app)
    resp = client.get("/api/quote/kline", params={"symbol": "600519", "period": "day", "count": 3})
    assert resp.status_code == 200
    assert resp.json()["bars"][0]["close"] == 1346.5


def test_index_endpoint(monkeypatch):
    _patch_market(monkeypatch, quotes=[{"symbol": "sh000001", "name": "上证指数", "price": 3934.09}])
    client = TestClient(server.app)
    resp = client.get("/api/quote/index", params={"codes": "sh000001"})
    assert resp.status_code == 200
    assert resp.json()["quotes"][0]["name"] == "上证指数"


def test_stock_info_endpoint(monkeypatch):
    _patch_market(monkeypatch, mcp_text='{"A股简称": "贵州茅台"}')
    client = TestClient(server.app)
    resp = client.get("/api/stock/info", params={"symbol": "600519"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool"] == "get_stock_basic_info"
    assert body["data"]["A股简称"] == "贵州茅台"


def test_stock_financials_endpoint(monkeypatch):
    _patch_market(monkeypatch, mcp_text="not-json")
    client = TestClient(server.app)
    resp = client.get("/api/stock/financials", params={"symbol": "600519"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool"] == "get_financial_metrics"
    assert body["data"] is None
    assert body["text"] == "not-json"


def test_mcp_call_endpoint(monkeypatch):
    _patch_market(monkeypatch, mcp_text="hello")
    client = TestClient(server.app)
    resp = client.post("/api/stock/mcp/call", json={"tool": "get_time_info", "arguments": {}})
    assert resp.status_code == 200
    assert resp.json()["result_text"] == "hello"


def test_mcp_call_endpoint_error(monkeypatch):
    _patch_market(monkeypatch, mcp_error=RuntimeError("mcp down"))
    client = TestClient(server.app)
    resp = client.post("/api/stock/mcp/call", json={"tool": "get_time_info"})
    assert resp.status_code == 502
