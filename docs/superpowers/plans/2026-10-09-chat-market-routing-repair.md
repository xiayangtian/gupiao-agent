# 智能问答市场链路修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This header is not delegation permission; implement directly unless the operator explicitly authorizes subagents.

**Goal:** 让大盘与个股行情问句在澄清、时间解析、来源失败和终态阶段保持同一受控市场任务，杜绝无行情却全库财报兜底并宣称完成。

**Architecture:** 服务端冻结 `ResolvedRequest`（对象、意图、窗口、Scope、来源）；`ChatStore` 只保存一次性待补槽位；市场规划仅在授权的来源和调用预算中构造计划，失败时使用同意图确定性计划或诚实不可用；终态由匹配的行情证据覆盖决定，而非由“没有数字可反驳”决定。SSE 与旧记录向后兼容。

**Tech Stack:** 项目现有 Python、FastAPI/Starlette SSE、pytest、原生 JSON `ChatStore`、腾讯行情适配器与可选 MCP；不增加默认依赖。

**Spec:** `docs/superpowers/specs/2026-10-09-chat-market-routing-repair-design.md`（按 2026-10-09 已确认口径执行）。

## Global Constraints

- **本计划是待确认的实施方案，不是编码、合并、推送或真实来源探测授权。** 执行前先在项目设计台账标记实施中；如属于需改代码并合入分支的执行请求，先建立全局任务台账，隔离工作区从最新 `main` 创建功能分支，保留现存未跟踪文件，不修改运行中的 8000 服务。
- “近期/近5日”市场走势默认最近 **5 个实际交易日**，以上海时区冻结请求时刻、只用返回的有日期数据；答复和首帧披露默认口径及实际覆盖。财报的“近期”不得复用市场默认。
- 明确大盘请求在**现有**授权及预算内尝试可用的 MCP 广度；四指数最多 4 次、概览最多 3 次、市场最多 7 次、整轮最多 9 次、网页最多 2 次，配置更低则从低，**不得提高配置或 `ToolPolicy` 的既有上限**。如果现行公司行情链路只准 1 次，趋势使用一次 K 线，不擅自加报价。
- P1 Scope/来源授权与物理计数/去重、P2 时间/复盘口径、P3 120/300 秒、4 workers、64 SSE 缓冲与取消/研究恢复、P4 证据窗口/离线评分均不回退；不把真实 MCP 故障作为既成结论。
- 默认验证只跑脱敏固定替身的相关单测、SSE/浏览器直接影响回归；出站网络、真实行情/MCP/网页、财报下载及生产账号均禁用，真实来源验证单独授权。未验证不得标记完成、合并或推送。
- UX 使用现有 `clarification`、`scope_resolved`、`execution_plan`、`done` 事件；若实际改变界面，先按 `ui-ux-pro-max`/`ui-styling` 检查语义、可见焦点、键盘、对比度 ≥4.5:1 和移动端无横溢；浏览器按项目约定做 QA。旧会话缺待补字段视为无状态，不能从助手自由文本反推授权。

## 文件责任图与跨任务合同

