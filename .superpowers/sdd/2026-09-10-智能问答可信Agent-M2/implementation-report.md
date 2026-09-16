# 智能问答可信 Agent M2 实施报告

## 阶段与进展

M2 Task 1–7 已在 `feat/trusted-chat-m2` 完成；未实现 M3 Planner、恢复步骤或 M4 工作台。

| Task | 状态 | 提交 |
| --- | --- | --- |
| 1 意图与工具策略 | 完成 | `8b2866d` |
| 2 事实归一与冲突 | 完成 | `0ba8f38` |
| 3 论断核验 | 完成 | `fcd04db` |
| 4 RAG 策略门控 | 完成 | `542bedf` |
| 5 Server 编排/持久化 | 完成 | `be8c7c0` |
| 6 前端来源边界展示 | 完成 | `0ab83ad` |
| 7 评测与浏览器闭环 | 完成 | `1209913` |

## TDD RED/GREEN 证据

- T1 RED：`python3 -m pytest tests/unit/test_chat_policy.py -q` → `ModuleNotFoundError: webapp.chat_policy`；GREEN：policy + chat model tests `34 passed`。
- T2 RED：`python3 -m pytest tests/unit/test_chat_facts.py -q` → `ModuleNotFoundError: webapp.chat_facts`；GREEN：fact + model tests `34 passed`。
- T3 RED：`python3 -m pytest tests/unit/test_chat_verifier.py -q` → `ModuleNotFoundError: webapp.chat_verifier`；GREEN：verifier + model tests `34 passed`。
- T4 RED：`python3 -m pytest tests/unit/test_rag_policy_m2.py -q` → 3 failed（`tool_policy` 参数缺失）；GREEN：policy/RAG tests `45 passed, 3 warnings`。
- T5 RED：M2 adapter compatibility first reproduced an SSE error event; GREEN：server targeted `19 passed`、full server API `140 passed`。
- T6 RED：rendering test 1 failed（M2 renderer functions absent）；GREEN：`16 passed`。
- T7：fixture schema test and real browser flow were added and passed; this task did not retain an independent pre-fixture RED command, which is a process-evidence gap.

## 验证

- `python3 -m pytest -q` → `968 passed, 1 skipped, 3 warnings in 108.08s`。
- `python3 -m pytest tests/browser/test_chat_policy_flow.py -q` → `1 passed in 4.63s`。
- `python3 -m pytest tests/browser/test_chat_trust_flow.py -q` → `6 passed in 18.70s`。
- `python3 -m pytest tests/unit/test_browser_test_safety.py tests/unit/test_chat_policy_eval_cases.py -q` → `9 passed in 0.58s`。
- `git diff --check` → passed.
- `python3 scripts/check_css.py` → failed only for existing `.history-view-pane` duplicate selector; no M2 selector was named.

## 真实浏览器 QA

通过 `agent-browser` 实际打开 fixture app `http://127.0.0.1:58732/#/chat`，重开真实 SSE 创建的会话。实际 DOM 结果：`本次查证方式本地财报查证`、`未找到可核验的披露，不能确认该数值。`、`overflow: false`。浏览器回归同时覆盖 1280x900、768x1000、390x844，且断言无 console/page errors。

## 偏离/障碍

- CSS checker 的 `.history-view-pane` 重复选择器是现有文件问题；未扩大 M2 范围修改它。
- Task 7 fixture schema 未保留独立 RED 证据，其他新增行为均有 RED/GREEN 记录。
- 需要独立 reviewer 完成计划要求的 P0/P1 代码评审门禁；本实施线程未自行宣布该门禁通过。

## 自评

功能契约、持久化、SSE、前端和浏览器闭环已实现且全量 pytest 通过。因 CSS 检查的既有失败、Task 7 RED 证据缺口以及待独立评审，本次自评为 `DONE_WITH_CONCERNS`。

## M2 最终审查 Important 修复

最终审查的四项 Important finding 已在 `c4eaf72`（`fix: 加固可信问答 M2 状态契约`）修复，未实现 M3/M4、未推送或合并。

