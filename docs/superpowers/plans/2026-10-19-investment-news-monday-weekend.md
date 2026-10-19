# 周一投资资讯与周末综述模块 Implementation Plan

> **For agentic workers:** 使用 inline execution 按任务逐项实施；每项先写失败测试、确认失败，再实现并复测。

**Goal:** 周一资讯与综述读取周六至周一数据，单独突出周末分析，并只允许选择已有综述日期。

**Architecture:** Store 提供 Monday 周期边界、对应全量资讯与摘要日期；Service 用同一来源窗口生成 Monday 综述，并以独立的周末引用白名单校验模块。API 暴露可用摘要日期，前端据此填充日期选择框并在周一默认查询三日资讯范围。

**Tech Stack:** Python、SQLite、FastAPI、原生 JavaScript/HTML、pytest、现有本地浏览器 fixture。

**Spec:** `docs/superpowers/specs/2026-10-19-investment-news-monday-weekend-design.md`

## Global Constraints

- 只使用本地已采集资讯；不得为缺失周末资讯自动补采。
- 使用 `Asia/Shanghai` 日历周；周一摘要常规输入为周六至周一，周末模块仅允许周六、周日 item IDs。
- 不提供买卖建议或无来源推断；新要点必须能回链原始来源。
- 综述选择器仅显示已有综述日期；无可用日期时禁用并显示明确空状态。
- 不新增依赖；保留现有日期边界、摘要保留期、分页和无障碍约定。

---

### Task 1：存储日期窗口、指纹与已有综述日期

**Files:**
- Modify: `webapp/investment_news_store.py`
- Test: `tests/unit/test_investment_news_store.py`

- [ ] 编写失败测试：周一日期指纹包含周六、周日、周一资讯；周二仅包含周二；已有摘要日期按降序返回且不包括不存在的日期。
- [ ] 运行上述定向测试确认因缺少窗口/方法而失败。
- [ ] 增加共享的 `digest_source_window(day)`、`get_digest_input_items(day)` 与 `list_digest_days()`；Monday 指纹采用窗口边界与格式版本 envelope，非周一保留原列表哈希语义。
- [ ] 重跑 `python3 -m pytest tests/unit/test_investment_news_store.py -q`。

### Task 2：周一综述输入与周末模块引用校验

**Files:**
- Modify: `webapp/investment_news.py`
- Test: `tests/unit/test_investment_news_digest.py`

- [ ] 编写失败测试：Monday fake AI 收到周六至周一资讯与周末 ID 白名单；输出 Monday 和周末引用后，持久化的 `weekend_review` 只能包含周六/周日来源；Tuesday 输入仍为单日。
- [ ] 运行该定向测试确认失败。
- [ ] 让 digest 使用 store 的共享窗口；模型结构增加 `weekend_review`；批次与合并阶段都传递并校验周末白名单；payload 保存覆盖日期与周末输入数。
- [ ] 增加周一旧摘要强制 stale 的测试；周末无数据时仍可生成普通综述并输出零周末输入数。
- [ ] 重跑 `python3 -m pytest tests/unit/test_investment_news_digest.py -q`。

### Task 3：已有综述日期 API 与 API 回归

**Files:**
- Modify: `webapp/server.py`
- Test: `tests/unit/test_investment_news_api.py`

- [ ] 编写失败测试：`GET /api/investment-news/digest/days` 仅返回存储日期，降序排列；无日期时返回空列表。
- [ ] 运行该定向测试确认失败。
- [ ] 新增只读 API 路由，返回 `{"days": [...]}` 并沿用服务未就绪错误行为。
- [ ] 重跑 `python3 -m pytest tests/unit/test_investment_news_api.py -q`。

### Task 4：周一资讯范围和已有日期选择器

**Files:**
- Modify: `webapp/static/index.html`
- Modify: `webapp/static/investment_news.js`
- Test: `tests/unit/test_investment_news_ui.py`
- Test: `tests/browser/test_investment_news_status_ui.py`
- Test fixture: `tests/browser/investment_news_test_app.py`

- [ ] 先更新浏览器回归：周一默认列表范围为周六至周一；综述 `<select>` 只列 fixture 中已有日期；没有今日综述时选最近可用日；周末模块展示引用；新综述完成后选项刷新。
- [ ] 运行新浏览器断言确认失败。
- [ ] 改用语义化 `<select>`，依据 API 日期构造选项；实现默认日期策略及任务完成后的刷新；Monday “今天”资讯列表在 market/watchlist 均请求 Sat–Mon，并明确显示范围；Monday 综述展示 weekend_review、实际覆盖范围与周末无数据状态。
- [ ] 更新现有日期筛选浏览器用例以选择有效选项而非任意输入日期。
- [ ] 运行 `python3 -m pytest tests/unit/test_investment_news_ui.py tests/browser/test_investment_news_status_ui.py -q` 与 `node --check webapp/static/investment_news.js`。

### Task 5：聚焦验收与功能记录

**Files:**
- Modify: `docs/FEATURE-CATALOG.md`
- Modify: `docs/superpowers/DESIGN-REGISTRY.md`

- [ ] 运行投资资讯 store、digest、API、UI、调度和 server API 相关测试；执行 `python3 -m compileall -q webapp` 与 `git diff --check`。
- [ ] 运行投资资讯浏览器回归与 `browser_qa`，检查桌面/平板/移动视口、控制台、页面错误、失败请求和移动端溢出。
- [ ] 更新功能目录中的 Monday 覆盖、周末引用边界、已有日期选择器与验证入口；根据真实合并证据更新设计台账状态，未合入前不得标记已实现。
