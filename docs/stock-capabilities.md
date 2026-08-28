# 股票相关能力调研（MCP / API）

> 调研日期：2026-08-11
> 目的：为 gp-agent 发现可作为工具能力（供 agent 调用）的股票数据源，
> 重点 MCP，兼顾 API。已完成"能力发现 + 简单可用性验证"，未做完整接入。
> 数据仅供学习研究，不构成投资建议。
> ✅ 更新（2026-08-11）：腾讯免费 API 与 china-stock-mcp 已完成最小闭环接入，详见 README（CLI: `quote` / `mcp` 子命令；Web: `/api/quote`、`/api/stock/*` 端点）。

---

## 1. 验证结论速览

| 能力 | 类型 | 是否验证 | 结果 |
|---|---|---|---|
| 腾讯行情 API（实时/指数/K线） | HTTP API | ✅ 实测 | 可用，免费免 key |
| 新浪行情 API（实时行情） | HTTP API | ✅ 实测 | 可用，免费，需 Referer |
| china-stock-mcp | MCP（stdio/HTTP） | ✅ 实测 | 30 个工具正常列出，基本信息/宏观数据真实返回；实时行情依赖东财/雪球源，本验证环境网络出口不通 |
| 东方财富 API（push2/push2his） | HTTP API | ⚠️ 受限 | 当前验证环境返回空（网络出口问题），国内环境通常可用 |
| stock-data-mcp | MCP | 📖 文档确认 | A/港/美股 + 加密，多数据源故障转移 |
| ashare-mcp（baostock） | MCP | 📖 文档确认 | baostock 23 个接口 + 东财公告下载 |
| tushare-mcp-server | MCP | 📖 文档确认 | 覆盖 Tushare Pro 173 个 API，需 token |
| real-time-stock-mcp-service | MCP | 📖 文档确认 | 东财+雪球，34 个工具，部分接口需 Cookie |
| akshare-one-mcp | MCP | 📖 文档确认 | 与 china-stock-mcp 同源（akshare-one） |

---

## 2. MCP 服务器（推荐优先）

### 2.1 china-stock-mcp ⭐（已验证可用）
- 仓库：https://github.com/xinkuang/china-stock-mcp
- PyPI：`china-stock-mcp`（`uvx china-stock-mcp`）
- 底层数据源：akshare-one（东方财富 / 新浪 / 雪球，自动故障切换）
- 覆盖：A/B/H 股，历史行情（分钟~年）、实时行情、新闻、三大报表、财务指标、
  资金流、股东/高管/解禁/分红、筹码分布、研报、估值、技术指标（30+）、
  技术选股排名、行业板块、指数、宏观数据
- **共 30 个工具**（实测列出），示例：
  `get_hist_data` / `get_realtime_data` / `get_balance_sheet` /
  `get_income_statement` / `get_cash_flow` / `get_fund_flow` /
  `get_financial_metrics` / `get_shareholder_info` / `get_profit_forecast` /
  `get_stock_cyq`（筹码）/ `get_stock_research_report` / `get_macro_data` /
  `get_stock_technical_rank`（选股）/ `get_cni_index_hist`（指数）等
- 运行：stdio 默认；`--streamable-http --host 0.0.0.0 --port 8081` 可开 HTTP 模式
- 验证记录：FastMCP 3.4.7 启动成功；`get_stock_basic_info(600519)` 返回贵州茅台
  完整公司信息；`get_macro_data(stock_summary)` 返回沪深市场概览；
  `get_time_info` 返回交易日。实时行情在受限网络下失败（数据源不通）。

### 2.2 stock-data-mcp
- 仓库：https://github.com/stockmcp/stock-data-mcp（PyPI: `stock-data-mcp`，`uvx stock-data-mcp`）
- 底层：akshare / efinance / baostock / yfinance / tushare / OKX / Binance，多源故障转移
- 覆盖：A股/港股/美股 + 加密货币；财务指标、龙虎榜、涨停/强势股池、板块资金流、
  美股财报/内部交易/新闻情绪、OKX 借贷比/买卖量、全球财经快讯
- 可选环境变量：`TUSHARE_TOKEN`（A股高优源）、`ALPHA_VANTAGE_API_KEY`（美股增强）、
  `OKX_BASE_URL` / `BINANCE_BASE_URL`（代理）、`NEWSNOW_CHANNELS`
