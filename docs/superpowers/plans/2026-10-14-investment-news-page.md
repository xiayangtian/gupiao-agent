# 投资资讯页实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在单机 Web 应用中实现 A 股市场与最多 20 只共享自选股的资讯采集、SQLite 历史浏览、独立人工操作和每日两次 AI 综述。

**Architecture:** 新增独立 SQLite store、AKShare/Eastmoney 来源适配器、采集/综述协调服务和 Asia/Shanghai 内部调度器；FastAPI 暴露小页量读取及独立任务 API，静态页面复用现有 hash 路由和 CSS tokens。复用现有 `AIClient`，不新增依赖或重构问答系统。

**Tech Stack:** Python 3.11+、FastAPI、SQLite、AKShare、现有 `AIClient`、原生 HTML/CSS/JavaScript、pytest、项目现有浏览器测试工具。

**Spec:** `docs/superpowers/specs/2026-10-14-investment-news-page-design.md`

## Global Constraints

- 只针对单机单用户自用；无账号、多用户、公开分享或商业/再分发功能。
- 用户已获知东方财富法律声明后明确选择其作为个人非商用回退源；此决定不是上游书面许可或法律意见。保留来源标识和原文链接，不宣称内容授权；如部署范围改变，先停用或取得许可。
- 不新增 Python/JavaScript 依赖；复用现有 AKShare、FastAPI、AIClient、静态页面与 CSS tokens。
- 来源资讯片段和内容型 AI 日综述最多保留 90 天；来源名/链接/运行状态等必要元数据可留作运行审计。本项目不复制整篇文章，单条片段最多 1,200 字；个人非商用边界是用户选择，不代表平台授权或法律结论。
- 页面每页最多 20 条，但采集和综述输入不受该展示页量限制；综述覆盖当天所有已入库条目。
- 采集每天 `Asia/Shanghai` 09:00–16:00 整点运行；自动综述仅 09:00、16:00 且在该时点采集完成后运行；无新输入时跳过。
- 手动采集与手动综述独立；手动采集不调用 AI，手动综述只读库且强制生成。
- 最多 20 只共享自选股；一类任务活动时同类重复触发复用现有 run；采集和综述状态分开保存。
- 不将转载条数表述为独立佐证；不输出股价因果判断、买卖建议或黑箱重要性分数。
- 外部来源失败不得清空旧数据或伪装成空库；来源时间未知不得用采集时间伪装。

---

## 文件结构

- Create `webapp/investment_news_store.py`：资讯、自选股、运行、综述 SQLite schema 与事务查询。
- Create `webapp/investment_news_sources.py`：Eastmoney/AKShare DataFrame 规范化，隔离外部字段变化。
- Create `webapp/investment_news.py`：任务排队、采集、幂等 run 状态、摘要输入与输出验证。
- Create `webapp/investment_news_scheduler.py`：可注入时钟的小时/综述时段调度，生命周期启动/停止。
- Modify `webapp/server.py`：模块级服务注入、API 路由和既有 startup/shutdown handler。
- Modify `webapp/static/index.html`、`webapp/static/app.js`、`webapp/static/style.css`：导航、hash 路由、页面结构和移动优先样式。
- Create `webapp/static/investment_news.js`：列表、操作、轮询、综述和自选交互。
- Create `tests/unit/test_investment_news_store.py`、`test_investment_news_sources.py`、`test_investment_news.py`、`test_investment_news_scheduler.py`、`test_investment_news_api.py`、`test_investment_news_ui.py`、`investment_news_fixtures.py`。
- Create: `tests/browser/investment_news_test_app.py`，仅供 browser QA 启动真实 FastAPI 应用时注入本地 fake source/AI 与临时数据库，不纳入产品运行。浏览器 QA 使用 Pi `browser_qa`，不触发真实来源/AI。
- Modify `README.md`：功能合入后说明仅本机个人使用、数据源来源标识和仅自用范围。
- Modify `docs/FEATURE-CATALOG.md`：仅在实现验证通过并合入时补入口、关键不变量与测试入口。
- Modify `docs/superpowers/DESIGN-REGISTRY.md`：计划、状态和验证/合并证据随生命周期更新。