- `webapp/chat_policy.py`：单轮确定性 `QuestionRoute`；`webapp/chat_time.py`：解析上海时间的 `MarketWindow` 并选带日期的实际交易日，不负责权限。
- **新增** `webapp/chat_request_resolution.py`：定义 `ResolvedRequest` / `Clarification`；`resolve_chat_request(question: str, scope_request: ScopeRequest, pending: Mapping[str, Any] | None, now: datetime, availability: object) -> ResolvedRequest | Clarification` 只用当前输入/受控槽位，**不能查会话助手文本**。`ResolvedRequest` 包含 `route: QuestionRoute`、`scope: Scope | None`（公司识别在服务器现有 resolver 完成后冻结）、`effective_question: str`、`market_window: MarketWindow | None`、`window_origin: str`、`allowed_source_kinds: frozenset[str]`；调用侧继续用 `_resolve_scope` 得到 Scope，不同时维护两套公司解析。`Clarification` 包含 `message`、`missing_field`、可信 `subject_code?`、`intent?`。
- `webapp/chat_store.py`：会话可选 `pending_clarification`，新增 `save_pending(sid, pending)`, `consume_pending(sid, *, now, expected_version) -> dict | None`, `clear_pending(sid)`；全部在单个现有 `RLock` 和 `_save()` 内读写，只返回深拷贝；只保留一个、15 分钟/下一用户轮限制。对话消息仍用 `append_turn`/`append_messages`，不把 token 或模型推理写入槽位。
- `webapp/server.py`：一个入口先用受控解析与槽位构建 `effective_question`，冻结 Scope、意图、窗口、策略；预检退出原子落盘待补；市场请求无计划时不进入 RagQA 的默认财报路径；在所有调用/最终核验阶段沿用冻结合同。
- `webapp/execution_plan.py` / `execution_planner.py`：验证每个市场意图所需的至少一个行情来源及成本，并提供 `safe_market_plan(intent, available_kinds, max_calls)`，仅在合法授权/预算中生成市场专用计划，不生成 `retrieve`。`webapp/source_adapters.py` / `source_runtime.py` 继续执行最终授权/计数；`webapp/chat_execution.py` / `chat_verifier.py` 按市场来源与实际日期判定状态，不改旧财务规则。
- `tests/unit/test_chat_policy.py`、`test_chat_time.py`、**新增** `test_chat_request_resolution.py`、`test_chat_store.py`、`test_chat_execution_plan.py`、`test_source_runtime.py`、`test_chat_execution.py`、`test_server_api.py`：对应组件和端到端回归；如用 P4 runner，加 `tests/fixtures/chat_runtime_eval_cases.json` 和 `test_chat_runtime_eval_runner.py`，不改固定输出侧车。
- `docs/superpowers/DESIGN-REGISTRY.md`：本阶段待实现/实施中/合入证据；`docs/FEATURE-CATALOG.md` 与 `README.md` 仅**合入时**按实际用户能力变化更新，不在未合入计划中写“已实现”。

### Task 1：单轮路由与交易日窗口（可独立审查）

**Files:** Modify `webapp/chat_policy.py`, `webapp/chat_time.py`; Test `tests/unit/test_chat_policy.py`, `tests/unit/test_chat_time.py`.

**Interfaces:** `QuestionRouter.classify(question) -> QuestionRoute`；`resolve_market_window(question, now) -> MarketWindow | None`；`select_market_bars(rows, window) -> WindowSelection`。不改变 `resolve_financial_period` 或财报路由。

- [ ] **Step 1 RED：** 在 `test_chat_policy.py` 添加参数化断言：`分析今日股票行情`、`今日大盘走势`、`A股指数行情` → `market_recap/requires_company=False`；`分析福耀玻璃近期行情` → `market_trend/True/True`；`农业银行近三年营收趋势` → `financial_trend`；孤立 `近五交易日` → `clarification`；显式研究任务仍走现有服务端研究判别。`test_chat_time.py` 用冻结上海时间断言 `近期`、`近5日`、`近五交易日` 均返回五交易日；财报问句不应用该窗口；未来/逆序日期必须拒绝。例如：

