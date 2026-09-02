"""mcp_tools — MCP 工具定义 → OpenAI function calling 格式转换。

智能问答把 china-stock-mcp 的工具作为可调用函数提供给 LLM：
- to_openai_tools()：MCP 工具 dict（name/description/input_schema）→ OpenAI tools；
- build_tool_defs()：优先取 list_tools() 实时清单（按白名单/数量过滤），
  失败或为空时回退内置常用白名单（FALLBACK_MCP_TOOLS）。
"""

from typing import Any, Callable, Dict, List, Optional

# 内置兜底白名单：MCP 启动失败/未安装时仍可用统一参数模板
# （symbol=6 位代码 + output_format=json/markdown）
FALLBACK_MCP_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "get_stock_basic_info",
        "description": "公司基本信息：行业、主营业务、注册资本、上市日期等",
    },
    {
        "name": "get_realtime_quote",
        "description": "个股实时行情：最新价、涨跌幅、成交量、市盈率等",
    },
    {
        "name": "get_financial_metrics",
        "description": "关键财务指标：营收/净利/毛利率/ROE/资产负债率等",
    },
    {
        "name": "get_balance_sheet",
        "description": "资产负债表：货币资金、应收账款、存货、负债与股东权益",
    },
    {
        "name": "get_income_statement",
        "description": "利润表：营业收入、营业成本、净利润、每股收益",
    },
    {
        "name": "get_cash_flow",
        "description": "现金流量表：经营/投资/筹资活动现金流净额",
    },
    {
        "name": "get_fund_flow",
        "description": "资金流向：主力/超大单/大单净流入（近100交易日）",
    },
    {
        "name": "get_shareholder_info",
        "description": "股东情况：十大股东、持股比例、股东户数变化",
    },
    {
        "name": "get_profit_forecast",
        "description": "业绩预测：机构盈利预测与目标价",
    },
    {
        "name": "get_news_data",
        "description": "个股新闻：近期公告与媒体报道",
    },
    {
        "name": "get_stock_indicator",
        "description": "估值指标：PE/PB/PS/股息率等",
    },
    {
        "name": "get_industry_chain",
        "description": "产业链与行业数据：行业规模、竞争格局",
    },
]

# 内部工具，不经过 MCP。仅在运行环境配置 TAVILY_API_KEY 时注入模型。
WEB_SEARCH_TOOL: Dict[str, Any] = {
    "name": "web_search",
    "description": (
        "搜索公开网页并返回标题、URL、摘要与发布日期。仅用于补充外部或实时信息；"
        "财报数字必须优先采用本地财报证据。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "简洁、明确的搜索关键词"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 5, "description": "返回条数"},
            "search_depth": {"type": "string", "enum": ["basic", "advanced"]},
        },
        "required": ["query"],
    },
}


def _default_parameters() -> Dict[str, Any]:
    """兜底参数模板（MCP 未提供 inputSchema 时使用）"""
    return {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "6 位股票代码，如 600519"},
            "output_format": {
                "type": "string",
                "enum": ["json", "markdown"],
                "description": "返回格式：json 或 markdown",
            },
        },
        "required": ["symbol"],
    }


def to_openai_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 MCP 工具 dict 列表转 OpenAI function calling tools。

    每个输入为 {name, description, input_schema?}；
    input_schema 缺失时使用兜底参数模板（symbol + output_format）。
    """
    out = []
    for t in tools:
        name = t.get("name")
        if not name:
            continue
        schema = t.get("input_schema")
        parameters = schema if isinstance(schema, dict) else _default_parameters()
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description") or "",
                "parameters": parameters,
            },
        })
    return out


def build_tool_defs(
    list_tools_fn: Callable[[], List[Dict[str, Any]]],
    whitelist: Optional[List[str]] = None,
    max_tools: int = 12,
) -> List[Dict[str, Any]]:
    """构建注入模型的工具定义列表。

    - 优先调用 list_tools_fn() 获取实时清单（含 input_schema）；
    - whitelist 非空时仅保留白名单内工具；否则保留全部并截断到 max_tools；
    - list_tools 失败或结果为空时回退内置 FALLBACK_MCP_TOOLS。
    """
    listed: List[Dict[str, Any]] = []
    try:
        listed = list(list_tools_fn() or [])
    except Exception:
        listed = []

    if not listed:
        return to_openai_tools(list(FALLBACK_MCP_TOOLS)[:max_tools])

    if whitelist:
        wanted = {w for w in whitelist if w}
        listed = [t for t in listed if t.get("name") in wanted]
    else:
        listed = listed[:max_tools]
    return to_openai_tools(listed)