## 单元测试 fixture 契约

`tests/unit/investment_news_fixtures.py` 在 Task 1 创建；它只 import 当前 Task 已存在的 model，后续服务 import 延迟到调用：

```python
from datetime import datetime, timezone
import json
import re
from webapp.investment_news_store import InvestmentNewsStore, NewsItem


def make_item(index, *, day="2026-10-14", url=None, title=None, symbol=None):
    published = datetime.fromisoformat(f"{day}T10:00:00+08:00").astimezone(timezone.utc).isoformat()
    return NewsItem(
        id=f"item-{index}", title=title or f"测试新闻{index}",
        url=url or f"https://news.example/{index}", snippet="来源片段",
        source_id="eastmoney_test", source_name="测试来源",
        published_at=published, collected_at=published,
        kind="watchlist" if symbol else "market",
        symbols=(symbol,) if symbol else (),
    )


class FakeNewsSource:
    def __init__(self, market_items=(), stock_items_by_code=None, failures=None):
        self.market_items = list(market_items)
        self.stock_items_by_code = dict(stock_items_by_code or {})
        self.failures = dict(failures or {})
        self.calls = []

    def fetch_market(self):
        self.calls.append(("market", None))
        return list(self.market_items)

    def fetch_stock(self, code, name):
        self.calls.append(("stock", code))
        if code in self.failures:
            raise self.failures[code]
        return list(self.stock_items_by_code.get(code, []))


class RecordingAIClient:
    def __init__(self):
        self.calls = []
        self.seen_item_ids = set()

    def chat(self, messages, **kwargs):
        ids = sorted(set(re.findall(r"item-[0-9]+", json.dumps(messages, ensure_ascii=False))))
        self.calls.append({"messages": messages, "kwargs": kwargs})
        self.seen_item_ids.update(ids)
        content = json.dumps({
            "market_themes": [{"text": "测试综述", "item_ids": ids}] if ids else [],
            "policy_regulatory": [], "watchlist": [], "risks": [],
        }, ensure_ascii=False)
        return {"content": content, "model": "test-model", "usage": {}}


def make_service(tmp_path, source=None, ai_client=None):
    from webapp.investment_news import InvestmentNewsService
    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    service = InvestmentNewsService(
        store=store, source=source or FakeNewsSource(),
        ai_client=ai_client or RecordingAIClient(),
        stock_name_lookup=lambda code: f"测试股票{code}",
    )
    return service, store
```

- [x] **Task 1 中实现 `NewsItem`** 位于 `investment_news_store.py`，使源适配器和 fake fixture 共用同一数据契约。`make_service()` 在 Task 3 前不得被调用。

## Task 1: SQLite 资讯、自选与综述存储

**Files:**
- Create: `webapp/investment_news_store.py`
- Test: `tests/unit/test_investment_news_store.py`

**Interfaces:**
- `NewsItem`: frozen dataclass，字段 `id: str`、`title: str`、`url: str`、`snippet: str`、`source_id: str`、`source_name: str`、`published_at: str | None`、`collected_at: str`、`kind: str`、`symbols: tuple[str, ...]`。
- `InvestmentNewsStore(db_path: str)`；方法 `upsert_items(items) -> int`、`get_day_items(day) -> list[dict]`、`list_items(day, kind, symbol, sort, limit, offset) -> dict`、`get_watchlist() -> list[dict]`、`add_watchlist(code, name)`、`remove_watchlist(code) -> bool`、`create_run(kind, trigger) -> str`、`update_run(run_id, *, status, result=None, error=None, finished_at=None)`、`get_active_run(kind)`、`list_runs(limit=20)`、`save_digest(day, payload, input_hash, model, ...)`、`get_digest(day)`、`daily_input_hash(day)`、`has_schedule_slot(slot_id)`、`mark_schedule_slot(slot_id)`、`cleanup_expired(now) -> int`。`add_watchlist` 重复代码更新名称；第 21 个不同代码抛 `WatchlistLimitError`。
- `news_items` 对规范化 `url_key` 唯一；不因相似标题删除条目。标题标准化完全相同且同一上海自然日时只建关联分组。