1. **Scope 工具参数绕过**：问题不在工具白名单，而在已批准工具的参数未绑定冻结 Scope。`RagQA` 现拒绝 `symbol/code/company_code/report_id/report_ids` 中显式出现的越界公司身份，拒绝事件不会调用 executor。RED `python3 -m pytest tests/unit/test_rag_policy_m2.py -q`：`1 failed, 3 passed`；GREEN：`4 passed, 3 warnings`。
2. **M1 宽松 Fact 绕过 M2 归一**：server 曾将任意 raw JSON tool-result 经 M1 宽松路径直接持久化。现在 raw 文本仅为 reference artifact；Fact 只接受 policy-gated `structured_tool_result`，并由 `FactNormalizer` 校验完整字段。执行 artifact 覆盖 payload 声称的 provider/as_of。RED `python3 -m pytest tests/unit/test_server_api.py -q -k raw_tool_json_does_not_bypass_fact_normalization`：`1 failed, 140 deselected`；GREEN：`1 passed, 140 deselected, 3 warnings`。
3. **ClaimVerifier 指标与冲突误判**：同单位同数值曾可跨指标匹配，英文 `revenue` 冲突也无法识别中文“营业收入”。现在以受控别名（营收、净利润、经营现金流、价格）绑定数值论断和冲突相关性。RED `python3 -m pytest tests/unit/test_chat_verifier.py -q`：`2 failed, 4 passed`；GREEN：`6 passed`。
4. **Task 7 验收覆盖不足**：原 fixture 只校验元数据，浏览器 fake 只有通用回答路径。现在真实 FastAPI SSE + agent-browser 覆盖行业本地样本、实时 reference/as_of、公告事件网页来源、冲突披露、工具失败 partial 以及越界事实拒绝；重开会话逐项断言文本和无横向溢出/console/page errors。RED `python3 -m pytest tests/browser/test_chat_policy_flow.py -q`：`1 failed, 1 passed`；GREEN：`2 passed`。

## 修复后验证与真实浏览器 QA

- 受影响单测：`python3 -m pytest tests/unit/test_chat_verifier.py tests/unit/test_chat_facts.py tests/unit/test_chat_evidence.py tests/unit/test_rag_policy_m2.py tests/unit/test_rag_qa.py tests/unit/test_server_api.py -q` → `208 passed, 3 warnings in 9.03s`。
- M2 SSE/真实浏览器：`python3 -m pytest tests/browser/test_chat_policy_flow.py tests/browser/test_chat_trust_flow.py -q` → `8 passed in 31.29s`。测试用 agent-browser 打开真实 fixture FastAPI URL，重开行业、实时、事件和冲突会话并验证无横向溢出、console errors 或 page errors。
- 手工真实浏览器 QA：实际启动 URL `http://127.0.0.1:58765/#/chat`，经 SSE 创建冲突 run 后以 agent-browser 重开。DOM 显示两条外部 reference、`存在口径/时间差异`、`⚠️ 部分完成`；测量 `overflow: false`。
- 全量：`python3 -m pytest -q` → `972 passed, 2 skipped, 3 warnings in 126.06s`。
- `git diff --check`、`git diff --cached --check` → passed；修复提交前没有 staged files。
- `python3 scripts/check_css.py` → failed only on既有 `.history-view-pane` duplicate selector；`git show HEAD:webapp/static/style.css | grep -n '\.history-view-pane' | wc -l` → `2`，本修复没有触及 CSS。

## 修复后自评

`DONE_WITH_CSS_BASELINE_CONCERN`：四项 Important finding 均有最小 RED/GREEN 回归和全量/浏览器验证。残留风险仅为无关既有 CSS checker 失败；仍需独立 reviewer 执行门禁，且本分支尚未合并 main。

## M2 第二轮复审修复（代码提交 `8cbdd76`；本证据与代码同分支，未推送未合并）

复审确认的 7 项发现全部修复并提交在 `feat/trusted-chat-m2`（未推送、未合并、未实现 M3/M4）。
根因、RED/GREEN 命令与实际输出如下。

### 1. F1 身份参数绕过 Scope

**根因：** `RagQA._tool_arguments_within_scope()` 只把 `re.fullmatch(r"\d{6}")` 的显式代码
与 Scope 允许代码比对，非 6 位数字的名称/别名（如 `{"symbol": "长江电力"}`）直接放行，
模型可以用公司名称把外部工具指向范围外公司。

**RED（旧代码 `HEAD^` 实测）：** 临时回退 `financial_report_fetcher/rag/qa.py` 后运行
复现脚本（`{"symbol": "长江电力"}`、Scope=601288、policy=realtime_market）：

