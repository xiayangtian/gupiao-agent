"""
tests/unit/test_market_mcp_client.py

StockMCPClient 单元测试：使用 fake 会话验证
list_tools / call_tool / 失败重连逻辑（不启动真实子进程）。
"""

from types import SimpleNamespace

import pytest

from financial_report_fetcher.market.mcp_client import StockMCPClient


class FakeSession:
    """模拟 mcp ClientSession：记录调用，可按配置抛错。"""

    def __init__(self, fail_first=0, tool_text='{"ok": true}', tool_error=None):
        self.calls = []
        self.list_calls = 0
        self.fail_first = fail_first
        self.tool_text = tool_text
        self.tool_error = tool_error

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.tool_error is not None:
            raise self.tool_error
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("boom")
        return SimpleNamespace(content=[SimpleNamespace(text=self.tool_text)])

    async def list_tools(self):
        self.list_calls += 1
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("boom")
        return SimpleNamespace(
            tools=[SimpleNamespace(name="t1", description="desc1"),
                   SimpleNamespace(name="t2", description="desc2")]
        )


@pytest.fixture
def client():
    c = StockMCPClient(command=["fake", "cmd"])
    yield c
    c.close()


def _install_fake(client, session):
    """把实例的 _open_session 换成返回 fake session 的 async 函数。"""
    async def _open():
        return session
    client._open_session = _open
    return session


class TestListTools:
    def test_returns_tools(self, client):
        session = _install_fake(client, FakeSession())
        tools = client.list_tools()
        assert [t["name"] for t in tools] == ["t1", "t2"]
        assert tools[0]["description"] == "desc1"

    def test_retries_and_succeeds(self, client):
        session = _install_fake(client, FakeSession(fail_first=1))
        tools = client.list_tools()
        assert len(tools) == 2
        assert session.list_calls == 2

    def test_raises_after_two_failures(self, client):
        session = _install_fake(client, FakeSession(fail_first=99))
        with pytest.raises(RuntimeError):
            client.list_tools()
        assert session.list_calls == 2


class TestCallTool:
    def test_returns_text(self, client):
        session = _install_fake(client, FakeSession(tool_text='{"name": "贵州茅台"}'))
        result = client.call_tool("get_stock_basic_info", {"symbol": "600519"})
        assert result == '{"name": "贵州茅台"}'
        assert session.calls == [("get_stock_basic_info", {"symbol": "600519"})]

    def test_defaults_empty_arguments(self, client):
        session = _install_fake(client, FakeSession())
        client.call_tool("get_time_info")
        assert session.calls == [("get_time_info", {})]

    def test_retries_on_transient_failure(self, client):
        session = _install_fake(client, FakeSession(fail_first=1, tool_text="ok"))
        result = client.call_tool("t", {})
        assert result == "ok"
        assert len(session.calls) == 2

    def test_raises_after_two_failures(self, client):
        session = _install_fake(client, FakeSession(fail_first=99))
        with pytest.raises(RuntimeError):
            client.call_tool("t", {})
        assert len(session.calls) == 2


class TestSessionCloseOnFailure:
    def test_session_reopened_after_failure(self, client, monkeypatch):
        closed = []
        s1 = FakeSession(fail_first=1, tool_text="ok")

        async def _close():
            closed.append("close")

        async def _open():
            return s1

        client._open_session = _open
        client._close_session = _close
        assert client.call_tool("t", {}) == "ok"
        assert closed == ["close"]  # 失败后关闭了旧会话