- [x] **Step 1: 先写 SQLite 行为测试**，覆盖 URL 幂等、同标题不同 URL 保留并相关联、时间范围/类别/股票筛选、分页、20 只 watchlist 上限和过期清理。

```python
from tests.unit.investment_news_fixtures import make_item

def test_same_url_is_idempotent_but_related_urls_are_preserved(tmp_path):
    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    first = make_item(1, url="https://news.example/a", title="市场公告", symbol="600900")
    duplicate = make_item(2, url="https://news.example/a?utm_source=feed", title="市场公告", symbol="600900")
    related = make_item(3, url="https://other.example/b", title="市场公告", symbol="600900")

    store.upsert_items([first, duplicate, related])

    result = store.list_items("2026-10-14", "watchlist", "600900", "latest", 20, 0)
    assert result["total"] == 2
    assert result["items"][0]["story_group_id"] == result["items"][1]["story_group_id"]
```

- [x] **Step 2: 运行目标测试确认 RED**。

Run: `python3 -m pytest tests/unit/test_investment_news_store.py -q`  
Expected: `ModuleNotFoundError`，因为 store 尚不存在。

- [x] **Step 3: 实现 schema 和最小查询 API**。使用 `PRAGMA user_version` 管 schema；每个操作短连接、`foreign_keys=ON`、`busy_timeout=5000`；初始化启用 WAL。`news_items`、`watchlist`、`news_runs`、`daily_digests`、`schedule_slots` 建索引。事务内 `INSERT ... ON CONFLICT(url_key) DO UPDATE`，保留首采时间并更新最近采集时间。`cleanup_expired` 删除早于 90 天的资讯片段、关系和内容型日综述；只保留状态/计数/错误等运行元数据，不将模型综述全文写入永久运行日志。

- [x] **Step 4: 运行 store 测试确认 GREEN**。

Run: `python3 -m pytest tests/unit/test_investment_news_store.py -q`  
Expected: 7 passed。

- [x] **Step 5: 运行设计台账回归并检查差异**。

Run: `python3 -m pytest tests/unit/test_design_registry.py -q && git diff --check`  
Expected: 9 passed；无 whitespace 错误。

## Task 2: Eastmoney 来源规范化

**Files:**
- Create: `webapp/investment_news_sources.py`
- Test: `tests/unit/test_investment_news_sources.py`

**Interfaces:**
- `EastmoneyNewsSource(global_fetcher=None, stock_fetcher=None)`；默认惰性导入 `akshare` 并使用 `stock_info_global_em()`、`stock_news_em(symbol=code)`；测试注入 DataFrame 工厂。
- `fetch_market() -> list[NewsItem]`；`fetch_stock(code, name) -> list[NewsItem]`；`fetch_for_watchlist(entries) -> tuple[list[NewsItem], list[dict]]`，后一接口逐证券隔离错误。
- 市场字段：`标题`、`摘要`、`发布时间`、`链接`；个股字段：`新闻标题`、`新闻内容`、`发布时间`、`文章来源`、`新闻链接`、`关键词`。

- [x] **Step 1: 用 DataFrame fixture 先写 source contract 测试**，验证正确映射、空标题跳过、URL 协议校验、时间解析、摘要长度上限、缺字段时明确报错，以及单只股票失败不吞掉其他股票成功数据。

