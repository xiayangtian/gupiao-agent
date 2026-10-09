"""AIClient.chat 非流式解析单元测试。

通过 fake requests.Session.post 返回 JSON 响应，不依赖真实网络。
"""

import pytest
import requests

from financial_report_fetcher.ai_client import AIClient


class FakeJsonResp:
    """模拟 requests 非流式响应：仅提供 chat() 用到的两个方法"""

    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"HTTP {self.status_code}", response=self
            )

    def json(self):
        return self._data


def _client_with(payload, status_code=200):
    """构造注入 fake session 的 AIClient"""
    client = AIClient(api_key="sk-test", base_url="https://fake.example/v1")
    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return FakeJsonResp(payload, status_code)

    fake_session = FakeSession()
    client._session = fake_session
    return client


def _choice(message=None, finish_reason="stop"):
    """构造 choices[0]"""
    return {"message": message or {}, "finish_reason": finish_reason}


def test_chat_returns_content_and_usage():
    """常规非流式响应：提取 content / finish_reason / model / usage"""
    client = _client_with({
        "choices": [_choice({"content": "财务摘要内容"})],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        "model": "test-model",
    })
    result = client.chat([{"role": "user", "content": "分析"}])
    assert result["content"] == "财务摘要内容"
    assert result["finish_reason"] == "stop"
    assert result["model"] == "test-model"
    assert result["usage"]["total_tokens"] == 150


def test_chat_keeps_reasoning_content_when_content_empty():
    """推理模型 content 为空时，reasoning_content 单独保存在 reasoning 字段，
    不污染 content——调用方据此判断"模型未输出答案"而非"数据不存在"。
    """
    client = _client_with({
        "choices": [_choice({"content": "", "reasoning_content": "思考过程……"})],
        "usage": {"total_tokens": 99},
        "model": "test-model",
    })
    result = client.chat([{"role": "user", "content": "分析"}])
    assert result["content"] == ""
    assert result["reasoning"] == "思考过程……"


def test_chat_reasoning_defaults_empty():
    """非推理模型响应无 reasoning_content 时，reasoning 字段为空字符串"""
    client = _client_with({
        "choices": [_choice({"content": "普通回复"})],
        "usage": {"total_tokens": 99},
        "model": "test-model",
    })
    result = client.chat([{"role": "user", "content": "分析"}])
    assert result["reasoning"] == ""


def test_chat_stream_preserves_terminal_finish_reason():
    class FakeStreamResp:
        def raise_for_status(self):
            pass

        def iter_lines(self, decode_unicode=False):
            assert decode_unicode is True
            return iter([
                'data: {"choices":[{"delta":{"content":"部分答案"},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
                'data: [DONE]',
            ])

    class FakeSession:
        def post(self, *_args, **_kwargs):
            return FakeStreamResp()

    client = AIClient(api_key="sk-test", base_url="https://fake.example/v1")
    client._session = FakeSession()

    events = list(client.chat_stream([{"role": "user", "content": "分析"}]))

    assert events[-1]["type"] == "done"
    assert events[-1]["finish_reason"] == "length"


def test_chat_passes_thinking_mode_to_api():
    client = _client_with({"choices": [_choice({"content": "{}"})]})

    client.chat(
        [{"role": "user", "content": "分析"}],
        thinking={"type": "disabled"},
    )

    assert client._session.calls[0][1]["json"]["thinking"] == {"type": "disabled"}