```python
from datetime import datetime
from zoneinfo import ZoneInfo
from webapp.chat_policy import QuestionRouter
from webapp.chat_time import resolve_market_window

def test_broad_market_does_not_require_company():
    route = QuestionRouter().classify('分析今日股票行情')
    assert (route.intent, route.requires_company) == ('market_recap', False)

def test_recent_company_market_window_is_five_actual_bars():
    now = datetime(2026, 10, 9, 11, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
    window = resolve_market_window('分析福耀玻璃近期行情', now)
    assert (window.kind, window.trading_days) == ('rolling_trading_days', 5)
```
- [ ] **Step 2 RED：** `python3 -m pytest -q tests/unit/test_chat_policy.py tests/unit/test_chat_time.py`；确认至少大盘误判、近期未解析、孤立短句漏澄清的目标失败，不把导入/fixture 错误当红灯。
- [ ] **Step 3 GREEN：** 在 `classify()` 的公司市场分支之前识别明确全市场主语（且优先于泛词“股票”），在非研究/非财务上下文里识别孤立时间短句为澄清；在 `resolve_market_window` 的显式日期之后、返回 `None` 之前加入有界别名归一，不为财务问题造市场窗口；若已识别时间片段但日期无效，保持拒绝而非改用“近期”。现有 `select_market_bars()` 保留实际日期和不足五天的 `partial`。
- [ ] **Step 4 GREEN：** 同命令复跑，且 `python3 -m pytest -q tests/unit/test_chat_scope.py` 保障旧 Scope；逐条确认路由表。`git add webapp/chat_policy.py webapp/chat_time.py tests/unit/test_chat_policy.py tests/unit/test_chat_time.py && git commit -m 'fix: distinguish broad market questions and bounded trading windows'`。

### Task 2：一次性待补槽位与原子消费

**Files:** Modify `webapp/chat_store.py`; Test `tests/unit/test_chat_store.py`. 仅服务器信任槽位；此任务不接入 SSE。

**Interfaces:** `save_pending(sid, pending)` 记录 `version`（不可猜的随机 ID）、`created_at`（UTC aware ISO）、`missing_field`（`subject|window|kind`）、`intent`、`company_code`（如已唯一验证）、`question`（原始已持久化用户输入的引用优先）；`consume_pending(sid, *, now, expected_version)` CAS 返回拷贝并**删除**；`clear_pending(sid)` 删除。解析不合法/过期/未知会话统一 `None`，不能继承；创建消息与保存槽位在同一临界区/一次写盘，避免持久化半成品。

- [ ] **Step 1 RED：** 固定 `now=datetime(2026,10,9,10,0,tzinfo=timezone.utc)` 创建独立 `ChatStore(tmp_path/'sessions.json')`：保存后重建 store 可读；消费一次、16 分钟过期、错误版本/外会话/并发 `Barrier` 消费至多一次；删除会话/重开不得继承，旧 JSON 无字段仍可读。例如：

```python
from datetime import datetime, timezone
from webapp.chat_store import ChatStore

def test_pending_slot_is_one_shot_across_restart(tmp_path):
    path = str(tmp_path / 'sessions.json')
    store = ChatStore(path)
    sid = store.create_session()['id']
    now = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
    store.save_pending(sid, {'version': 'server-token', 'created_at': now.isoformat(),
                             'missing_field': 'window', 'intent': 'market_trend',
                             'company_code': '600660', 'question': '福耀玻璃股价走势'})
    reopened = ChatStore(path)
    assert reopened.consume_pending(sid, now=now, expected_version='server-token')['company_code'] == '600660'
    assert reopened.consume_pending(sid, now=now, expected_version='server-token') is None
```
- [ ] **Step 2 RED：** `python3 -m pytest -q tests/unit/test_chat_store.py -k 'pending or old_session'`，目标失败为方法不存在或 CAS 未生效。
- [ ] **Step 3 GREEN：** 在现有 `RLock` 下寻找会话并做 schema 白名单校验、TTL/版本校验、清除与 `_save()`；为预检新增 `append_clarification(sid, question, reply, pending)`，同锁将两条消息与槽位一起保存；不要额外泄露待补对象到 `list_sessions()`。避免直接拼接模型产物，待补仅来自服务端 `Clarification`。
- [ ] **Step 4 GREEN：** 复跑 `test_chat_store.py`；复核删除/重命名/旧 session 的既有用例。`git add webapp/chat_store.py tests/unit/test_chat_store.py && git commit -m 'feat: persist and atomically consume a single chat clarification slot'`。