```
names reaching executor: [{'symbol': '长江电力'}]
```

**GREEN：** 同一脚本在修复后输出 `names reaching executor: []`；回归测试
`python3 -m pytest tests/unit/test_rag_policy_m2.py -q` → `10 passed, 3 warnings`。

**修复：** 身份参数（`symbol/code/company_code/stock_code/ts_code/secu_code/ticker/report_*`）
一律先解析成 6 位代码再校验是否属于 Scope 允许公司集合；解析器由服务端注入，与工具执行器
使用**同一个** `_resolve_symbol_code`（`webapp/server.py:_init_rag`），避免策略与执行分叉；
解析失败即受控失败（fail-closed）。数字与名称两种形式各有回归测试。

### 2. P1-1 允许集与真实工具名同源

**根因：** `webapp/chat_policy.ToolPolicyResolver` 写死 `("get_quote", "web_search")`，而真实
provider 工具名是 `get_realtime_quote/get_realtime_data` 等，交集后实时/事件意图只剩
`web_search`（实测 `AssertionError: assert {'web_search'} == {'get_realtime_quote',
'get_realtime_data', 'web_search'}`）。

**RED：** `python3 -m pytest tests/unit/test_chat_policy.py -q` → `3 failed, 5 passed`
（新增 4 条用例中 3 条失败，失败信息即上面这行）。
**GREEN：** 同命令 → `8 passed, 3 warnings`。

**修复：** 工具家族谓词定义在 provider 工具名所在模块
`financial_report_fetcher/rag/mcp_tools.py`（`is_realtime_quote_tool/is_web_search_tool`、
`WEB_SEARCH_TOOL_NAME`），`ToolPolicyResolver` 只做「意图 → 家族 × 当前可用工具名」的交集，
所以授权名永远来自 `_build_chat_tool_defs` 的真实定义；非家族工具（`get_financial_metrics`
等）与 `request_missing_reports` 不会被授权。`source_policy.market_data` 现在只有真的授予了
行情工具家族才为 true。

### 3. P1-2 恢复受控补报工具

**根因：** `RagQA.answer_stream()` 在带 policy 时按 `tool_policy.allowed_tools` 过滤工具定义，
`request_missing_reports` 不在任何意图的允许集里，于是 main 已合入的问答补报能力在生产不可达。

**RED：** `python3 -m pytest tests/unit/test_rag_policy_m2.py -q -k supplement_tool` →
2 failed（`seen_tools` 中没有 `request_missing_reports`）。
**GREEN：** 同命令 → `2 passed`；整文件 `10 passed`。

**修复：** 受控工具（`CONTROLLED_TOOL_NAMES`，只申请授权、不执行外部动作）不被 policy 过滤，
也不占预算；其自身门控（载荷校验、handler、重复请求、无下载）完全不变，未批准的其他工具
仍被过滤——回归测试同时断言 `web_search not in seen_tools[0]` 与非法载荷只产生受控失败。

### 4. P2-3 消费 ToolPolicy.fallback_message

**根因：** `fallback_message` 只被写进 `ToolPolicy`/持久化 JSON，没有任何生产路径读取。

**RED：** `python3 -m pytest tests/unit/test_server_api.py -q -k 'm2_blocked_run or research_task
or fallback_hint'` → `3 failed`，其中两条为 `事件 'policy_fallback' 不存在`，
`research_task` 一条为模型被调用（`AssertionError: M2 的研究任务不应调用模型或工具`）。
**GREEN：** 同命令 → `3 passed`。

**修复：** 无外部工具或 `research_task` 时发出独立 SSE `policy_fallback`（含 intent 与
message）；`research_task` 按方案要求给出 M2 无工具回答（内容即 fallback 文案，状态 partial，
不调用模型与工具）。

### 5. P2-4 timeout_seconds 约束单次工具调用

**RED：** `python3 -m pytest tests/unit/test_rag_policy_m2.py -q -k timeout` →
`assert 2.0052919164299965 < 1.5`（policy timeout=1s，执行器 sleep 2s 仍被等满）。
**GREEN：** 同命令 → `1 passed`（总耗时 <1.5s，`tool_result ok=False` 且摘要含“超时”，
成功工具列表为空）。

**修复：** `RagQA._execute_tool()` 在有 policy 时经专用线程池按 `timeout_seconds` 等待，
超时即返回受控失败并放弃等待，不再拖住问答流。