```python
def test_stock_source_normalizes_provider_fields_without_fetching_article_body():
    import pandas as pd
    source = EastmoneyNewsSource(
        global_fetcher=lambda: pd.DataFrame(columns=["标题", "摘要", "发布时间", "链接"]),
        stock_fetcher=lambda symbol: pd.DataFrame([{
            "关键词": "公告", "新闻标题": "公司临时公告", "新闻内容": "来源片段",
            "发布时间": "2026-10-14 10:00:00", "文章来源": "来源甲",
            "新闻链接": "https://example.test/story/1",
        }]),
    )

    items = source.fetch_stock("600900", "长江电力")

    assert items[0].title == "公司临时公告"
    assert items[0].snippet == "来源片段"
    assert items[0].symbols == ("600900",)
```

- [x] **Step 2: 运行目标测试确认 RED**。

Run: `python3 -m pytest tests/unit/test_investment_news_sources.py -q`  
Expected: `ModuleNotFoundError`。

- [x] **Step 3: 实现适配器**。数据源模块内不导入 server；只在默认 fetcher 第一次调用时 `import akshare as ak`。将 AKShare 函数 globals 中的 `requests` 换为局部 timeout proxy（连接 5s/读取 20s，不修改全局 requests）；解析时间为 `Asia/Shanghai`，存 UTC ISO 时间；只保留 HTTP/HTTPS 原文 URL；移除 HTML 标签、控制字符和空内容；来源片段最多 1,200 字；从 `文章来源` 填个股来源名，市场流标为“东方财富市场快讯”。

- [x] **Step 4: 运行来源测试确认 GREEN**。

Run: `python3 -m pytest tests/unit/test_investment_news_sources.py -q`  
Expected: 所有规范化和失败隔离测试通过；测试不触网。

## Task 3: 采集服务、运行状态、去重与保留

**Files:**
- Create: `webapp/investment_news.py`
- Test: `tests/unit/test_investment_news.py`、`tests/unit/test_investment_news_digest.py`

**Interfaces:**
- `InvestmentNewsService(store, source, ai_client, stock_name_lookup, clock=...)`；方法 `submit_collection(trigger) -> dict`、`submit_digest(trigger, force=False) -> dict`、`collect_now(trigger) -> dict`、`generate_digest(day, force=False) -> dict`、`shutdown()`、`status()`。
- 运行由服务自己的 `ThreadPoolExecutor(max_workers=2)` 执行，与财报 `TaskManager` 分离。每类最多一个活动 run；重复提交返回活动 run ID。

- [x] **Step 1: 先写 service 测试**：重复采集返回同一活动 run；每只自选股部分失败仍保留成功项目并记录 partial；手动采集从不触发 AI；失败保留数据库旧条目；单日输入 hash 稳定且内容变化时更新。

```python
def test_manual_collection_never_generates_digest(tmp_path):
    from tests.unit.investment_news_fixtures import RecordingAIClient, make_service
    ai = RecordingAIClient()
    service, store = make_service(tmp_path, ai_client=ai)

    result = service.collect_now(trigger="manual")

    assert result["status"] == "succeeded"
    assert ai.calls == []
```

- [x] **Step 2: 运行测试确认 RED**。

Run: `python3 -m pytest tests/unit/test_investment_news.py -q`  
Expected: `ModuleNotFoundError`。

- [x] **Step 3: 实现 service**。采集快讯一次，再逐个获取 watchlist，收集每个代码的异常到 source result 列表，不让一个代码失败终止全批；原子保存成功条目、run 明细和采集水位。操作活动 ID 由锁保护。run 的状态严格使用 `queued/running/succeeded/partial/failed/skipped`。`daily_input_hash` 对当天条目的 ID、文本、发布时间、来源链接、类别及关联代码做 SHA-256。每天采集和页面请求前做 90 天清理。

- [x] **Step 4: 运行 service 测试确认 GREEN**。

Run: `python3 -m pytest tests/unit/test_investment_news.py -q`  
Expected: 所有 run 状态、手动隔离、部分成功、hash 测试通过。

## Task 4: AI 日综述与引用校验