### Task 3：冻结解析合同并接通预检/澄清

**Files:** Create `webapp/chat_request_resolution.py`, `tests/unit/test_chat_request_resolution.py`; Modify `webapp/server.py`, `tests/unit/test_server_api.py`.

**Interfaces:** 从 Task 1 的路由/窗口与 Task 2 的 `consume_pending` 生成 `ResolvedRequest | Clarification`；返回 `effective_question` 仅由可信槽位和当前回答组成（例如已验证公司代码对应原请求＋“近五交易日”），**不能从历史助手回复或其他会话借权限**。`scope` 在 `_resolve_scope` 成功后绑定一次并传后续阶段；显式 `focus_report`/`scope_mode` 冲突则澄清。TTL 过期与新主题清槽；任何补答最多取一次槽。

- [ ] **Step 1 RED：** 纯函数测试：大盘今日、个股近期、孤立时间句、匹配槽位、当前改口大盘、财报近三年。API 测试用 TestClient + 隔离 ChatStore：预检“福耀玻璃股价走势”得到 `clarification`、`done.run is None`，后续“近五交易日”必须产生市场计划或明确无来源，绝不查询 PDF；旧会话无 pending 的短句仅澄清。纯函数边界样例：

```python
from datetime import datetime, timezone
from webapp.chat_request_resolution import Clarification, resolve_chat_request
from webapp.chat_scope import ScopeRequest

def test_orphan_market_window_requires_subject():
    result = resolve_chat_request('近五交易日', ScopeRequest.auto(), None,
                                  datetime(2026, 10, 9, tzinfo=timezone.utc), object())
    assert isinstance(result, Clarification)
    assert result.missing_field == 'subject'
```
- [ ] **Step 2 RED：** `python3 -m pytest -q tests/unit/test_chat_request_resolution.py tests/unit/test_server_api.py -k 'clarification or market_resolution'`；确认失败在边界而非真实 AI/MCP。
- [ ] **Step 3 GREEN：** 在 `_chat_stream_impl` 中只替换入口解析段：`route=...; resolution=resolve_chat_request(...); if Clarification: append_clarification(...); return SSE`；有 pending 时先 CAS，冲突/过期退回澄清；Scope 仍由 `_resolve_scope` 唯一判定。后续 `ExecutionPlanner`、retrieve/搜索 handler、`answer_from_context`、`ClaimVerifier` 用 `effective_question` / 冻结窗口，不把当前孤立补答交给旧 `IntentRouter` 重新分类；持久化的原始 `body.question` 不变。最小变更原有 `_chat_preflight_response` 保留 `session/clarification/delta/done.run=None` 兼容。
- [ ] **Step 4 GREEN：** 上述测试和 `python3 -m pytest -q tests/unit/test_chat_scope.py tests/unit/test_chat_policy.py`；追加并发 API 补答仅一次取数、焦点报告冲突、取消/截止时间不残留授权的回归。`git add webapp/chat_request_resolution.py webapp/server.py tests/unit/test_chat_request_resolution.py tests/unit/test_server_api.py && git commit -m 'feat: carry bounded market clarification into one resolved request'`。

### Task 4：市场计划合同、MCP 门控和安全降级

**Files:** Modify `webapp/execution_plan.py`, `webapp/execution_planner.py`, `webapp/server.py`, `webapp/chat_policy.py`（仅修正误导的市场 fallback 文案）, possibly `webapp/chat_execution.py` for policy-compatible budget; Test `tests/unit/test_chat_execution_plan.py`, `test_execution_plan_recap.py`, `test_execution_market_recap.py`, `test_server_api.py`, `test_source_runtime.py`.

