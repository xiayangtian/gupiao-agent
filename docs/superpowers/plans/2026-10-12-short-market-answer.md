# 短期行情答案直观展示实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对短期个股/大盘行情，在答案正文展示逐日价格与变化表，只分析请求窗口，避免无关免责声明。

**Architecture:** 继续由现有 resolver 冻结行情意图与窗口，使用持久化 `Fact` 在浏览器端生成可追溯的行情表格；对市场行情模型回答附加窗口范围内的表达规则。 broad-market 路由当前磁盘实现已正确，本次增加精确回归并重启本地服务来加载已合入的路由代码，不另改路由条件。

**Tech Stack:** Python、pytest、原生 JavaScript、HTML/CSS、Pi browser QA。

**Spec:** `docs/superpowers/specs/2026-10-12-short-market-answer.md`

## Global Constraints

- 使用现有 `Fact` 和来源 ID；不新增行情来源或网络调用。
- 百分比由相邻已取得收盘价计算并标为相邻采样变化；不生成价格以外的因果结论。
- 不主动加入用户未询问的原因归因/中长期免责声明；数据覆盖、失败和实际缺失仍须如实说明。
- 使用现有设计 token 与语义化 HTML；桌面/移动无页面横向溢出。

---

### Task 1: 锁定短期行情答案的文本约束

**Files:**
- Modify: `financial_report_fetcher/rag/qa.py` (`RagQA.answer_from_context` 的 system prompt)
- Test: `tests/unit/test_rag_qa.py`

**Interfaces:**
- 输入仍为现有 `question`、`AnswerContext` 与 `Scope`。
- 非行情问题保持现有 system prompt；`market_trend` / `market_recap` 明确限制在用户窗口，并避免未请求的套话。

- [x] **Step 1: 写失败测试**：使用现有 fake AI client 捕获 `answer_from_context` 传入的 system prompt；对短期个股/大盘问题断言只分析请求窗口、仅在用户问原因时才讨论因果；对公司财报问题确认不进入行情约束。
- [x] **Step 2: 运行目标测试确认 RED**：新增断言在实现前因缺少行情专用规则失败，随后验证通过。
- [x] **Step 3: 最小实现**：在 `answer_from_context` 使用 `QuestionRouter` 判别行情意图，仅在行情趋势/大盘复盘的 system prompt 中附加经确认的格式约束；非市场路径不改变。
- [x] **Step 4: 运行目标测试确认 GREEN**：聚焦单测通过，包含 `tests/unit/test_rag_qa.py`。

### Task 2: 在答案正文渲染短期行情事实表

**Files:**
- Modify: `webapp/static/chat_rendering.js`（新增 `renderInlineMarketFacts(run)` 并导出）
- Modify: `webapp/static/app.js`（紧随答案正文加入 renderer 输出）
- Modify: `webapp/static/style.css`（沿用现有 token 做窄屏表格布局）
- Test: `tests/unit/test_chat_rendering_js.py`

**Interfaces:**
- `renderInlineMarketFacts(run)` 返回 HTML 字符串；只有 market trend/recap 且至少有两条结构化收盘事实时输出，否则返回空串。
- 行字段为对象、日期、收盘价、较前一采样点涨跌；区间变化由同一对象首尾事实计算。

- [x] **Step 1: 写失败测试**：验证五日样例输出日期、收盘、相邻变化及区间 `+3.52%`；验证冲突/单点/跨对象和非市场 run 不产生错误趋势，长列表可展开。
- [x] **Step 2: 运行目标测试确认 RED**：renderer 接入前，相关测试因 renderer 未定义而失败；修复后目标测试通过。
- [x] **Step 3: 实现 renderer 与接入**：只收集 `source_category=market` 且 metric 为 price/index_close 的有限数值；按对象和日期排序，重复对象日期去重；使用相邻采样计算收益率；最多正文展示 20 行，更多数据用原生 `<details>` 展开；在 `appendAssistantRun` 的答案正文后插入该 HTML。
- [x] **Step 4: 适配样式**：表格复用项目现有 surface/text/border token，固定表格列并在窄屏换行；补充 `.chat-main` 的 flex 收缩约束，确保长表格下消息区滚动且输入行可见；保留键盘操作和语义化表头。
- [x] **Step 5: 运行目标测试确认 GREEN**：内联行情、请求解析、行情策略和 RAG 测试共 125 项通过；渲染与布局浏览器回归通过。

### Task 3: 回归 broad-market 路由及浏览器体验

**Files:**
- Modify: `tests/unit/test_chat_request_resolution.py`（精确请求固定 `market_recap` 与近5交易日）
- Test/Run: `tests/browser/test_chat_source_runtime.py` 或等价本地 fixture；运行已批准的 browser QA。

- [x] **Step 1: 写回归断言**：精确断言“分析A股近5个交易日整体表现”解析为 `market_recap`、`requires_company=False`，并解析近5交易日窗口。
- [x] **Step 2: 运行回归观察**：断言通过，证明当前磁盘 resolver 正确；历史失败发生在尚未加载此前路由修复的旧服务进程中。未把无关的历史运行归因为行情数据缺失。
- [x] **Step 3: 聚焦验证**：125 项聚焦单测通过；含行情表格的 desktop/tablet/mobile 浏览器布局回归 3 项通过；`browser_qa` URL `http://127.0.0.1:8129/#/chat` 三视口无控制台/页面错误，网络仅有预期的 `favicon.ico` 404。
- [ ] **Step 4: 重启本地 127.0.0.1:8000 服务**：仅在当前分支已合并 `main` 后执行，确认进程加载新代码并完成不触发生产行情请求的本地健康/路由验证。

---

**实施方式：**当前会话 inline 执行。每个任务先 RED、再 GREEN；验证后更新功能目录、README、设计台账和全局任务台账，测试失败若为基线问题则保留证据并明确报告。
