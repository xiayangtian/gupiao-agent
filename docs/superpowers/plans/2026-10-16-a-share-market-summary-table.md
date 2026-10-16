# A 股日度全市场概览表实施计划

> **For agentic workers:** 按顺序执行；每步先补失败测试，再做最小实现并运行指定测试。不得扩大到周度/个股/归因功能。

**目标：** 今日 A 股回答稳定呈现四指数日行情和上一交易日变化、全市场有效成交/广度/量比及涨跌停摘要；修复单日表格和重试污染时间戳。
**设计：** [A 股日度全市场概览表](../specs/2026-10-16-a-share-market-summary-table.md)
**分支：** `feat/market-overview-summary`，基线 `74d2d09`

## 任务 1：固定汇总模型与覆盖降级

**文件：** `webapp/market_breadth.py`（新增）、`tests/unit/test_market_breadth.py`（新增）

1. 写测试覆盖已确认的成交量加权量比公式、涨跌/平盘计数、成交额/量汇总、字段缺失、分母无效、日期不一致及覆盖不足。
2. 实现仅接受固定的 A 股现货快照与当日涨跌停池输入的纯函数；返回小型摘要，不保留/输出原始数千行证券数据。
3. 对缓存单独测试：同进程 single-flight、有效期内命中、跨交易日失效、异常不污染缓存、同日可披露缓存 `as_of`。避免缓存未验证数据为“全市场”。
4. 全市场快照等待上限设为 80 秒，大盘复盘使用 180 秒整轮上限（普通聊天 120 秒、研究 300 秒保持不变），给冷缓存指数/快照取数及答案核验留足预算；超时降级且不填默认 0，任何无法验证的样本/口径标记 partial。

**验证：** `python3 -m pytest -q tests/unit/test_market_breadth.py`

## 任务 2：接入受控来源运行时与可信事实

**文件：** `webapp/source_adapters.py`、`webapp/chat_execution.py`、`webapp/chat_facts.py`、`webapp/chat_models.py`、`webapp/server.py`、`webapp/execution_executor.py`、相关来源/事实/复盘单测

1. 先写来源授权和执行测试：只有服务端固定 `akshare` 操作、市场类别、获准的复盘请求可触发；模型/用户输入不能选择函数、股票代码、日期或请求参数。
2. 增加固定 `a_share_index_kline` 操作：仅允许四个白名单指数，日期范围由服务端从今日窗口生成并限制在窄区间；把 AkShare 中文列映射为可信 `date/close/symbol/kline_period` 行。先测非法 symbol/date、空行、日期错配和有效日线。
3. 对今日 `market_recap` 的四指数调用改用该 AkShare 操作，不再对已返回 WAF 501 的 Tencent K 线重复发请求；其他时间范围和个股 K 线路由不变。用 `select_market_bars` 筛出请求日，并把前一根交易日记录加入比较事实。
4. 通过 `SourceRuntime` 调用摘要和指数获取器，保留 provider、operation、数据日期、获取时间、样本覆盖、状态和错误码；受控授权、缓存及尝试预算不得绕过。
5. 将单行聚合摘要转换为带来源证据的 `ALL_A` 市场事实；AkShare 指数日线转换成与现有 K 线兼容的指数收盘事实，并维持既有 AnswerRun 的 JSON 边界；旧记录读取不变。
6. 调整复盘来源组合，避免把原 MCP 的 50 行涨跌停截断误作市场总数；保证四个 AkShare 指数请求、一个市场快照及每日允许的 MCP 请求不超过现有预算。任一来源失败时保留其他有效事实并产生 partial。
7. 对确认/未确认的事实增加校验和指标显示名；验证模型回答不能改写来源数据或把缺失值补成 0。
8. 为 broad-market recap 单独设置有界 180 秒整轮超时，不扩大普通问答和研究任务期限；验证 `CHAT_RUN_TIMEOUT_SECONDS` 覆盖、默认聊天时限及研究时限仍按各自契约执行。

**验证：** `python3 -m pytest -q tests/unit/test_source_adapters.py tests/unit/test_source_runtime.py tests/unit/test_chat_facts.py tests/unit/test_chat_verifier.py tests/unit/test_execution_market_recap.py tests/unit/test_server_api.py`

## 任务 3：单日行情表与原始问题重试

**文件：** `webapp/static/chat_rendering.js`、`webapp/static/app.js`、`tests/unit/test_chat_rendering_js.py`、必要的聊天浏览器测试

1. 先增加回归测试：只有一日的四个指数事实也必须显示；缺少前一交易日对比时显示不可用；市场摘要日期/样本量/数据源可读；无法用历史或未知指标误触发表格。
2. 只在 `market_recap` 场景渲染有界的语义表格，使用 `<th scope="col">`，保留现有聊天设计与移动端可读性。
3. 发送用户消息时保存原始问题到安全的 DOM data 字段；重试/编辑/分支追问优先读原始问题，兼容旧历史气泡并排除 `<time>` 内容。
4. 测试无时间戳的旧消息、新消息上海时间戳和原问题中含 HTML 字符的安全处理。

**验证：** `python3 -m pytest -q tests/unit/test_chat_rendering_js.py tests/unit/test_chat_market_charts_js.py`、相关 JS `node --check` 与聊天浏览器回归。

## 任务 4：文档、聚焦验证与交付

1. 更新 `docs/superpowers/DESIGN-REGISTRY.md` 与 `docs/FEATURE-CATALOG.md`；在用户可见的数据口径/覆盖说明有变化时同步 README。登记实现阶段，不声称已合入。
2. 执行任务 1–3 的聚焦测试；再运行与聊天执行、来源运行时、问答验证直接相关的既有回归，排除已知独立并发时序用例并明确记录。
3. 对本地聊天页真实浏览器验证：桌面/平板/手机视口，检查单日表格、时间/来源口径、重试问题、控制台/页面错误/失败请求及移动端横向溢出。
4. `git diff --check`、工作区/分支审计，确保不包含主工作区现有未提交/未跟踪内容。验证通过后按用户授权自动合入本地 `main` 并重启服务、检查健康状态；不得推送远端。若主工作区脏文件阻止安全合入，完整保留这些文件并报告阻塞，不得擅自清理或纳入提交。

**完成判据：** 上述相关测试与浏览器验证通过；本地真实“分析今日A股行情”链路在有界期限内生成并核验答案；缺失/陈旧/超时数据均诚实降级；设计/功能目录记录与实现一致；本地用户文件不受影响。
