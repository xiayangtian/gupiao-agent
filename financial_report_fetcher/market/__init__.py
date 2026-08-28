"""
股票市场数据源包（最小闭环）

提供两类可被 agent 调用的股票数据能力：
1. tencent.TencentQuote  — 腾讯免费行情 API（实时行情 / 指数 / 日K线），零依赖
2. mcp_client.StockMCPClient — china-stock-mcp 的 MCP stdio 客户端
   （财务 / 基本面 / 历史行情 / 技术指标等 30 个工具，复用长会话）

统一入口：
    from financial_report_fetcher.market import tencent_quote, stock_mcp
"""

import logging

from .tencent import TencentQuote
from .mcp_client import StockMCPClient

logger = logging.getLogger(__name__)

# 模块级单例：Web / CLI 共享同一连接池与会话
tencent_quote = TencentQuote()
stock_mcp = StockMCPClient()

__all__ = ["TencentQuote", "StockMCPClient", "tencent_quote", "stock_mcp"]