**Interfaces:** `safe_market_plan(intent: str, available_kinds: set[str], max_external_calls: int) -> ExecutionPlan | None` 对 quote 必需 `market_quote`、trend 必需 `market_kline`、recap 必需 `market_indices`（4 次），可用/授权/剩余额度允许时加 `market_overview`（3 次）；返回前复用 `validate_execution_plan`。模型计划应至少含相应必需步骤，拒绝 answer-only、异意图 source_mode、retrieve、预算超限。对 recap 仅四指数可用时计划是合法**降级计划**但最终 `partial`。

- [ ] **Step 1 RED：** `test_chat_execution_plan.py` 表驱动验证 quote/trend/recap `answer` 单步、仅 web、用 PDF 代替 K 线、cost=7 > cap=6 全部拒绝；`test_execution_plan_recap.py` 验证三个预算档。API 测试 planner 异常或非法 JSON 后仍保持市场意图，`retrieve`/补报 0 次；MCP 关闭、熔断、清单或白名单缺项不提供 overview。例如：

```python
from webapp.execution_planner import safe_market_plan

def test_market_recap_fallback_respects_costs():
    kinds = {'market_indices', 'market_overview'}
    assert [step.kind for step in safe_market_plan('market_recap', kinds, 7).steps] == [
        'market_indices', 'market_overview', 'answer']
    assert [step.kind for step in safe_market_plan('market_recap', kinds, 4).steps] == [
        'market_indices', 'answer']
    assert safe_market_plan('market_recap', kinds, 3) is None
```
- [ ] **Step 2 RED：** `python3 -m pytest -q tests/unit/test_chat_execution_plan.py tests/unit/test_execution_plan_recap.py tests/unit/test_server_api.py -k 'market_plan or overview_gate'`；目标失败是接受空市场计划或错误宣告 MCP 可用。
- [ ] **Step 3 GREEN：** `server.py` 的 `available_kinds` 必须取自配置+`SourceAccess` 对固定三项 `market_recap_tool_calls` 的逐项许可、熔断和**实际实时定义**，不能只看 `body.use_mcp`；`market_indices`/quote/kline 也需腾讯启用与成本可用。模型 planning 失败时调用 `safe_market_plan`；`None` 时在进入 `_produce` 前返回受控不可用，绝不调用 `RagQA.answer_stream` 默认财报路径。`_produce` 的受控计划只执行允许的 market handlers；即使工具状态执行前变化，`SourceRuntime` 仍做最终授权/物理预算。`ToolPolicyResolver` 市场文案改“行情来源暂不可用，无法核实该行情”，且市场来源授权与本地 PDF 权限分离；不要扩大 `max_calls` 或模型可见工具集。
- [ ] **Step 4 GREEN：** 跑本任务列出的五个测试文件（只对直接改动回归）；用替身统计物理 `SourceRuntime.call` 最多 4+3、网页最多 2、总不超 min(config,9)，授权关闭时 0 次实际 MCP；保留已有效指数且不伪称概览。`git add webapp/execution_plan.py webapp/execution_planner.py webapp/server.py webapp/chat_policy.py webapp/chat_execution.py tests/unit/test_chat_execution_plan.py tests/unit/test_execution_plan_recap.py tests/unit/test_execution_market_recap.py tests/unit/test_server_api.py tests/unit/test_source_runtime.py && git commit -m 'fix: keep market plans inside authorized source budgets'`（只 add 实际改动文件）。

### Task 5：行情证据充分性与诚实终态

**Files:** Modify `webapp/chat_execution.py`, `webapp/server.py`, `webapp/chat_verifier.py`（仅如需冻结请求窗口对齐）, `webapp/chat_models.py`（仅需要可选安全诊断码时）； Test `tests/unit/test_chat_execution.py`, `test_chat_verifier.py`, `test_source_adapters.py`, `test_server_api.py`.

