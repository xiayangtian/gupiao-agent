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
    fake_session = type("FakeSession", (), {
        "post": lambda self, *a, **kw: FakeJsonResp(payload, status_code),
    })()
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
