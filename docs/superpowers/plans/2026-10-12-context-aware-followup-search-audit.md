# 上下文追问与 WebSearch 查询词溯源实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:executing-plans 在当前隔离工作区逐任务执行。每项任务结束后运行其所列测试。

**Goal:** 对上轮展示/来源追问优先使用本会话上一轮安全运行上下文，并让每一次 WebSearch 的实际查询词在本轮来源证据中可见、可追溯。

**Architecture:** 新增有界上一轮上下文投影与窄范围的 context-only follow-up 判定；Server 在计划前用 answer-only `conversation_context` 路径处理图表等展示追问。普通计划接收最近上下文，由受校验执行计划携带可选 search query；实际 WebSearch 调用保存该 query 的脱敏摘要，来源面板显示不同的 query。RAG 工具调用路径继续使用现有参数标准化契约。

**Tech Stack:** Python 3.12, FastAPI, existing `ExecutionPlan`/`SourceRuntime`/`AnswerRun` contracts, vanilla JavaScript evidence renderer.

**Spec:** `docs/superpowers/specs/2026-10-12-context-aware-followup-search-audit-design.md`

## Global Constraints

- 用户已确认：针对上一轮图表/展示结果的解释无需 WebSearch；市场因果问题仍可搜索。
- 当前问题是本轮意图权威输入；仅提供最近一轮安全摘要，不带全量会话/思维链/原始工具返回。
- Context-only 运行不执行计划模型、RAG 检索或任何外部来源；用当前会话前一轮的事实与元数据回答。
- Search query 是参数，不是授权；既有来源授权、预算、计划校验和事实校验保持有效。
- 实际发送的 query 必须以脱敏后的版本为准；持久化每次调用各自有界记录，输出 HTML 前 escape。

---

### Task 1: 上一轮安全上下文投影与窄范围展示追问判定（TDD）

**Files:**
- Create: `webapp/chat_followups.py`
- Create: `tests/unit/test_chat_followups.py`
- Modify: `webapp/chat_request_resolution.py`
- Modify: `tests/unit/test_chat_request_resolution.py`

**Interfaces:**
- `build_previous_turn_context(messages: Sequence[Mapping[str, Any]]) -> PreviousTurnContext | None`：只接受最近一条具有合法 `AnswerRun` 的 assistant 消息及其前一条 user 问题，输出有界问题、回答、scope、执行计划、Facts、source metadata，不输出推理/原始参数。
- `is_context_only_followup(question: str, context: PreviousTurnContext | None) -> bool`：只识别引用上一轮且询问图表/表格/展示/来源等本轮结果呈现的问题；不拦截“股价为什么涨/跌”等行情因果问题。
- `ResolvedRequest.context_only_followup: bool` 默认 false；解析器可接收已构造的上下文，不扫描任意更早 transcript。

- [x] **Step 1: 新增纯函数测试**：验证最近合法 run/context、过长字段截断、缺失/损坏 run 返回 None、排除 reasoning/raw tool arguments、上一轮范围/Fact 可读；验证“这个为什么没画图”命中，“这只股票为什么涨了”不命中，无 prior context 不命中。
- [x] **Step 2: 运行目标测试确认 RED**：`python3 -m pytest tests/unit/test_chat_followups.py tests/unit/test_chat_request_resolution.py -q`；新模块/API 未存在导致目标用例失败。
- [x] **Step 3: 实现安全上下文和判定**：以 `AnswerRun.from_dict` 校验运行合同；从消息数组最近的完整 assistant turn 找其前一个 user turn；各文本字段和 Fact 数量严格设上限；只通过白名单字符串生成模型 context。
- [x] **Step 4: 运行目标测试至 GREEN**：`python3 -m pytest tests/unit/test_chat_followups.py tests/unit/test_chat_request_resolution.py -q`。

### Task 2: Context-only answer 路径与规划上下文（TDD）

**Files:**
- Modify: `webapp/server.py`
- Modify: `webapp/execution_plan.py`
- Modify: `webapp/execution_planner.py`
- Modify: `webapp/execution_executor.py`（仅需改变 search handler 入参时）
- Modify: `tests/unit/test_execution_plan.py`
- Modify: `tests/unit/test_execution_planner.py`
- Modify: `tests/unit/test_execution_executor.py`
- Modify: `tests/unit/test_server_api.py`

