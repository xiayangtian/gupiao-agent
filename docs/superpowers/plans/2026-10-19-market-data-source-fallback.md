# A 股行情多来源降级与核验实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 A 股今日复盘增加受控的逐指数腾讯→AkShare→BaoStock串行降级，并修复腾讯个股报价因嵌套盘口字段而被拒的问题；今日复盘市场来源调用上限单独提高至 15。

**Architecture:** 所有出站尝试仍经 `SourceRuntime` 和 `SourceAccess`；来源顺序、证券代码、日期窗及能否使用某来源由服务器固定。为 BaoStock 增加窄能力适配器与串行锁；一次成功后停止，完整保留失败尝试及其来源证据。普通/网页问答预算保持原值。新浪因许可未核实默认不接入；双源交叉核验与全市场快照备用源后续另立阶段。

**Tech Stack:** Python 3, FastAPI, PyYAML, pytest, Tencent HTTP API, AkShare, BaoStock Python API。

**Spec:** `docs/superpowers/specs/2026-10-19-market-data-source-fallback-design.md`

## Global Constraints

- 市场复盘总来源调用最多 15；今日复盘硬超时 180 秒；更低管理员专属限额优先，运行不得超过限额。
- 仅今日复盘增加专用预算；普通问答、研究和网页调用预算不变。
- 外部行情请求只可由服务端构造固定 `SourceCall` 并经 `SourceRuntime` 授权、去重和计数；取消后不得启动后续来源。
- 今日指数顺序为 Tencent K 线 → AkShare 日线 → BaoStock 日线；BaoStock 只尝试所需白名单指数，串行连接；新浪保持默认禁用。
- 只接纳代码、日期、时点、单位和日线口径匹配的来源。冲突不平均；缺失、覆盖不足时如实标记 partial/unavailable。
- 不改动主工作区既有未提交文件，不推送远端；始终使用 TDD，先验证失败再写生产代码。

---

## 文件与职责

- `financial_report_fetcher/market/baostock.py`（新建）：懒加载 BaoStock SDK，单锁内登录、查询、登出；只提供限白名单指数、短日期窗的日线查询。
- `financial_report_fetcher/market/__init__.py`：按现有导出模式导出适配器实例或窄函数。
- `webapp/source_adapters.py`：仅为 Tencent `quote` 增加标量字段投影；新增 BaoStock 精确操作和参数授权。
- `webapp/server.py`：今日复盘按每指数逐级执行三源链，保留所有来源结果，成功即止；保留原 AkShare 快照和 MCP 概览，预算用尽时跳过可选来源。
- `webapp/chat_execution.py`、`webapp/execution_planner.py`、`webapp/execution_plan.py`：将专用复盘限额贯穿来源运行时、计划估算和服务端校验，普通意图成本不变。
- `financial_report_fetcher/rag/config.py`、`config.example.yaml`、`config.yaml`：增加 `market_recap_max_tool_calls`，默认/项目配置 15；保留 `mcp_max_tool_calls` 供其他路径使用。
- `requirements.txt`：添加已探测可用的 BaoStock Python SDK 受控版本范围。
- `tests/unit/test_source_adapters.py`、`test_chat_execution.py`、`test_execution_planner.py`、`test_execution_plan_recap.py`、`test_server_api.py`、新增 `test_market_baostock.py`：覆盖规范化、授权、串行适配器、预算、调用顺序、停止条件、证据与 partial。
- `docs/FEATURE-CATALOG.md`、`README.md`：完成后记录主备数据能力、口径/许可限制与配置项；仅在合入 `main` 后更新正式功能目录/README。

## Task 1：修复腾讯个股报价标量规范化

**Files:** `tests/unit/test_source_adapters.py`, `webapp/source_adapters.py`。

- [ ] 增加失败测试：给 `SourceCall("tencent", "quote", "market", {"symbol":"600900"})` 输入带 `bid_ask` 嵌套字典及 `price`、`change_pct` 标量的实际形状；要求 `normalize_source` 为 success，保留标量，payload 不含 `bid_ask`。
- [ ] 运行 `python3 -m pytest tests/unit/test_source_adapters.py -q -k tencent_quote_nested_bid_ask`；确认失败为 `unsupported_payload`。
- [ ] 在 `normalize_source` 解析前只对 Tencent `quote` 行做白名单标量投影；不要放宽全局 `_safe_cell`，不要递归 flatten 盘口。
- [ ] 重跑该测试及 `python3 -m pytest tests/unit/test_source_adapters.py -q`，确认通过。

## Task 2：引入串行 BaoStock 指数日线适配器

**Files:** 新建 `financial_report_fetcher/market/baostock.py`、`tests/unit/test_market_baostock.py`；修改 `financial_report_fetcher/market/__init__.py`、`requirements.txt`、`webapp/source_adapters.py`。

- [ ] 先写 BaoStock client fake 测试：固定支持四个内部指数代码映射；正确查询 `date,code,open,high,low,close,preclose,volume,amount,pctChg`；空行、登录失败、接口错误均不返回行情；`logout()` 在异常时仍被调用。
- [ ] 写串行测试：两个并发调用通过单个模块锁保护，BaoStock 的 login/query/logout 临界区不重叠。
- [ ] 写授权测试：`SourceAccess` 默认拒绝未启用的 BaoStock、非市场类别、未知操作/代码、参数多余及超过 14 天的窗口；仅允许固定四指数及 ISO 日期。
- [ ] 运行 `python3 -m pytest tests/unit/test_market_baostock.py tests/unit/test_source_adapters.py -q`；确认新能力测试按预期失败。
- [ ] 实现懒加载 SDK 适配器：进程内 `Lock` 包围登录到登出全过程；把 `sh000001/sz399001/sz399006/sh000688` 映射到 BaoStock `sh.000001/sz.399001/sz.399006/sh.000688`；按文档将 provider 请求结束日期加一天，再在客户端过滤回调用者声明的闭区间；仅回传有限数值和日期，并要求实际返回请求窗口内行。
- [ ] 将 `baostock` 限定版本加入依赖；从 `SourceAccess` 授权 BaoStock 仅执行 `a_share_index_kline`；为结果设置 provider/date coverage。
- [ ] 重跑上述测试及 `python3 -m pytest tests/unit/test_market_breadth.py tests/unit/test_source_adapters.py -q`，确认不影响现有行情规范化。

