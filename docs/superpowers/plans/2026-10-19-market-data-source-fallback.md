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

- [x] 增加失败测试：给 `SourceCall("tencent", "quote", "market", {"symbol":"600900"})` 输入带 `bid_ask` 嵌套字典及 `price`、`change_pct` 标量的实际形状；要求 `normalize_source` 为 success，保留标量，payload 不含 `bid_ask`。
- [x] 运行 `python3 -m pytest tests/unit/test_source_adapters.py -q -k tencent_quote_nested_bid_ask`；确认失败为 `unsupported_payload`。
- [x] 在 `normalize_source` 解析前只对 Tencent `quote` 行做白名单标量投影；不要放宽全局 `_safe_cell`，不要递归 flatten 盘口。
- [x] 重跑该测试及 `python3 -m pytest tests/unit/test_source_adapters.py -q`，确认通过：14 passed。

## Task 2：引入串行 BaoStock 指数日线适配器

**Files:** 新建 `financial_report_fetcher/market/baostock.py`、`tests/unit/test_market_baostock.py`；修改 `financial_report_fetcher/market/__init__.py`、`requirements.txt`、`webapp/source_adapters.py`。

- [x] 先写 BaoStock client fake 测试：固定支持四个内部指数代码映射；正确查询 `date,code,open,high,low,close,preclose,volume,amount,pctChg`；空行、登录失败、接口错误均不返回行情；`logout()` 在异常时仍被调用。
- [x] 写串行测试：两个并发调用通过单个模块锁保护，BaoStock 的 login/query/logout 临界区不重叠。
- [x] 写授权测试：`SourceAccess` 默认拒绝未启用的 BaoStock、非市场类别、未知操作/代码、参数多余及超过 14 天的窗口；仅允许固定四指数及 ISO 日期。
- [x] 运行 `python3 -m pytest tests/unit/test_market_baostock.py tests/unit/test_source_adapters.py -q`；初始能力测试按预期因模块/授权未实现而失败。
- [x] 实现懒加载 SDK 适配器：进程内 `Lock` 包围登录到登出全过程；把 `sh000001/sz399001/sz399006/sh000688` 映射到 BaoStock `sh.000001/sz.399001/sz.399006/sh.000688`；provider 查询截止日额外一天后在客户端过滤至调用窗口闭区间；仅回传有限数值和日期，并要求实际返回请求窗口内行。
- [x] 将 `baostock` 限定版本加入依赖；从 `SourceAccess` 授权 BaoStock 仅执行 `a_share_index_kline`；为结果设置 provider/date coverage。
- [x] 重跑上述测试及 `python3 -m pytest tests/unit/test_market_breadth.py tests/unit/test_source_adapters.py -q`：27 passed；行情规范化回归 25 passed。

## Task 3：为今日四指数接入受控串行降级

**Files:** `webapp/server.py`、`tests/unit/test_server_api.py`、`tests/unit/test_execution_market_recap.py`。

- [x] 修正基线复盘测试夹具：比较日期从 `trade_date` 动态生成，并跳过周末；确认无过时硬编码日期。原用例失败于硬编码 2026-10-16；修复后单测 1 passed。
- [x] 写服务端假源回归：Tencent success 时只调用 Tencent；Tencent 失败后调用 AkShare；AkShare 失败后调用 BaoStock；任何一级成功即停止；全源失败时保留来源尝试并标记 partial。
- [x] 写证据测试：失败尝试与最终来源都留在当前 AnswerRun，只有合格数据生成事实；缺少或无效的前一交易日收盘不推导涨跌，并继续尝试回退源。
- [x] 写约束测试：固定供应商顺序、BaoStock `sh000688` 空数据不伪造、取消后不启动下一次 SourceRuntime 调用。
- [x] 增加服务端预算耗尽集成断言：管理员额度 4 时先尝试四个指数首源，跳过 AkShare/BaoStock 及概览真实请求，保留预算失败来源并标为 partial。
- [x] 运行 `python3 -m pytest 'tests/unit/test_server_api.py::TestChatSessionsApi::test_recap_asgi_falls_back_through_authorized_daily_index_sources' -q`；三种成功/失败组合通过。
- [ ] 抽取私有的指数来源尝试函数或在既有 `indices()` handler 中实现明确链表：每次建立不同 provider 的 `SourceCall`，通过同一 `execute_source(runtime, ...)`；只在来源选择已包含请求窗口及目标日/比较基线时停下；AkShare 与 BaoStock 的空/旧/日期错配视为该来源失败后继续。
- [ ] 对每个指数收集来源尝试结果，逐项保持顺序；最终一个来源合格后不再请求低优先级来源。周/长区间行为保持腾讯单源，不在本次范围扩展。
- [ ] 重跑新增失败用例与 `python3 -m pytest tests/unit/test_server_api.py tests/unit/test_execution_market_recap.py -q`。