**Interfaces:**
- `ExecutionPlan.search_query: str = ''` 是受限可选字段，长度最多 300；它不改变可用工具授权。
- `ExecutionPlanner.plan(..., conversation_context: str = '')` 将不超过上下文限额的历史摘要放入 planner snapshot；prompt 要求当前问题优先、只解析最近指代，并在选择 `web_search` 时生成简短 query。
- `ExecutionPlan.source_mode='conversation_context'` 只允许单步 `answer`；普通 `ExecutionExecutor` 的来源执行不被调用。

- [x] **Step 1: 写规划与 API 回归测试**：planner snapshot 接收 bounded context；有效 `search_query` 保留；超过长度/错误类型被拒；conversation_context 模式拒绝任何检索/外部步骤；“这个为什么没画图”不调用 planner、retrieve、market 或 web callback，RAG 接收含上一轮 Facts/执行信息的合成 history；市场因果追问仍按标准 source plan 搜索。
- [x] **Step 2: 运行精确测试确认 RED**：`python3 -m pytest tests/unit/test_execution_plan.py tests/unit/test_execution_planner.py tests/unit/test_execution_executor.py tests/unit/test_server_api.py -q -k 'conversation_context or search_query or chart_followup or contextual_query'`；新契约和行为断言预期失败。
- [x] **Step 3: 实现受限计划字段与Server路由**：在 plan model/validator 中增加 `search_query` 和 `conversation_context` 单步边界；从当前会话构造 previous context；展示追问在任何 planner/source 之前创建 answer-only 计划，设置无工具 policy、skip retrieval，并把摘要作为一次性、非持久化 history 消息；普通 planner 收到 bounded context。
- [x] **Step 4: 运行目标测试至 GREEN**：`python3 -m pytest tests/unit/test_execution_plan.py tests/unit/test_execution_planner.py tests/unit/test_execution_executor.py tests/unit/test_server_api.py -q -k 'conversation_context or search_query or chart_followup or contextual_query'`。

### Task 3: 执行上下文生成的 WebSearch query（TDD）

**Files:**
- Modify: `webapp/server.py`
- Modify: `webapp/execution_plan.py`
- Modify: `webapp/execution_planner.py`
- Modify: `tests/unit/test_execution_plan.py`
- Modify: `tests/unit/test_execution_planner.py`
- Modify: `tests/unit/test_execution_executor.py`
- Modify: `tests/unit/test_server_api.py`

- [x] **Step 1: 新增 query 选择测试**：计划含 WebSearch 时实际调用使用 `ExecutionPlan.search_query`；没有有效生成词时使用有界回退（无上下文=当前问题；上下文指代=上一轮主体+当前目标）；其他 source handler 参数不变。断言不存在 context-only 调用。
- [x] **Step 2: 运行 query 用例确认 RED**：`python3 -m pytest tests/unit/test_execution_planner.py tests/unit/test_execution_executor.py tests/unit/test_server_api.py -q -k 'search_query or contextual_query'`。
- [x] **Step 3: 接入 planner 输出与搜索 handler**：JSON plan schema/prompt 明确可选 `search_query` 字段；planner snapshot 的上下文仅作指代解析数据；Server 在已校验计划包含 `web_search` 后采用生成词。失败回退只拼接本轮问句和最小 subject；实际 outbound query 统一做空白折叠、最多 300 字及敏感值脱敏。
- [x] **Step 4: 运行目标测试至 GREEN**：`python3 -m pytest tests/unit/test_execution_planner.py tests/unit/test_execution_executor.py tests/unit/test_server_api.py -q -k 'search_query or contextual_query'`。

### Task 4: 每次实际 query 持久化及来源详情呈现（TDD）

**Files:**
- Modify: `webapp/chat_evidence.py`
- Modify: `webapp/source_runtime.py`（增加 `SourceResult.arguments_summary: str = ''` 并校验字符串类型）
- Modify: `webapp/chat_execution.py`
- Modify: `webapp/static/chat_rendering.js`
- Modify: `tests/unit/test_chat_evidence.py`
- Modify: `tests/unit/test_chat_execution.py`
- Modify: `tests/unit/test_chat_rendering_js.py`
- Modify: `tests/browser/test_chat_trust_flow.py`