**Files:**
- Modify: `webapp/investment_news.py`
- Test: `tests/unit/test_investment_news_digest.py`

**Interfaces:**
- `generate_digest(day, force=False)` 只读取 store，不调用 source fetch。
- JSON payload：`market_themes: list[{text, item_ids}]`、`policy_regulatory: list[...]`、`watchlist: list[...]`、`risks: list[...]`、`input_count`、`source_count`、`data_through`。
- 每个 item 输入包含内部 ID、来源、发布时间、标题、最多 1,200 字片段、URL、类别与关联股票。按 24,000 字符分 batch；每个 batch 对应明确的 item ID 覆盖集，最终调用只传 batch 结果和保留的 ID 映射。

- [x] **Step 1: 先写 AI digest 测试**：手动综述不采集；自动相同 hash 标记 skipped；手动 force 即使 hash 不变仍调用 AI；所有当天 item IDs 都进入任一 batch；越权 citation ID 被剔除并使无有效引用的事实不保存；AI 失败保留旧综述。

```python
def test_manual_digest_uses_all_same_day_items_without_fetching(tmp_path):
    from tests.unit.investment_news_fixtures import FakeNewsSource, RecordingAIClient, make_item, make_service
    items = [make_item(n) for n in range(55)]
    source = FakeNewsSource(market_items=items)
    ai = RecordingAIClient()
    service, store = make_service(tmp_path, source=source, ai_client=ai)
    service.collect_now(trigger="manual")
    source.calls.clear()

    digest = service.generate_digest("2026-10-14", force=True)

    assert source.calls == []
    assert digest["input_count"] == 55
    assert {item.id for item in items} <= ai.seen_item_ids
```

- [x] **Step 2: 运行目标用例确认 RED**。

Run: `python3 -m pytest tests/unit/test_investment_news.py -q -k digest`  
Expected: 新 digest 用例失败，因为 generator 未实现。

- [x] **Step 3: 实现摘要 pipeline**。使用当前 `AIClient.chat(messages=..., system=..., temperature=0.2, max_tokens=4096, response_format={"type":"json_object"})`。Prompt 明确：只按输入事实归纳；不能推断股价涨跌因果/买卖建议；watchlist 要点须引用关联股票条目；不得将同题转载描述为独立佐证；要点携带输入 ID。先分批生成有引用的要点，再最终合成并再次校验 ID 全属于输入。序列化 JSON 校验失败或无有效事实引用时 run 失败且保留旧综述。自动运行仅当当天 hash 变化或当天没有成功综述时调用；手动强制调用。

- [x] **Step 4: 运行 digest 用例确认 GREEN**。

Run: `python3 -m pytest tests/unit/test_investment_news.py -q -k digest`  
Expected: 所有摘要隔离、全量输入清单、引用/失败行为通过。

## Task 5: Asia/Shanghai 调度器与 API 生命周期

**Files:**
- Create: `webapp/investment_news_scheduler.py`
- Modify: `webapp/server.py`
- Create: `tests/unit/test_investment_news_scheduler.py`
- Create: `tests/unit/test_investment_news_api.py`

**Interfaces:**
- `InvestmentNewsScheduler(service, store, clock, wait)`；`start()`、`stop()`、`run_once(now)`。
- GET `/api/investment-news`、`/watchlist`、`/runs`、`/digest?day=`、`/status`；POST `/collect`、`/digest`、`/watchlist`；DELETE `/watchlist/{code}`。
- POST run 请求返回 `202` 和 `run_id/status`; `GET /digest` 返回当日综述和当前输入是否变化；读取 API 对日期、种类、股票代码、分页值做校验，page size 限定 `1..20`。

- [x] **Step 1: 先写 scheduler 单测**：09 到 16 的整点 collect slots、仅 09/16 auto digest、总结先于/后于采集次序正确、窗口内启动补采但不生成 digest、同一持久化 slot 不重复触发、窗口外不 catch-up。