## Task 4：贯通今日复盘专用 15 次预算

**Files:** `financial_report_fetcher/rag/config.py`、`config.example.yaml`、`config.yaml`、`webapp/chat_execution.py`、`webapp/execution_planner.py`、`webapp/execution_plan.py`、`webapp/server.py`、相应 unit tests。

- [x] 在 `tests/unit/test_chat_execution.py` 增加预算测试：今日专属默认 15、较低值被尊重、普通请求仍沿用 `mcp_max_tool_calls`、总额硬封顶 15；另验证非今日复盘保留原 9 总额/7 行情/2 网页限额。
- [x] 在 `tests/unit/test_execution_planner.py` 增加计划成本测试：今日 `market_indices=12`、`market_overview=3`，预算 15 才包含两个步骤；低于总成本时仅计划指数步骤。
- [x] 增加 API/SSE 预算用例：今日复盘在默认15额度下实际7/8次；管理员额度4时仅4次指数首源调用，未调用回退/概览/快照，并保留 partial 状态。
- [x] 实现独立 `market_recap_max_tool_calls` 默认15、上界15；仅今日 `market_recap` 使用。配置低于默认时遵守低值，不改变普通 RAG 设置。今日 recap plan 和 Runtime 使用同一预算；非今日复盘继续使用 legacy 预算与成本。
- [x] 对可选概览/快照采用预算优先级：指数步骤先执行；预算耗尽后 SourceRuntime 拒绝后续回退/概览/快照调用，持久化 `budget_exhausted` 结果并保持 partial。
- [ ] 运行 `python3 -m pytest tests/unit/test_chat_execution.py tests/unit/test_execution_planner.py tests/unit/test_execution_plan_recap.py tests/unit/test_execution_market_recap.py -q`。

## Task 5：回归、配置说明与交付检查

**Files:** `README.md`、`docs/FEATURE-CATALOG.md`、`tests/unit/test_design_registry.py`；代码及测试修改路径见前序任务。

- [x] 聚焦全链路测试：`python3 -m pytest tests/unit/test_market_baostock.py tests/unit/test_source_adapters.py tests/unit/test_chat_execution.py tests/unit/test_chat_facts.py tests/unit/test_execution_planner.py tests/unit/test_execution_plan_recap.py tests/unit/test_execution_market_recap.py tests/unit/test_market_breadth.py tests/unit/test_server_api.py -q`：319 passed，3 warnings。
- [x] `python3 -m compileall -q webapp financial_report_fetcher`、`git diff --check` 及 BaoStock mock concurrency test 通过。`tests/unit/test_design_registry.py` 为 1 failed/8 passed，失败是既有遗漏 `2026-10-16-a-share-market-summary-table.md` 登记行；在实现分支父提交 `f26328b` 上同样复现，未扩大本次变更范围。
- [ ] 在 README 配置/行情说明中注明 Tencent→AkShare→BaoStock 仅用于今日四指数日线、每日复盘专属上限15、BaoStock 串行及 17:30 数据更新、已观测 000688 空行；注明实时个股在新浪许可未确认前没有自动第二来源。
- [ ] 功能目录只记录已合入 main 的最终短 SHA、验证和用户影响；合入前可以准备本分支差异但不得宣称已交付。
- [x] 本次没有前端 DOM、样式或浏览器交互改动，因此浏览器视口矩阵不适用；受控 API/SSE fake-source 集成覆盖回退证据、事实和 partial，不发起新浪或未批准数据请求。
- [x] 复核提交 `85f0e5d` 仅含本功能代码、测试、配置与计划文件；主工作区既有 dirty/untracked 文件未改、未暂存、未清理；未推送远端。
- [ ] 本地集成、README/功能目录更新及服务重启待处理：主工作区的 `docs/superpowers/DESIGN-REGISTRY.md` 已修改且本分支也修改该路径，合并可能覆盖脏改动；`docs/FEATURE-CATALOG.md` 亦有未提交改动，须待主工作区清理/确认后再安全更新。按保护规则未尝试合并。