**Interfaces:** 新增 `market_task_status(intent: str, scope: Scope, window: MarketWindow, sources: Sequence[SourceResult], verification: str | None) -> Literal['completed','partial','failed']`，只认同一公司或四个预期指数的带日期 `market` 证据，尊重 `SourceCoverage` 的查询/实际窗口、`SourceResult.status`，不把 `ClaimVerifier` 的空事实 `passed` 当充分性；在原 `resolve_answer_status` 计算之后收紧市场状态，永远不升级 stopped/waiting/failed。无任何可用有效行情→`failed`；5 天仅 2 天→`partial`；只四指数但缺概览→`partial`；无日期/未来日期/不同公司/概览数据不在请求日→不可宣称覆盖。部分答复附来源实际日期和缺失维度，不能基于缺失来源推断涨跌。

- [ ] **Step 1 RED：** 构造 `SourceResult` 替身，逐项断言“五日五条且同公司、验核 passed→completed；两日→partial；四指数成功但概览失败→partial；全部失败/只有 PDF/空 answer 且 verifier passed→failed；错公司/无 as_of/今日前一日伪装今日→failed”；一条未支持数字触发原 blocked/降级。复用 `test_chat_execution.py` 现有 `Scope`/`SourceResult` 工厂；核心断言：

```python
from webapp.chat_execution import market_task_status

def test_empty_market_evidence_cannot_complete(company_scope, five_day_window):
    assert market_task_status('market_trend', company_scope, five_day_window, (), 'passed') == 'failed'
```
- [ ] **Step 2 RED：** `python3 -m pytest -q tests/unit/test_chat_execution.py tests/unit/test_chat_verifier.py tests/unit/test_server_api.py -k 'market_status or market_evidence'`，应定位当前空事实仍 completed 的失败。
- [ ] **Step 3 GREEN：** 把 `SourceResult` 的请求/返回覆盖与选中日期传给 `market_task_status`；先让 `ClaimVerifier` 对有数值论断照常核验，再按市场任务证据收紧最终状态与可见答复。避免仅在最终状态改字：`answer_from_context` 无行情时不应生成貌似完成的走势结论，用固定诚实说明替代；已得部分市场数据只说明实际覆盖。可选诊断码是新字段，旧 `AnswerRun.from_dict` 缺字段时保留旧语义。
- [ ] **Step 4 GREEN：** `python3 -m pytest -q tests/unit/test_chat_execution.py tests/unit/test_chat_verifier.py tests/unit/test_source_adapters.py tests/unit/test_server_api.py`；检查 `done.run.status` 与持久化一致、`source_result` 从未混入财报。`git add webapp/chat_execution.py webapp/server.py webapp/chat_verifier.py webapp/chat_models.py tests/unit/test_chat_execution.py tests/unit/test_chat_verifier.py tests/unit/test_source_adapters.py tests/unit/test_server_api.py && git commit -m 'fix: require matching market evidence before completing chat'`（只 add 实际改动文件）。

### Task 6：故障输入真实 SSE 离线回放与直接影响的界面验证

**Files:** Modify `tests/unit/test_server_api.py`, `tests/fixtures/chat_runtime_eval_cases.json`（若现有 schema 可表达此类 case）, `tests/unit/test_chat_runtime_eval_runner.py`; optionally `tests/browser/chat_reliability_p3_app.py`, `tests/browser/test_chat_reliability_p3.py`；仅确实需要显示新提示时修改 `webapp/static/app.js` / `chat_rendering.js` 及对应 JS 单测。

**Interfaces:** 在隔离 TestClient/ChatStore 与固定模型、腾讯、MCP、网页替身下顺序提交现场四个输入，**新版本前三轮独立走大盘/个股行情，第四轮在无待补槽位时只澄清**；另用真正的“福耀玻璃股价走势→近五交易日”完成一次性补答。观察 SSE `session/clarification/run_started/execution_plan/source_result/verification/done` 与持久化 `AnswerRun`、来源次数，禁止依赖模拟的固定答案回放。