### 6. P2-5 blocked 只替换不受支持的论断

**RED：** `python3 -m pytest tests/unit/test_server_api.py -q -k m2_blocked_run` →
`AssertionError: assert '营业收入为 100 亿元' in '未找到可核验的披露，不能确认该数值。'`
（整段替换，受支持内容与上下文被丢弃）。
**GREEN：** 同命令 → `1 passed`（受支持的“营业收入为 100 亿元”与公司身份保留，不受支持的
621 亿元被替换，回答含安全说明）。`tests/unit/test_chat_verifier.py -q` → `13 passed`。

**修复：** `ClaimVerifier.degrade_blocked()` 只替换没有本范围事实支持的数值论断；整篇没有
可核验论断时才回退为固定安全说明（保持旧文案，浏览器验收仍断言该文案）。

### 7. 残留：指标词收敛到单条论断 + 评测用例真正跑流水线

**指标词（RED）：** 新增用例后 `python3 -m pytest tests/unit/test_chat_verifier.py -q` →
先因 `ImportError: cannot import name 'SAFE_UNSUPPORTED_CLAIM_TEXT'` 收集失败，实现后又出现
`assert 'passed' == 'blocked'`（“营业收入为 100 亿元，净利润为 100 亿元”里，整篇出现的
“营业收入”让净利润的 100 亿元被误判为受支持）。**GREEN：** `13 passed`。
修复：指标词只在数值所在分句内判定；越界事实不再为数值背书；千分位逗号不被当作分句边界，
金额按整值比对（`4,108.71 亿元` 不再被切碎）。

**评测用例（RED/GREEN）：**
`tests/unit/test_chat_policy_eval_cases.py` 现在用真实 FastAPI 端点驱动真实 Scope 解析、
IntentRouter、ToolPolicyResolver、`_build_chat_tool_defs` 的真实工具名、FactNormalizer、
冲突识别、ClaimVerifier 与持久化；只有模型/外部工具由按问题脚本化的假协作方替代，
用例缺少脚本直接失败（不允许静默跳过）。首轮实跑即暴露 fixture 从未执行过的事实：
`4 failed, 6 passed`，根因是 `expected_status` 混用了 AnswerStatus 与核验状态两套词表
（“passed”不是合法 AnswerStatus）。现在 fixture 显式区分 `expected_status`（运行：
completed/partial）与 `expected_verification`（核验：passed/partial/blocked），并逐条比对
intent、允许来源、实际使用来源、禁止报告、运行/核验状态与真实证据 id；实跑结果
`10 passed`。把允许集临时改回复审前的字面量 `get_quote` 后复跑，恰好 4 条实时/事件用例失败
（`realtime_market0/1/2`、`event_attribution`），证明这些用例真的卡住了 P1-1 的缺陷。

### 修复后验证（实际输出）

- 受影响单测：`python3 -m pytest tests/unit/test_chat_verifier.py tests/unit/test_chat_facts.py tests/unit/test_chat_evidence.py tests/unit/test_chat_policy.py tests/unit/test_chat_policy_eval_cases.py tests/unit/test_rag_policy_m2.py tests/unit/test_rag_qa.py tests/unit/test_server_api.py tests/unit/test_mcp_tools.py -q` → `252 passed, 3 warnings in 10.31s`。
- tests/unit 全量：`python3 -m pytest tests/unit -q` → `953 passed, 3 warnings in 15.55s`。
- 全量：`python3 -m pytest -q` → `1002 passed, 1 skipped, 3 warnings in 115.27s`（skip 为既有环境跳过：Chrome `--dump-dom` 在 15 秒内未退出）。
- M2 浏览器流：`python3 -m pytest tests/browser/test_chat_policy_flow.py -q` 与补报浏览器回归
  `tests/browser/test_chat_pdf_supplement.py -q` → `13 passed in 51.08s`；M1 信任闭环
  `tests/browser/test_chat_trust_flow.py -q` → `6 passed in 18.15s`。
- `git diff --check` → passed；提交前 `git status --short` 全部为已暂存改动，提交后工作区干净。
- `python3 scripts/check_css.py` → 仍失败，且只报既有重复选择器 `.history-view-pane`
  （`webapp/static/style.css:1755/1765`，本次 diff 未触及 CSS）。

### 本次评审中的判断与偏离（供复审确认）