**Interfaces:**
- 新增/公开 `safe_arguments_summary(arguments)`，现有业务参数白名单不扩展；`query` 字段上限为 300 字，其他值仍受现有上限/脱敏规则保护。
- `execute_source` 将安全摘要放入短生命周期 `SourceResult.arguments_summary`；`project_sources` 复制至 `ToolArtifact.arguments_summary`。RAG tool-event 路径继续使用同一摘要规则。
- `compactToolEntries` 对 web `arguments_summary` 解析并提取 query；不同 query 使用不同分组键；展开来源详情以转义纯文本显示“搜索词：…”。

- [x] **Step 1: 写溯源单测**：搜索 query 参数存入 ToolArtifact；非 query 的未知字段、token/password 被丢弃/脱敏；query 允许最多 300 字并按上限截断；不同查询词不会归并，UI 搜索词以纯文本安全显示；未提供词时不显示伪造 query。
- [x] **Step 2: 运行目标测试确认 RED**：`python3 -m pytest tests/unit/test_chat_evidence.py tests/unit/test_chat_execution.py tests/unit/test_chat_rendering_js.py -q -k 'argument or query or source'`。
- [x] **Step 3: 实现共享安全摘要、投影和渲染**：来源参数进入 artifact 前完成白名单筛选和脱敏；执行来源不持久化原始任意参数；web 查询词作为 entry 内容展示且按 query 分组；保留现有来源状态、数据时间、Facts 和链接。
- [x] **Step 4: 运行目标测试至 GREEN**：`python3 -m pytest tests/unit/test_chat_evidence.py tests/unit/test_chat_execution.py tests/unit/test_chat_rendering_js.py -q`。
- [x] **Step 5: 真实 SSE/浏览器回归**：通过受控本地 search mock 产生不同 query 调用，确认 run artifact 保存每次实际 query、页面来源详情可查、context-only 追问网络来源调用为零、XSS 字符转义。运行 `python3 -m pytest tests/unit/test_server_api.py -q -k 'contextual_chart_followup or contextual_web_search'` 与 `python3 -m pytest tests/browser/test_chat_trust_flow.py -q -k 'web_search_source_details or individual_stock_chart'`。

### Task 5: 文档与整体聚焦验收

**Files:**
- Modify: `README.md`（仅功能列表或用法有变化时）
- Modify: `docs/FEATURE-CATALOG.md`
- Modify: `docs/superpowers/DESIGN-REGISTRY.md`
- Modify: `docs/superpowers/specs/2026-10-12-context-aware-followup-search-audit-design.md`

- [x] **Step 1: 更新功能目录**：记录查询上下文边界、context-only 的无检索条件、query 证据路径和聚焦测试入口；README 和功能目录已在代码 fast-forward 合入后同步更新。
- [x] **Step 2: 执行聚焦回归**：`python3 -m pytest tests/unit/test_chat_followups.py tests/unit/test_chat_request_resolution.py tests/unit/test_execution_plan.py tests/unit/test_execution_planner.py tests/unit/test_execution_executor.py tests/unit/test_chat_evidence.py tests/unit/test_chat_execution.py tests/unit/test_chat_rendering_js.py tests/unit/test_server_api.py tests/unit/test_design_registry.py -q`；`python3 -m pytest tests/browser/test_chat_trust_flow.py -q -k 'web_search_source_details or individual_stock_chart'`；`git diff --check`。
- [x] **Step 3: 复核计划覆盖**：检查两类 query 事件（计划执行与 RAG 工具事件）、无 run/损坏 run/跨会话、市场归因、检索禁用、脱敏、UI 转义和重复来源分组；独立审查无 Critical/Important 发现。

## Self-review

- 本计划将原本容易被误判为外部事件的 UI 解释追问与需要新外部证据的市场因果追问分开。
- WebSearch query 保持无权限效力；授权与预算仍由既有服务端策略控制。
- 两个独立可交付方案分别有独立设计/计划，统一在同一受隔离验证的功能分支中完成。