- 约 28 个工具：`search` / `stock_prices` / `stock_realtime` /
  `stock_batch_realtime` / `stock_zt_pool_em` / `stock_lhb_ggtj_sina` /
  `stock_indicators_a` / `stock_news_global` 等

### 2.3 ashare-mcp（baostock）
- 仓库：https://github.com/maimai-hqw/ashare-mcp（`uv run ashare-mcp`）
- 底层：baostock（免费免注册）+ 东方财富公告接口
- 覆盖：baostock 全部 23 个接口（K线、季频财务六维、业绩快报/预告、分红、复权因子、
  行业分类、指数成分股、交易日历、宏观存款/贷款/准备金/货币供应量），
  外加**信息披露公告**工具（列公告 + 下载公告 PDF，可对接 Read 工具直接读 PDF）
- 代码格式：`sh.600519` / `sz.000001` / `bj.430047`（也支持纯 6 位数字）
- 注意：baostock 需连其数据端口，境外可能超时；公告接口走东财 HTTPS 一般境外可用

### 2.4 tushare-mcp-server
- 仓库：https://github.com/erwanjun/tushare-mcp-server（`npx -y tushare-mcp-server`）
- 需 **Tushare Pro token**（tushare.pro 注册，积分制）
- 覆盖：Tushare Pro 173 个 API 分 18 类：A股行情/财务/市场参考/指数/资金流/基金/期货/
  期权/债券/港股/美股/宏观/板块概念等
- 工具：`tushare_query`（通用查询）+ 15 个便捷工具（`get_daily_bars`、
  `get_income`、`get_balancesheet`、`get_cashflow`、`get_fina_indicator`、
  `get_moneyflow`、`get_concept`、`get_shibor` 等）+ 内置文档查询工具
- 配置：`env: { "TUSHARE_TOKEN": "..." }`

### 2.5 real-time-stock-mcp-service
- 仓库：https://github.com/DannyWongIsAvailable/real-time-stock-mcp-service
- 底层：东方财富 + 雪球，免费免登录（部分接口需浏览器 Cookie）
- 覆盖：共 **34 个工具**：股票搜索、K线（B股/H股/大盘/分时）、技术指标（MA/MACD/BOLL/RSI）、
  基本面（主营构成/经营范围）、财务分析（比率/业绩概况）、估值（PE/PB）、
  板块行情/同行对比/资金流向、智能点评与评分
- 魔搭社区提供在线体验/远程连接：https://modelscope.cn/mcp/servers/DannyWong/real-time-stock-mcp
- 环境变量：`EASTMONEY_COOKIE` / `XUEQIU_COOKIE`（过期需更新）

### 2.6 akshare-one-mcp
- 仓库：https://github.com/zwldarren/akshare-one-mcp（与 china-stock-mcp 同源，star 更高）
- 与 2.1 工具基本一致（历史行情、实时行情、新闻、财报等）
- Smithery 一键安装：`npx -y @smithery/cli install @zwldarren/akshare-one-mcp`

### 2.7 海外市场 MCP（供参考，英文为主）
| 名称 | 说明 | 定价 |
|---|---|---|
| Yahoo Finance MCP | 美股实时/历史行情 | 免费 |
| Alpha Vantage MCP | 股票/外汇/加密/技术指标 | Freemium（免费 key） |
| Financial Modeling Prep MCP | 250+ 工具，基本面/财报 | Freemium |
| Finnhub MCP | 行情+新闻+基本面 | Freemium |
| Polygon.io MCP | 美股/期权/加密 | Freemium |
| FRED MCP | 美联储经济数据 | 免费 |
| Alpaca MCP | 美股交易执行（paper/实盘） | 免费开户 |
| 精选列表 | https://github.com/BlockRunAI/awesome-finance-mcp | - |

---

## 3. 免费行情 API（已验证可用，无需 key）

### 3.1 腾讯行情（推荐）
- 实时行情：`http://qt.gtimg.cn/q=sh600519,sz000001`（GBK 编码）
  返回字段含最新价、涨跌幅、成交量/额、买卖五档、市值、市盈率等
- 指数：`http://qt.gtimg.cn/q=sh000001,sz399001`（上证/深证成指等）
- 日 K 线（前复权）：`https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,5,qfq`
- ✅ 实测：实时行情、指数、K 线均返回正常数据
- 限制：A股为主；接口为抓取型，无 SLA，仅供学习

