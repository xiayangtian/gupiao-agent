"""AIClient.chat_stream 流式解析单元测试。

通过 fake requests.Session.post 返回 SSE 行，不依赖真实网络。
"""

import json
import os

import pytest
import requests

from financial_report_fetcher.ai_client import AIClient


class FakeResp:
    """模拟 requests 流式响应：iter_lines 产出 SSE 行"""

    def __init__(self, lines, status_code=200):
        self.lines = lines
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"HTTP {self.status_code}", response=self
            )

    def iter_lines(self, decode_unicode=False):
        for line in self.lines:
            yield line


def _sse_delta(content="", reasoning="", finish=None):
    """构造一行 SSE data 帧"""
    delta = {}
    if content:
        delta["content"] = content
    if reasoning:
        delta["reasoning_content"] = reasoning
    choice = {"index": 0, "delta": delta}
    if finish:
        choice["finish_reason"] = finish
    return "data: " + json.dumps(
        {"choices": [choice], "usage": {"total_tokens": 99}}, ensure_ascii=False
    )


def _client_with(lines, status_code=200):
    client = AIClient(api_key="sk-test", base_url="https://fake.example/v1")
    fake_session = type("FakeSession", (), {
        "post": lambda self, *a, **kw: FakeResp(lines, status_code),
    })()
    client._session = fake_session
    return client


def test_chat_stream_yields_deltas_and_done():
    """多段内容增量逐条 yield，最终 done 拼接完整答案"""
    lines = [
        _sse_delta(content="长江"),
        _sse_delta(content="电力"),
        _sse_delta(content="营收", finish="stop"),
        "data: [DONE]",
    ]
    client = _client_with(lines)
    events = list(client.chat_stream([{"role": "user", "content": "x"}]))
    texts = [e for e in events if e["type"] == "delta"]
    assert [e["text"] for e in texts] == ["长江", "电力", "营收"]
    done = events[-1]
    assert done["type"] == "done"
    assert done["answer"] == "长江电力营收"
    assert done["usage"]["total_tokens"] == 99


def test_chat_stream_parses_reasoning_content():
    """推理模型 reasoning_content 与 content 分离产出"""
    lines = [
        _sse_delta(reasoning="先看营收"),
        _sse_delta(content="营收为862亿"),
        "data: [DONE]",
    ]
    client = _client_with(lines)
    events = list(client.chat_stream([{"role": "user", "content": "x"}]))
    delta_types = [e for e in events if e["type"] == "delta"]
    assert delta_types[0]["reasoning"] == "先看营收"
    assert delta_types[0]["text"] == ""
    assert delta_types[1]["text"] == "营收为862亿"
    assert events[-1]["type"] == "done"
    assert events[-1]["reasoning"] == "先看营收"


def test_chat_stream_http_error_yields_error_event():
    """HTTP 非 2xx 产出 error 事件而非抛异常"""
    client = _client_with([], status_code=500)
    events = list(client.chat_stream([{"role": "user", "content": "x"}]))
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "HTTP 500" in events[0]["error"]


def test_chat_stream_sends_stream_flag():
    """请求体携带 stream: true 且透传 system/messages"""
    captured = {}

    def fake_post(self, url, json=None, timeout=None, stream=False):
        captured["json"] = json
        captured["stream"] = stream
        return FakeResp(["data: [DONE]"])

    client = AIClient(api_key="sk-test", base_url="https://fake.example/v1")
    client._session = type("FakeSession", (), {"post": fake_post})()
    list(client.chat_stream(
        [{"role": "user", "content": "问题"}],
        system="你是分析师",
        model="test-model",
    ))
    assert captured["stream"] is True
    assert captured["json"]["stream"] is True
    assert captured["json"]["model"] == "test-model"
    assert captured["json"]["messages"][0]["role"] == "system"


def _sse_tool_delta(index, tool_id=None, name=None, arguments=None):
    """构造 tool_calls 分片 SSE 帧"""
    tc = {"index": index}
    if tool_id:
        tc["id"] = tool_id
    fn = {}
    if name:
        fn["name"] = name
    if arguments:
        fn["arguments"] = arguments
    if fn:
        tc["function"] = fn
    return "data: " + json.dumps(
        {"choices": [{"delta": {"tool_calls": [tc]}}]}, ensure_ascii=False
    )


def test_chat_stream_accumulates_tool_calls():
    """流式 tool_calls 分片（id/name/arguments）累积为完整事件"""
    lines = [
        _sse_tool_delta(0, tool_id="call_1", name="get_financial_metrics",
                        arguments='{"symbol":'),
        _sse_tool_delta(0, arguments='"600519"}'),
        _sse_tool_delta(1, tool_id="call_2", name="get_realtime_quote",
                        arguments="{}"),
        "data: " + json.dumps(
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        ),
        "data: [DONE]",
    ]
    client = _client_with(lines)
    events = list(client.chat_stream([{"role": "user", "content": "x"}], tools=[{"type": "function"}]))
    tool_events = [e for e in events if e["type"] == "tool_calls"]
    assert len(tool_events) == 1
    calls = tool_events[0]["tool_calls"]
    assert len(calls) == 2
    assert calls[0] == {
        "id": "call_1",
        "name": "get_financial_metrics",
        "arguments": '{"symbol":"600519"}',
    }
    assert calls[1]["name"] == "get_realtime_quote"
    # 无 content → 不产 done 事件
    assert not [e for e in events if e["type"] == "done"]


def test_chat_stream_no_tool_calls_still_done():
    """无 tool_calls 时保持现状：产出 done 而非 tool_calls"""
    lines = [
        _sse_delta(content="答案"),
        "data: [DONE]",
    ]
    client = _client_with(lines)
    events = list(client.chat_stream([{"role": "user", "content": "x"}], tools=[{"type": "function"}]))
    assert [e["type"] for e in events] == ["delta", "done"]
    assert events[-1]["answer"] == "答案"


def test_chat_stream_sends_tools_in_body():
    """tools 参数写入请求体，且 tool_choice 为 auto"""
    captured = {}

    def fake_post(self, url, json=None, timeout=None, stream=False):
        captured["json"] = json
        return FakeResp(["data: [DONE]"])

    client = AIClient(api_key="sk-test", base_url="https://fake.example/v1")
    client._session = type("FakeSession", (), {"post": fake_post})()
    tools = [{"type": "function", "function": {"name": "get_financial_metrics"}}]
    list(client.chat_stream([{"role": "user", "content": "x"}], tools=tools))
    assert captured["json"]["tools"] == tools
    assert captured["json"]["tool_choice"] == "auto"


def test_ignore_unwritable_sslkeylog(tmp_path, monkeypatch):
    """SSLKEYLOGFILE 指向不可写路径时忽略之，避免 urllib3 HTTPS 连接失败"""
    bad = str(tmp_path / "no_such_dir" / "key.log")   # 目录不存在 → 不可写
    monkeypatch.setenv("SSLKEYLOGFILE", bad)
    AIClient(api_key="sk-test", base_url="https://fake.example/v1")
    assert os.environ.get("SSLKEYLOGFILE") is None


def test_keep_writable_sslkeylog(tmp_path, monkeypatch):
    """SSLKEYLOGFILE 可写（合法调试场景）时保留"""
    good = tmp_path / "key.log"
    good.write_text("")
    monkeypatch.setenv("SSLKEYLOGFILE", str(good))
    AIClient(api_key="sk-test", base_url="https://fake.example/v1")
    assert os.environ.get("SSLKEYLOGFILE") == str(good)