1. **fixture 词表拆分**：`expected_status` 改为 AnswerStatus，新增 `expected_verification`。
   依据：`blocked/partial/passed` 与 `completed/partial` 是两套词表；且既有契约
   （`tests/browser/test_chat_trust_flow.py` 断言核验不通过的回答 run 仍为 `completed`）要求
   运行状态与可信状态分开记录。
2. **冲突用例改为行情 vs 网页冲突**：M2 服务端路径只能从结构化工具事件产生 Fact
   （`_relay_rag_event` 只把 `structured_tool_result` + ToolArtifact 交给 `FactNormalizer`），
   PDF 证据只能产生 EvidenceArtifact；因此“PDF 事实 vs 外部事实”的冲突在 M2 不可达，
   原用例的 `revenue_pdf` 期望无法真实满足。改用 `get_realtime_data`（3.2）与 `web_search`
   （3.5）的价格冲突，仍是“同指标同口径来源冲突必须披露”的同一契约。
3. **research_task 直达回答**：方案 Task 1 明确要求“research_task 收到无工具的 M2 回答”，
   因此该意图不再调用模型/工具，直接持久化 fallback 文案（status partial）。
4. **新增 SSE `policy_fallback`**：报告类意图的 SSE 事件序列多了一帧，
   `tests/unit/test_server_api.py::TestChatSupplementApi::test_stream_supplement_needs_consent_and_does_not_download`
   的期望序列同步更新为 `session → scope_resolved → policy_resolved → policy_fallback → run_started → supplement_needed`。
   前端未消费该事件（不在本次修复清单内），未知事件在 `webapp/static/app.js` 中被忽略。

### 遗留风险（未在本次修复范围内）

1. **PDF 事实不可达**：服务端没有任何路径把“PDF 页码证据 + 结构化数值”转成 `verified` Fact，
   因此纯 PDF 依据的数字在核验时一律 blocked（fail-closed）。这是 M2 设计
   “关键数字均有期间、单位、主体口径和证据”尚未闭环的部分，需要补一条受控 PDF 事实来源
   （超出本轮 7 项修复，未自行决策）。
2. **数值格式**：已支持千分位，仍不支持全角数字、“亿”不带“元”等写法；这类写法会被判为
   无证据并降级（fail-closed）。
3. **工具超时实现**：超时后放弃等待，底层线程仍可能跑到自身 HTTP 超时；执行器自身超时
   （MCP 30s / 网页 15s）仍是最外层兜底。补报恢复路径（M1 路径）没有 ToolPolicy，
   因此不受策略超时约束。
4. **自由文本参数**：`query/keyword` 不是身份参数（按方案定义），不参与 Scope 校验；跨公司
   事实/数字仍会被 FactNormalizer 与 ClaimVerifier 拦下。
5. **评测脚本层**：评测用例的模型/工具层是脚本化的，RagQA 内部的策略门控由
   `tests/unit/test_rag_policy_m2.py` 覆盖；评测用例断言的是策略授予与运行结果契约。
6. **CSS 检查**：既有 `.history-view-pane` 重复选择器失败未修（本分支未触及 CSS）。

### 自评

`DONE_WITH_RESIDUAL_RISKS`：7 项复审发现均有 RED/GREEN 回归证据、最小修复与提交
（`8cbdd76`），受影响单测、tests/unit 全量、全量 pytest、M2 浏览器流与补报浏览器回归全部通过；
`git diff --check` 通过；CSS 检查仍只有既有失败（未声称通过）。仍需独立 reviewer 执行门禁，
分支未推送、未合并。

## M2 第二轮复审遗留 P1 修复（`27a3c3d`、`3d63f9e`；未推送、未合并）

本轮严格限于 M2：修复冻结 Scope 下自由文本网页查询绕过（P1-A），并让 8 条
`chat_policy_eval_cases.json` 真实经过 `RagQA.answer_stream()` 的工具策略门控（P1-B）。未改动
M3/M4、前端或外部网络调用。

### P1-A：网页 `query` 绑定冻结 Scope

**根因：** `RagQA._tool_arguments_within_scope()` 仅校验 `symbol/code/report_id` 等结构化参数，
将 `web_search.query` 视为不带身份的自由文本；所以 `company_only(601288)` 下
`web_search(query="长江电力 公告")` 可直达 executor。

