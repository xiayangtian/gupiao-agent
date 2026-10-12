# 多指数行情图表 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在智能问答的大盘行情回答中，根据本轮对齐的指数事实绘制相对走势折线图，同时保留原始点位表和来源证据。

**Architecture:** `ChatRendering` 从当前 `AnswerRun.facts` 生成可测的多指数共同日期 view model，并在现有正文行情区输出图表容器。独立的 `ChatMarketCharts` 使用页面已加载的 Chart.js 挂载/销毁图表；历史重载与实时完成共用 `appendAssistantRun`，清空会话时统一销毁实例。

**Tech Stack:** 原生 JavaScript、现有本地 Chart.js、语义化 HTML/CSS、pytest + Node 单测 + Pi browser 测试。

**Spec:** `docs/superpowers/specs/2026-10-12-multi-index-market-chart-design.md`

## Global Constraints

- 只使用当前 AnswerRun 中 `source_category=market`、`metric=index_close`、单位 `点`、已知指数代码和有效日期的持久化事实；不新增行情请求、依赖或外部数据调用。
- 仅在至少两个指数共享至少两个采样日期时绘图；所有曲线使用共同首日为 0% 的区间涨跌幅，不插值或外推。
- 日期冲突的指数系列不进入图表；图表不足/组件不可用时保留原始收盘表和证据。
- Chart.js 实例在聊天 DOM 清空或会话切换前销毁；桌面、平板、移动端无横向溢出。
- 图例必须有文本，canvas 有描述性标签；精确原始点位始终可从现有表格读取。

---

### Task 1: 生成可比较的多指数数据模型

**Files:**
- Modify: `webapp/static/chat_rendering.js`（导出 `marketIndexChartData(run)` 并在 `renderInlineMarketFacts(run)` 中输出 chart placeholder）
- Test: `tests/unit/test_chat_rendering_js.py`

**Interfaces:**
- `marketIndexChartData(run)` 返回 `null` 或 `{ periods, series }`。
- `series` 每项为 `{ code, label, closeValues, changePctValues }`；各数组与 `periods` 等长，`changePctValues[0] === 0`。

- [x] **Step 1: 写失败测试**：构造上证/深证/创业板/科创 50 的 3 日 `index_close` facts；验证只保留共同日期，四个系列首点为 0%，后续点依据各自首日计算，label 和 raw close 保留。再测重复同值去重、重复日期异值排除该指数、跨 `period_kind`/未知代码/非 market/非 `index_close` 事实不混入；不足两个指数或不足两个共同日期返回 `null`。
- [x] **Step 2: 运行目标测试确认 RED**：`python3 -m pytest -q tests/unit/test_chat_rendering_js.py -k market_index_chart_data`：2 项因 `marketIndexChartData` 尚未导出而按预期失败。
- [x] **Step 3: 实现最小提取逻辑**：按固定指数代码建序列，日期 ISO 字符串排序；先排除非参考行情 Fact、无效点位、无效日期、未知代码和冲突系列，再计算所有合格指数的日期交集；只在交集长度≥2 且合格系列数≥2时返回 view model；按共同首日计算百分比，不改写输入 run。
- [x] **Step 4: 运行目标测试 GREEN**：新增数据模型测试 2 passed；`tests/unit/test_chat_rendering_js.py`：39 passed；`node --check webapp/static/chat_rendering.js` 与 `git diff --check` 通过。

### Task 2: 实现 Chart.js 多指数折线组件

**Files:**
- Create: `webapp/static/chat_market_charts.js`
- Create: `tests/unit/test_chat_market_charts_js.py`
- Modify: `webapp/static/index.html`（在 ChatRendering 和 app.js 之间加载组件）

**Interfaces:**
- `window.ChatMarketCharts.mount(container, run)`：从 `ChatRendering.marketIndexChartData(run)` 读取系列，在 `container` 内挂载 `Chart`；数据不足时 no-op；无 Chart 或构造异常时显示状态提示并保留表格。
- `window.ChatMarketCharts.destroyAll()`：销毁所有已注册的图表并清空实例集合。
- `window.ChatMarketCharts.chartConfig(data)`：纯函数，返回 Chart.js `line` 配置；tooltip 标签携带日期、原始点位和区间涨跌幅。