```python
def test_startup_catchup_does_not_generate_automatic_digest(tmp_path):
    from datetime import datetime

    class RecordingScheduleService:
        def __init__(self):
            self.collection_triggers = []
            self.digest_triggers = []
        def submit_collection(self, trigger):
            self.collection_triggers.append(trigger)
        def submit_digest(self, trigger, force=False):
            self.digest_triggers.append(trigger)

    service = RecordingScheduleService()
    store = InvestmentNewsStore(str(tmp_path / "news.sqlite3"))
    scheduler = InvestmentNewsScheduler(
        service=service, store=store,
        clock=lambda: datetime.fromisoformat("2026-10-14T11:20:00+08:00"),
        wait=lambda event, seconds: None,
    )

    scheduler.run_once(scheduler.clock(), startup=True)

    assert service.collection_triggers == ["startup_catchup"]
    assert service.digest_triggers == []
```

- [x] **Step 2: 运行 scheduler/API 测试确认 RED**。

Run: `python3 -m pytest tests/unit/test_investment_news_scheduler.py tests/unit/test_investment_news_api.py -q`  
Expected: 新模块导入失败。

- [x] **Step 3: 实现 scheduler**。用 `threading.Event` 可中止循环；`ZoneInfo("Asia/Shanghai")` 计算下一 slot，注入时钟以避免 sleep 测试。启动时在窗口内提交一次 `startup_catchup`；该触发永不调用自动 digest。计划时段 ID (`YYYY-MM-DDTHH:00+08:00`) 写入 `schedule_slots`，成功写入后不再重复。整点 09/16 在 scheduled collection 完成后再按输入 hash 生成 digest。

- [x] **Step 4: 实现 API 与 FastAPI 生命周期**。在 `server.py` 增加全局 service factory 和测试注入点；startup 初始化 store/service/scheduler，shutdown 先停止 scheduler 再 shutdown service。用既有 `stock_index.company_name/is_valid_code` 校验 A 股代码，未就绪返回 503，watchlist 达 20 返回 409。所有来源/AI 网络工作通过 service worker 执行，不在 request handler 内阻塞网络。

- [x] **Step 5: 运行目标测试确认 GREEN**。

Run: `python3 -m pytest tests/unit/test_investment_news_scheduler.py tests/unit/test_investment_news_api.py -q`  
Expected: schedule matrix、启动回补、路由参数/错误、活动 run 复用、手动动作隔离测试通过。

## Task 6: 投资资讯页面

**Files:**
- Modify: `webapp/static/index.html`
- Modify: `webapp/static/app.js`
- Modify: `webapp/static/style.css`
- Create: `webapp/static/investment_news.js`
- Modify: `webapp/server.py` (`_render_index()` asset mtime list)
- Test: `tests/unit/test_investment_news_ui.py`

**Interfaces:**
- hash route `#/investment-news` 调用 `window.initInvestmentNewsPage()`。
- HTML IDs：`investment-news-page`、`news-digest`、`news-list`、`news-watchlist`、`news-collect`、`news-generate-digest`、`news-status`。
- `investment_news.js` 只用 `textContent`/DOM constructors 输出 provider title/snippet，不插入 API 提供的 HTML；来源链接用 `target="_blank" rel="noopener noreferrer"`。

- [x] **Step 1: 先写 HTML/API 页面契约测试**：`tests/unit/test_investment_news_ui.py` 用 `server._render_index()` 断言导航链接、页面容器、两个独立按钮和版本化脚本；用 DOM render 测试确认外部标题/片段进入 `textContent` 而非 `innerHTML`。

```python
def test_index_has_investment_news_route_and_separate_actions(client):
    html = client.get("/").text
    assert 'href="#/investment-news"' in html
    assert 'id="news-collect"' in html
    assert 'id="news-generate-digest"' in html
    assert "/static/investment_news.js?v=" in html
```

- [x] **Step 2: 运行 HTML/API 契约测试确认 RED**。