## Task 3：为今日四指数接入受控串行降级

**Files:** `webapp/server.py`、`tests/unit/test_server_api.py`、`tests/unit/test_execution_market_recap.py`。

- [ ] 写服务端假源回归：Tencent success 时只调用 Tencent；Tencent 失败后调用 AkShare；AkShare 失败后调用 BaoStock；任何一级成功即停止；各级都失败时每个指数只有失败来源，整体 partial。
- [ ] 写证据测试：失败尝试与最终来源都留在当前 AnswerRun，只有合格数据生成事实；缺少某指数或前一实际交易日时不推导涨跌。
- [ ] 写约束测试：BaoStock 对四指数按固定顺序串行；BaoStock 空数据（包括 000688）不伪造；取消后不调用下一来源；超预算后保留已有数据并提示 coverage partial。
- [ ] 运行 `python3 -m pytest tests/unit/test_server_api.py -q -k 'market_recap_fallback or market_recap_provider_order'`；确认失败因当前只支持原有单源路径。
- [ ] 抽取私有的指数来源尝试函数或在既有 `indices()` handler 中实现明确链表：每次建立不同 provider 的 `SourceCall`，通过同一 `execute_source(runtime, ...)`；只在来源选择已包含请求窗口及目标日/比较基线时停下；AkShare 与 BaoStock 的空/旧/日期错配视为该来源失败后继续。
- [ ] 对每个指数收集来源尝试结果，逐项保持顺序；最终一个来源合格后不再请求低优先级来源。周/长区间行为保持腾讯单源，不在本次范围扩展。
- [ ] 重跑新增失败用例与 `python3 -m pytest tests/unit/test_server_api.py tests/unit/test_execution_market_recap.py -q`。

## Task 4：贯通今日复盘专用 15 次预算

**Files:** `financial_report_fetcher/rag/config.py`、`config.example.yaml`、`config.yaml`、`webapp/chat_execution.py`、`webapp/execution_planner.py`、`webapp/execution_plan.py`、`webapp/server.py`、相应 unit tests。

- [ ] 在 `tests/unit/test_chat_execution.py` 增加预算测试：复盘专属配置默认 15、管理员配置 0/较低值被尊重、普通请求仍沿用 `mcp_max_tool_calls`、复盘 market/total 均不超过 15；先运行并确认失败。
- [ ] 在 `tests/unit/test_execution_planner.py` 与 `test_execution_plan_recap.py` 增加成本测试：今日 `market_indices=12`（每指数最多 3 源），`market_overview=3`（周概览最坏值），预算 15 才计划两个步骤；低于核心步骤成本时 fail-closed 或无概览计划，不将 required 概念解释为保证数据存在。
- [ ] 增加 API/SSE 预算用例：今日复盘实际调用不超过用户配置/15；日复盘最多 12 指数尝试加最多 3 次概览；达到上限后没有额外快照/核验调用，已取得来源仍持久化。
- [ ] 实现独立 `market_recap_max_tool_calls` 默认15、上界15；仅 `market_recap` 使用。配置低于默认时遵守低值，不改变普通 RAG 的 10 次设置。今日 recap plan 和 Runtime 使用同一预算，Step cost 用每日窗口判定，周复盘保持原成本。
- [ ] 对可选概览/快照采用预算优先级：先满足必需四指数的首源和可用回退，再在额度允许时执行概览；调用耗尽、时间耗尽时将未执行可选项记录为预算/时间限制并保持 partial。
- [ ] 运行 `python3 -m pytest tests/unit/test_chat_execution.py tests/unit/test_execution_planner.py tests/unit/test_execution_plan_recap.py tests/unit/test_execution_market_recap.py -q`。

## Task 5：回归、配置说明与交付检查

**Files:** `README.md`、`docs/FEATURE-CATALOG.md`、`tests/unit/test_design_registry.py`；代码及测试修改路径见前序任务。

- [ ] 执行聚焦全链路测试：`python3 -m pytest tests/unit/test_market_baostock.py tests/unit/test_source_adapters.py tests/unit/test_chat_execution.py tests/unit/test_execution_planner.py tests/unit/test_execution_plan_recap.py tests/unit/test_execution_market_recap.py tests/unit/test_market_breadth.py tests/unit/test_server_api.py -q`。
- [ ] 运行 `python3 -m compileall -q webapp financial_report_fetcher`、`git diff --check`、BaoStock client mock concurrency test。
- [ ] 在 README 配置/行情说明中注明 Tencent→AkShare→BaoStock 仅用于今日四指数日线、每日复盘专属上限15、BaoStock 串行及 17:30 数据更新、已观测 000688 空行；注明实时个股在新浪许可未确认前没有自动第二来源。
- [ ] 功能目录只记录已合入 main 的最终短 SHA、验证和用户影响；合入前可以准备本分支差异但不得宣称已交付。
- [ ] 用受控浏览器 fake-source 流程验证回答/证据显示降级来源和 partial；不在外网自动化测试中发起新浪或任意未批准数据请求。
- [ ] 复核 diff 仅含本功能文件；确认主工作区的既有 dirty 与 untracked 文件未改、未暂存、未清理；不推送远端。