- [x] **Step 1: 写失败测试**：验证 chart config 是 line chart、数据集数与指数数一致、x 轴日期对齐、y 轴及提示使用百分比、tooltip formatter 同时显示原始点位与涨跌；使用 stub Chart 验证 mount 成功、Chart 缺失/构造异常 fallback，以及 destroyAll 对每个实例恰好调用 destroy。
- [x] **Step 2: 运行目标测试确认 RED**：`python3 -m pytest -q tests/unit/test_chat_market_charts_js.py`：2 项因图表模块尚未创建而按预期失败。
- [x] **Step 3: 实现模块**：创建独立 UMD/IIFE 模块；读取 `window.ChatRendering` 数据，采用项目现有色彩/字体 token 对应的可区分线型和文字图例；配置响应式折线及日期/原始点位/百分比 tooltip；所有异常退化为可读状态提示；实例用 Map 管理。
- [x] **Step 4: 加载模块**：将 `/static/chat_market_charts.js?v=__APP_VERSION__` 放在 `chat_rendering.js` 后、`app.js` 前，确保单页加载和历史重开均可用。
- [x] **Step 5: 运行目标测试 GREEN**：`tests/unit/test_chat_market_charts_js.py` 2 passed；数据模型目标测试 2 passed；`tests/unit/test_chat_rendering_js.py` 39 passed；`node --check` 和 `git diff --check` 通过。

### Task 3: 集成回答正文和图表生命周期

**Files:**
- Modify: `webapp/static/app.js`（`appendAssistantRun` 挂载；新增/使用聊天清空辅助函数销毁图表）
- Modify: `webapp/static/chat_rendering.js`（多指数时在原始点位表之前生成 figure、canvas、文字图例和说明）
- Modify: `webapp/static/style.css`（响应式图表容器、文字图例、加载失败状态）
- Test: `tests/unit/test_chat_rendering_js.py`、`tests/unit/test_chat_market_charts_js.py`

- [x] **Step 1: 确保 RED**：renderer 与 app harness 的 3 项扩展测试先按预期失败，再实现后通过；覆盖多指数图表容器、数据不足不绘图、挂载顺序与清理顺序。
- [x] **Step 2: 接入现有统一渲染路径**：figure 位于点位表前；`appendAssistantRun` 在插入 DOM 后挂载图表；`clearChatHistory()` 统一销毁实例并清空 DOM，历史重开和 SSE 完成共用路径。
- [x] **Step 3: 添加窄屏样式**：图表宽度 `100%`、桌面/移动稳定高度、文字图例可换行；canvas `max-width:100%`；无横向滚动容器；失败 fallback 隐藏空 canvas 并收起绘图区。
- [x] **Step 4: 运行目标测试 GREEN**：renderer/chart module 聚焦测试 43 passed；app/rendering/chart JS `node --check` 通过；`git diff --check` 通过。

### Task 4: 浏览器验收和项目文档

**Files:**
- Test: `tests/browser/test_chat_trust_flow.py` 或新增聚焦 `tests/browser/test_chat_market_chart.py`
- Modify after merge: `README.md`、`docs/FEATURE-CATALOG.md`、`docs/superpowers/DESIGN-REGISTRY.md`

- [x] **Step 1: 添加浏览器 fixture**：实际应用浏览器 fixture 注入四指数三日 AnswerRun，断言 canvas/图例/点位表、Chart.js 对齐与基准、挂载无新增 fetch；切换会话后旧实例与 figure 均清除。
- [x] **Step 2: 验证边界**：数据模型测试覆盖单指数、共同日期不足、重复冲突与混周期；Chart 缺失/构造异常测试验证隐藏 canvas、收起空绘图区并保留文本 fallback；单指数 renderer 仍保留表格。
- [x] **Step 3: 执行聚焦验证**：renderer/chart 单测 43 passed；含多指数图表及既有行情表格的浏览器测试 6 passed；三个 JS 文件语法检查及 `git diff --check` 通过。browser_qa 已验 desktop/tablet/mobile，console/page errors 无，网络仅 favicon.ico 404。
- [x] **Step 4: 更新用户文档**：合并 1188365 后更新 README 行情能力、FEATURE-CATALOG 的实现入口/不变量/验证记录，以及 DESIGN-REGISTRY 的合并 SHA 与测试证据。

---

**实施方式：**内联执行，独立 worktree；先确认本计划，再开始实现。每个行为遵循 RED→GREEN；不调用真实行情供应商，不操作生产数据。