Run: `python3 -m pytest tests/unit/test_investment_news_ui.py -q`  
Expected: 由于导航、页面容器和版本化资源未实现而失败。

- [x] **Step 3: 实现语义化页面和原生 JS**。复用项目浅色 token、字体、间距和卡片样式；桌面双栏、手机单栏；日期/资讯类别/自选股票筛选，解释性的自选股关联提示，按提示优先或最新排序，20 条服务端分页，自选添加/删除，独立任务状态轮询，空态/错误/综述输入变化提示，来源时间/采集时间与原文链接。来源片段严格 `textContent` 转义；生成中的操作用 `aria-live="polite"` 暴露状态；键盘焦点可见，移动端无横向溢出。

- [x] **Step 4: 加入资源版本刷新**。将 `investment_news.js` 加入 `_render_index()` 的 `versioned_assets`，并确认脚本加载顺序保证 router 调用前 `window.initInvestmentNewsPage` 可用。

- [x] **Step 5: 运行 HTML/API 契约测试确认 GREEN**。

Run: `python3 -m pytest tests/unit/test_investment_news_ui.py -q`  
Expected: 导航、页面节点、独立操作和文本安全测试通过。

## Task 7: 聚焦回归、真实页面 QA 和文档

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/DESIGN-REGISTRY.md`
- Modify: `docs/FEATURE-CATALOG.md` (only after local merge)
- Test: all news unit tests and related `tests/unit/test_server_api.py`; browser QA uses the test-only app launcher.

- [x] **Step 1: 跑聚焦后端/页面回归**。

Run: `python3 -m pytest tests/unit/test_investment_news_store.py tests/unit/test_investment_news_sources.py tests/unit/test_investment_news.py tests/unit/test_investment_news_digest.py tests/unit/test_investment_news_scheduler.py tests/unit/test_investment_news_api.py tests/unit/test_investment_news_ui.py tests/unit/test_server_api.py tests/unit/test_design_registry.py -q`  
Expected: 258 passed（含 9 项设计台账回归），3 项第三方弃用警告；`node --check`、`compileall`、`git diff --check` 均通过。

- [x] **Step 2: 跑前端静态检查与完整 browser QA**。

Run: `node --check webapp/static/investment_news.js && python3 -m pytest tests/unit/test_investment_news_ui.py -q`  
Expected: JavaScript 语法通过；2 项页面契约测试通过。实际 browser QA 在 desktop/tablet/mobile 无控制台/页面错误及 4xx/5xx；交互操作经 fake source/AI 验证独立。

- [x] **Step 3: 对本地应用运行 browser QA**。使用 `tests/browser/investment_news_test_app.py` 以隔离临时 SQLite、假来源和假 AI 启动真实 FastAPI 应用；在 `http://127.0.0.1:8765/#/investment-news` 执行 desktop/tablet/mobile QA，页面错误/控制台为零、4xx/5xx 为零；390px 下 `document.documentElement.scrollWidth == 390`。手动点击验证采集只调用假 source、综述只调用假 AI；不触碰真实 Eastmoney 或 AI 凭证。

- [ ] **Step 4: 合入后更新 README 和功能目录**。按仓库规则仅在本地合入后说明实际可见能力、Asia/Shanghai 调度、手动动作独立、90 天保留、Eastmoney 来源及个人自用边界；功能目录记录合入短 SHA、入口、不变量与验证结果。设计台账保持实施中直到合并证据写入。

- [ ] **Step 5: 最终检查并提交**。

Run: `git diff --check && git status --short && git diff --stat`  
Expected: 变更仅在本 feature worktree；无用户 root 未提交文件混入；只提交代码、测试与文档，不提交 SQLite、日志、截图或浏览器认证状态。

- [ ] **Step 6: 验证后本地合入**。运行适用测试全部通过后，将 feature branch fast-forward 合入本地 `main`，更新 `FEATURE-CATALOG.md` 和设计台账合并短 SHA/验证证据；不推送远端。