**RED（实际）：** 先新增最小参数化回归后执行
`python3 -m pytest tests/unit/test_rag_policy_m2.py -q -k out_of_scope_web_query_identity`，结果
`3 failed, 10 deselected, 3 warnings in 0.76s`；名称、`600900` 和别名 `长电` 三种输入均显示
`('web_search', {'query': ...})` 已进入 executor。

**实现与 GREEN：** `27a3c3d`（`fix: bind scoped web queries to company identity`）增加
`_bind_web_query_to_scope()`：对 `company_only/company_industry` 的每个网页查询解析 6 位代码与
中文名称/别名片段（复用 executor 的同一名称→代码 resolver），任一可解析身份不在冻结 Scope
即在 executor 前返回受控失败；通过后用服务端 Scope 身份前缀构造 provider query。`whole_corpus`
保留原 query 不收窄。GREEN：`python3 -m pytest tests/unit/test_rag_policy_m2.py -q` →
`15 passed, 3 warnings in 1.87s`；覆盖范围外名称/代码/别名不执行、范围内 `农行 公告` 可执行且被绑定、
`whole_corpus` 原样执行，以及 `event_attribution` 仍可网页查询。

### P1-B：评测真实通过 RagQA 策略门控

**根因：** 原 `_ScriptedRagQA` 直接 yield `tool_call/structured_tool_result/done`，使 8 条评测
绕过 `RagQA` 的模型可见工具定义过滤、未批准工具拒绝、Scope 参数检查和真实 executor 调度。

**RED（实际）：** 新增断言后执行
`python3 -m pytest tests/unit/test_chat_policy_eval_cases.py -q -k evaluation_pipeline_uses_real_ragqa`，结果
`1 failed, 10 deselected, 3 warnings in 1.09s`，断言显示
`isinstance(<_ScriptedRagQA>, RagQA)` 为 false。

**实现与 GREEN：** `3d63f9e`（`test: run M2 policy evaluation through real RagQA`）以 fake store、
fake AI 和 recording fake executor 组装真实 `RagQA` 后注入真实 FastAPI `/api/chat/stream`；fake AI
只产生 OpenAI 风格请求，所有工具过滤、参数绑定、调用、结构化事件、SSE、Fact/Verifier 和 run
持久化均由生产代码完成。GREEN：`python3 -m pytest tests/unit/test_chat_policy_eval_cases.py -q` →
`11 passed, 3 warnings in 1.58s`。逐条断言 8 条 fixture 的 intent、模型可见定义、policy、实际 executor
调用、来源/禁止报告、证据、状态与持久化 run；另有真实 RAG 探针断言未批准
`get_financial_metrics` 与范围外网页 query 均被拒绝且 executor 未调用。

### 验证（实际输出）

- 受影响单测：`python3 -m pytest tests/unit/test_rag_policy_m2.py tests/unit/test_chat_policy_eval_cases.py tests/unit/test_rag_qa.py tests/unit/test_server_api.py -q` → `212 passed, 3 warnings in 13.47s`。
- 全部 unit：`python3 -m pytest tests/unit -q` → `961 passed, 3 warnings in 15.04s`。
- 全量：`python3 -m pytest -q` → `1008 passed, 1 skipped, 3 warnings in 112.37s`。
- 浏览器指定回归：`python3 -m pytest tests/browser/test_chat_policy_flow.py tests/browser/test_chat_pdf_supplement.py -q` → `13 passed in 52.46s`。
- `git diff --check` 和 `git diff --cached --check` → passed；验证时无 staged files。
- `python3 scripts/check_css.py` → failed，仅有既有 `.history-view-pane` 重复 2 次；本轮未改 CSS。

### 遗留风险与自评

- 中文自由文本身份解析依赖现有名称→代码 resolver；无法解析的自由文本会保留并加 Scope 身份前缀，
可解析的范围外身份 fail-closed。解析器的股票别名覆盖范围仍受其本地索引/词典数据质量限制。
- `company_industry` 的网页 query 会绑定 focus 公司名称及 Scope 中的允许代码；搜索 provider 仍可能返回
无关网页，但范围外可解析公司输入不会被执行。
- 评测保持完全离线：模型、store、executor 为 fake；生产 Scope/Policy/RagQA/Fact/Verifier/SSE/持久化为真实路径。
- CSS 基线失败未扩大范围修复；仍需独立 reviewer 门禁。自评：`DONE_WITH_RESIDUAL_RISKS`。