- [ ] **Step 1 RED：** 在 `test_server_api.py` 为以上两条真实多轮路径、重复/过期槽位、配置低于 4/7、MCP 清单缺项及 0/2/5 日来源建立表驱动断言；测试 fixtures 默认禁止出站，漏注入模型/行情/MCP/网页即失败。旧财报趋势、知识问答、研究恢复和取消保留现有回归。固定期望表：

```python
EXPECTED = (
    ('分析今日股票行情', 'market_recap', 'market_recap'),
    ('分析福耀玻璃近期行情', 'market_trend', 'external_market'),
    ('分析福耀玻璃近5日行情', 'market_trend', 'external_market'),
)
# 逐轮通过隔离 TestClient.stream 消费 SSE；断言 run.execution_plan['source_mode']
# 分别等于上表第三列、来源调用只在 market、PDF/补报计数始终为零。
```
- [ ] **Step 2 RED：** `python3 -m pytest -q tests/unit/test_server_api.py -k 'market_repair or clarification_market'`，新增端到端场景先失败。
- [ ] **Step 3 GREEN：** 只修 Task 1–5 遗留的接线问题；若不能用现有 runner schema 表达 SSE 场景，就在 `test_server_api.py` 用本文件现有 TestClient fixture，不修改评测 schema 来伪造通过。页面不需新增组件时保留现有澄清消息与输入框；需新文案才改 JS，提示可读、可键盘继续输入，尽量不改样式/布局。
- [ ] **Step 4 GREEN：** 聚焦命令：`python3 -m pytest -q tests/unit/test_chat_policy.py tests/unit/test_chat_time.py tests/unit/test_chat_request_resolution.py tests/unit/test_chat_store.py tests/unit/test_chat_scope.py tests/unit/test_chat_execution_plan.py tests/unit/test_execution_plan_recap.py tests/unit/test_execution_market_recap.py tests/unit/test_chat_execution.py tests/unit/test_chat_verifier.py tests/unit/test_source_adapters.py tests/unit/test_source_runtime.py tests/unit/test_server_api.py tests/unit/test_chat_runtime_eval_runner.py tests/unit/test_design_registry.py`；如改 JS，再跑 `test_chat_rendering_js.py`。隔离本地 Web app 完成后遵项目规则先 `browser_qa` 桌面/平板/移动，最小交互用 `browser_open`/`browser_snapshot` 验证澄清、补答、禁用来源；检查 console/errors/network 和移动横溢，记录**实际 URL/视口**，不触碰当前 8000 的真实数据环境。如有直接相关失败必须修复或如实阻断。
- [ ] **Step 5：** `git diff --check`；按实际变更提交测试或最小 JS 修补（`git add` 仅本任务的修改文件，`git commit -m 'test: replay market clarification over offline chat SSE'`）。生成脱敏验收记录，**不提交**原会话文本、账号认证或浏览器截图。将项目设计台账更新为待验证时仍不是已实现；仅在用户明确授权、测试通过且合入 `main` 后，更新设计台账、功能目录/需要时 README，填入真实合并 SHA/聚焦验证结果。不得默认推送。

## 自检与执行门槛

- 覆盖映射：设计 §4 → Tasks 1/3；§5 → 2/3/6；§6 → 4/6；§7 → 5/6；§8 → 6；§2/§9 的预算、取消、旧记录、无真实 MCP 由 Global Constraints 与 Tasks 2/4/6 保证。
- 未经实施授权：只保留本文件与设计台账的“待实现”，不运行以上 RED/GREEN、不占用真实来源。开始实施时再登记全局任务、更新项目台账为实施中；某步发现来源预算或冻结 Scope 与设计不可兼容，暂停并请用户重新确认，不静默放宽。
- 上线回退：市场计划/证据不可用时仅返回受控不可用/澄清，不恢复 `whole_corpus` PDF fallback；项目功能目录必须由实际合并证据驱动，不能凭本计划宣布交付。
