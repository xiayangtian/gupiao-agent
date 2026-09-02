"""MCP 工具定义转换模块测试：list_tools 增强 + OpenAI function 格式"""

import pytest

from financial_report_fetcher.rag.mcp_tools import (
    FALLBACK_MCP_TOOLS,
    build_tool_defs,
    to_openai_tools,
)


class FakeListedTool:
    """模拟 mcp.types.Tool（name/description/inputSchema）"""

    def __init__(self, name, description, input_schema):
        self.name = name
        self.description = description
        self.inputSchema = input_schema


def test_tool_to_dict_keeps_input_schema():
    """list_tools 增强：返回 name/description/input_schema"""
    from financial_report_fetcher.market.mcp_client import _tool_to_dict

    tool = FakeListedTool(
        "get_financial_metrics",
        "关键财务指标",
        {"type": "object", "properties": {"symbol": {"type": "string"}}},
    )
    out = _tool_to_dict(tool)
    assert out["name"] == "get_financial_metrics"
    assert out["input_schema"]["properties"]["symbol"]["type"] == "string"


def test_tool_to_dict_keeps_empty_input_schema():
    from financial_report_fetcher.market.mcp_client import _tool_to_dict

    out = _tool_to_dict(FakeListedTool("get_time_info", "当前时间", {}))
    assert out["input_schema"] == {}


def test_to_openai_tools():
    """MCP 工具 dict → OpenAI function calling 格式"""
    tools = [
        {
            "name": "get_financial_metrics",
            "description": "关键财务指标",
            "input_schema": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
        {"name": "get_realtime_quote", "description": "实时行情", "input_schema": None},
    ]
    out = to_openai_tools(tools)
    assert len(out) == 2
    fn = out[0]["function"]
    assert out[0]["type"] == "function"
    assert fn["name"] == "get_financial_metrics"
    assert fn["parameters"]["required"] == ["symbol"]
    # input_schema 缺失时 parameters 用兜底模板
    assert out[1]["function"]["parameters"]["properties"]["symbol"]["type"] == "string"


def test_to_openai_tools_preserves_empty_no_argument_schema():
    out = to_openai_tools([{
        "name": "get_time_info",
        "description": "当前时间",
        "input_schema": {"type": "object", "properties": {}},
    }])
    assert out[0]["function"]["parameters"] == {
        "type": "object", "properties": {},
    }


def test_build_tool_defs_filters_whitelist_and_limits():
    """自动清单：按白名单过滤 + 数量上限"""
    listed = [
        {"name": f"tool_{i}", "description": f"工具{i}",
         "input_schema": {"type": "object", "properties": {}}}
        for i in range(20)
    ]
    out = build_tool_defs(lambda: listed, whitelist=["tool_3", "tool_7"], max_tools=12)
    names = [t["function"]["name"] for t in out]
    assert names == ["tool_3", "tool_7"]


def test_build_tool_defs_limits_without_whitelist():
    """白名单为空：取全部并限制数量"""
    listed = [
        {"name": f"tool_{i}", "description": f"工具{i}",
         "input_schema": {"type": "object", "properties": {}}}
        for i in range(30)
    ]
    out = build_tool_defs(lambda: listed, whitelist=[], max_tools=12)
    assert len(out) == 12


def test_build_tool_defs_fallback_when_list_tools_fails():
    """list_tools 失败：回退内置白名单"""
    def _fail():
        raise RuntimeError("MCP 不可用")

    out = build_tool_defs(_fail, max_tools=12)
    assert out, "应回退内置白名单"
    names = [t["function"]["name"] for t in out]
    for t in FALLBACK_MCP_TOOLS:
        assert t["name"] in names


def test_fallback_tools_have_parameters():
    """内置白名单转 OpenAI 格式后每个工具都有可用的 parameters 定义"""
    assert FALLBACK_MCP_TOOLS
    converted = to_openai_tools(FALLBACK_MCP_TOOLS)
    assert len(converted) == len(FALLBACK_MCP_TOOLS)
    for t in converted:
        fn = t["function"]
        assert fn["name"]
        assert fn["description"]
        params = fn["parameters"]["properties"]
        assert "symbol" in params
        assert "output_format" in params
