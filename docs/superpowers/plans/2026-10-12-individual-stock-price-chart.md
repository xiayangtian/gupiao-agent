# 个股多期行情价格图表实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:executing-plans 在当前隔离工作区逐任务执行。每项任务结束后运行其所列测试。

**Goal:** 基于本轮持久化的个股行情 Facts 为多期价格序列绘制原始价格折线图，并保留现有表格与多指数图。

**Architecture:** 在 `chat_rendering.js` 将图表资格与数据组装扩展为既有指数模式和新增个股原价模式；`chat_market_charts.js` 根据图表模式配置 Y 轴、tooltip 和挂载；表格仍由原有渲染路径输出。仅消费本轮 Facts，无新增数据源请求和依赖。

**Tech Stack:** Vanilla JavaScript, Chart.js, existing Python/Node rendering tests, FastAPI browser regression fixtures.

**Spec:** `docs/superpowers/specs/2026-10-12-individual-stock-price-chart-design.md`

## Global Constraints

- 用户已确认个股图使用原始收盘价纵轴，悬停补充相对首个采样点的涨跌幅。
- 至少两个有效日期；失败时保留表格与来源，不插值、不补请求。
- 指数 0% 相对比较、事实表和来源证据必须兼容保持。
- 遵循项目现有 JS/CSS 模块和 Chart.js 依赖，不引入 Tailwind 或新依赖。

---

### Task 1: 个股图表数据资格与视图模型（TDD）

**Files:**
- Modify: `tests/unit/test_chat_rendering_js.py`
- Modify: `webapp/static/chat_rendering.js`

**Interfaces:**
- 新增 `marketChartData(run)`，返回既有指数图或 `{mode:'stock_price', periods, periodKind, series:[{code,label,unit,closeValues,changePctValues}]}`；不满足资格时为 `null`。
- 保留 `marketIndexChartData(run)` 兼容原调用/测试；其原行为不改变。

- [x] **Step 1: 添加个股图纯数据测试**：至少两个每日收盘事实按日期排序且收盘值不归一化；再覆盖周/月粒度、值相同重复折叠、冲突、单点、坏日期、非有限/非正价格、来源/实体/核验不符、混合粒度或单位。使用现有 Node 子进程调用 `ChatRendering`。
- [x] **Step 2: 运行目标测试确认 RED**：`python3 -m pytest tests/unit/test_chat_rendering_js.py -q`；预期新个股数据测试失败、原指数用例仍通过。
- [x] **Step 3: 实现 `marketChartData`**：将个股 Facts 限定为 `metric=price`、`entity_scope=security`、`source_category=market`、`source_type=tool`、`verification=reference`；校验数值、日期、单位和粒度；重复冲突丢弃该实体曲线；仅返回不少于两个日期的数据。`marketIndexChartData` 保持原有输出。
- [x] **Step 4: 再运行 RED 目标命令至 GREEN**：`python3 -m pytest tests/unit/test_chat_rendering_js.py -q`；预期目标模块通过。

### Task 2: Chart.js 个股价格轴与访问可读标签（TDD）

**Files:**
- Modify: `tests/unit/test_chat_market_charts_js.py`
- Modify: `webapp/static/chat_market_charts.js`
- Modify: `webapp/static/chat_rendering.js`

**Interfaces:**
- `chartConfig(data)` 对 `mode='stock_price'` 将 dataset `data` 映射为原始 `closeValues`，Y 轴标签包含价格单位；tooltip 使用 `changePctValues` 展示相对首点涨跌幅。
- 图表 DOM 明确标注个股收盘价；挂载逻辑改用 `marketChartData`，无资格数据时不创建图表。

- [x] **Step 1: 添加配置与 DOM 测试**：断言原始价格数组、原始价格格式、单位 Y 轴标题、tooltip 日期/价格/相对首点涨跌；断言图表 HTML 的个股标题、可访问标签与现有事实表并存。
- [x] **Step 2: 运行目标测试确认 RED**：`python3 -m pytest tests/unit/test_chat_market_charts_js.py tests/unit/test_chat_rendering_js.py -q`；预期新增图表配置/DOM 断言失败。
- [x] **Step 3: 实现模式分支**：增加价格格式化和 stock tooltip/Y 轴，扩展图表 renderer 标题及 aria-label；保持指数 tooltip 与百分比 Y 轴不变；共享图表生命周期、fallback 和销毁行为。
- [x] **Step 4: 运行目标测试至 GREEN**：`python3 -m pytest tests/unit/test_chat_market_charts_js.py tests/unit/test_chat_rendering_js.py -q`。

### Task 3: 真实聊天行情回归与功能文档

**Files:**
- Modify: `tests/browser/test_chat_trust_flow.py` 或 `tests/browser/test_chat_source_runtime.py`
- Modify: `README.md`（仅若可见功能目录要求/入口说明需要更新）
- Modify: `docs/FEATURE-CATALOG.md`
- Modify: `docs/MARKET-CHART.md`

- [x] **Step 1: 写真实应用浏览器回归**：以本地受控 `AnswerRun` 注入两个证券日期点，验证图表和原表格同屏存在；小数据或冲突事实只显示原表格，不产生空图。
- [x] **Step 2: 运行浏览器回归确认 RED，再实现 fixture/回归修正直至 GREEN**：`python3 -m pytest tests/browser/test_chat_trust_flow.py -q -k 'individual_stock_chart'`。
- [ ] **Step 3: 更新行情图表和功能目录**：描述新入口、数据资格、单位语义和测试入口；README 仅在使用方式/可见功能索引确有需要时同步。
- [ ] **Step 4: 运行聚焦验证**：`python3 -m pytest tests/unit/test_chat_rendering_js.py tests/unit/test_chat_market_charts_js.py tests/unit/test_design_registry.py -q`；`node --check webapp/static/chat_rendering.js`；`node --check webapp/static/chat_market_charts.js`；`git diff --check`。

## Self-review

- 用户可见数值、事实表和来源证据均保留；小样本条件按已确认需求为两个日期，不套用通用图表库的四点推荐。
- 不影响指数模式与用现有 Fact 进行核验的业务链路。