### 3.2 新浪行情
- 实时行情：`https://hq.sinajs.cn/list=sh600519`（需 `Referer: https://finance.sina.com.cn`）
- ✅ 实测：返回贵州茅台实时行情（名称/开/收/高/低/量额/时间）
- 另有新浪财经分钟线、日线等接口（`gu.sina.cn`）

### 3.3 东方财富
- 行情：`https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=...`
- K 线：`https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&klt=101&fqt=1&...`
- ⚠️ 实测：当前验证环境（受限网络）返回空；国内正常网络一般可用
  （akshare 等库默认走东财源，说明其稳定性被广泛依赖）

---

## 4. 数据源 Python 库（作为"库型工具"供 agent 代码调用）

| 库 | 特点 | 验证/说明 |
|---|---|---|
| **AkShare** | 开源免费，覆盖最广（行情/财报/宏观/板块/新闻/另类数据），东方财富+新浪+同花顺等 | 本文 MCP 验证已间接覆盖（china-stock-mcp 依赖它） |
| **akshare-one** | akshare 的高层封装，带缓存/故障切换 | china-stock-mcp 基于它 |
| **Tushare Pro** | 需要 token（积分制），接口稳定规范，覆盖 173+ 接口 | 详见 2.4 |
| **Baostock** | 免费免注册，接口规范稳定，含季频财务/杜邦分析 | 详见 2.3 |
| **efinance** | 东财数据接口封装，轻量 | stock-data-mcp 依赖 |
| **yfinance** | 美股 Yahoo 数据，免费 | 美股场景 |
| **Ashare**（mpquant） | 腾讯+新浪双源实时/历史 K 线极简封装 | 免费 |
| **pqquation** | 新浪/腾讯/东财/集思录免费实时行情 | 免费 |

---

## 5. 与本项目（gp-agent）的契合度与接入建议

现状：gp-agent 已能抓巨潮（CNINFO）财报 PDF + AI 分析。
已接入（最小闭环）：腾讯免费 API（行情/K线）+ china-stock-mcp（财务/基本面）。
以下是其余增量能力与后续可接入路径（按性价比排序）：

1. ~~**实时行情/历史 K 线**~~ ✅ 已接入：`financial_report_fetcher/market/tencent.py`，CLI `quote` 子命令 + Web `/api/quote` 系列端点。
2. ~~**财务/基本面增强**~~ ✅ 已接入：`financial_report_fetcher/market/mcp_client.py`，CLI `mcp` 子命令 + Web `/api/stock/*` 端点。后续可继续接入：
   - china-stock-mcp：`get_balance_sheet` / `get_income_statement` /
     `get_cash_flow` / `get_financial_metrics`（30 工具，MCP 一键接入）。
   - ashare-mcp：季频财务六维 + **公告 PDF 下载**（与现有"下载财报 PDF"链路互补）。
   - tushare-mcp-server：数据最规范全面，但需注册 token。
3. **多市场**（港股/美股/加密）：stock-data-mcp 或海外 MCP（Yahoo/Alpha Vantage）。
4. **技术分析/选股**：china-stock-mcp 的 30+ 技术指标、
   `get_stock_technical_rank`（创新高/突破均线/量价齐升等）、涨停/强势股池（stock-data-mcp）。
5. **舆情/新闻**：china-stock-mcp `get_news_data`、stock-data-mcp `stock_news_global`。

> 接入方式（MCP）：fastmcp/mcp Python 客户端连接 stdio 或 streamable HTTP，
> 与本项目 FastAPI 后端天然可集成；也可直接按 3.x 的 HTTP API 或 4.x 的库方式
> 做成内部工具函数。推荐先接 china-stock-mcp + 腾讯 API 两个最小闭环。

---

## 6. 参考链接

- china-stock-mcp: https://github.com/xinkuang/china-stock-mcp
- stock-data-mcp: https://github.com/stockmcp/stock-data-mcp
- ashare-mcp: https://github.com/maimai-hqw/ashare-mcp
- tushare-mcp-server: https://github.com/erwanjun/tushare-mcp-server
- real-time-stock-mcp-service: https://github.com/DannyWongIsAvailable/real-time-stock-mcp-service
- akshare-one-mcp: https://github.com/zwldarren/akshare-one-mcp
- awesome-finance-mcp（精选列表）: https://github.com/BlockRunAI/awesome-finance-mcp
- AkShare 文档: https://akshare.akfamily.xyz/
- Tushare Pro: https://tushare.pro/
- Baostock: http://baostock.com/
