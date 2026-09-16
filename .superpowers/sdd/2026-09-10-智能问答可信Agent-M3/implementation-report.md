# 智能问答可信 Agent M3 实施报告

## Task 1 — 研究状态契约

- RED：`python3 -m pytest tests/unit/test_research_models.py -q`，因 `webapp.research_models` 不存在而收集失败。
- GREEN：`python3 -m pytest tests/unit/test_research_models.py tests/unit/test_chat_models.py -q`，35 passed。
- 实现：冻结 ResearchPlan/ResearchStep/ResearchStepRun/ResearchRun；合法状态转换、依赖安全恢复、已完成步骤不可变、JSON 契约禁止持久化推理链；AnswerRun 仅保存 research_run_id 和安全摘要。
- 偏离：无。

## Task 2 — 受限 Planner

- RED：`python3 -m pytest tests/unit/test_research_planner.py -q`，因模块不存在而收集失败。
- GREEN：`python3 -m pytest tests/unit/test_research_planner.py tests/unit/test_research_models.py -q`，9 passed。
- 实现：仅 `research_task` 或显式制定研究计划进入规划；AI JSON 仅作为候选，静态核验 Scope、ToolPolicy、依赖、循环、长度和验收条件，不合格时回退确定性五步计划。
- 偏离：无。

## Task 3 — 可停止 Executor

- RED：`python3 -m pytest tests/unit/test_research_executor.py -q`，因模块不存在而收集失败。
- GREEN：`python3 -m pytest tests/unit/test_research_executor.py -q`，3 passed。
- 实现：只并行无依赖 retrieve/tool，其他步骤按依赖顺序；每步立即经持久化回调保存，停止保留完成步骤，恢复跳过完成工具与事实。
- 偏离：无。

## Task 4 — ResearchAgent

- RED：`python3 -m pytest tests/unit/test_research_agent.py -q`，因模块不存在而收集失败。
- GREEN：`python3 -m pytest tests/unit/test_research_agent.py tests/unit/test_research_executor.py tests/unit/test_research_models.py -q`，11 passed。
- 实现：串联 Planner、Executor 与 M2 ClaimVerifier；仅发出业务研究事件；核验 blocked 时严格降为 partial，不创建计划的简单意图走普通回答。
- 偏离：无。

## Task 5 — SSE、持久化与恢复端点

- RED：研究存储/API 契约在新增实现前不可导入；现有 M2 research fallback 断言已更新为 M3 SSE 契约。
- GREEN：`python3 -m pytest tests/unit/test_research_store.py tests/unit/test_research_agent.py tests/unit/test_server_api.py -q -k 'research or agent'`，5 passed、143 deselected。
- 实现：ResearchRun 以会话 owner 索引持久化；研究流发出计划/步骤/终态事件；读取和恢复端点均按 session_id 隔离，普通 SSE 仍由既有独立 producer 路径服务。
- 偏离：未使用 TaskManager 承载普通流式研究，避免改变其普通任务语义。

## Task 6 — 研究计划与恢复交互

- RED：新增研究渲染函数在实现前不存在。
- GREEN：`python3 -m pytest tests/unit/test_chat_rendering_js.py -q`，17 passed。
- 实现：纯渲染业务计划、步骤和恢复资格；SSE 消费研究事件；恢复按钮含可访问标签/焦点，移动样式允许步骤名换行且不以颜色单独传达状态。
- 偏离：无。

## Task 7 — 浏览器回归与验收

- RED：研究浏览器用例在实现前不存在。
- GREEN：`python3 -m pytest tests/browser/test_research_agent_flow.py -q`，4 passed；`python3 -m pytest -q`，1029 passed、2 skipped、3 warnings；`git diff --check` 通过；`python3 scripts/check_css.py` 通过。
- 真实浏览器 QA：agent-browser 访问真实 fixture 应用并在 1280x900、768x1000、390x844 打开重开研究会话；三档均 `overflow=false`。浏览器回归同时验证控制台/页面错误为空。
- 偏离：浏览器 fixture 使用确定性本地协作方，不调用 AI、MCP、网页或真实 RAG 摄取。

## Stop/Resume corrective implementation (authorized)

- 裁决：协调方确认 queue producer + disconnect watcher 是已确认 Task 5 的最小必要实现。
- RED：原研究流在 `await asyncio.to_thread(...)` 期间无法观察断开；历史重开也未取回完整计划。
- GREEN：`python3 -m pytest tests/browser/test_research_agent_flow.py tests/unit/test_server_api.py -q -k research`，5 passed、143 deselected；`git diff --check` 与 CSS 检查通过。
- 实现边界：研究独立 producer thread 经 asyncio queue 转发事件，断开时设置同一 stop_event；会话历史仅依 owner-scoped endpoint 获取完整 ResearchRun；未引入依赖或 M4 功能。
- 风险：默认 fixture 研究步骤非常快，浏览器无法稳定在中途断开；停止/重试的依赖与缓存语义由 Executor 单元回归覆盖。

## 最终审查 P1 修复（2026-09-16，commit `3052017`、`87dbe48`）

### Review 结论（阻塞项）

最终审查判定 M3 的 5 项 P1：生产研究未接入既有 RAG/工具/事实归一与证据产物（无 handler 时空产物被标 completed，并声称“已核对来源”）；步骤未逐步持久化、默认 Executor 未拿到 persist 回调；并行批次 sibling 失败时丢失已完成结果且恢复会重复外部调用；public SSE 状态契约为 running/started/completed/failed/blocked 未被转发/消费/渲染，浏览器回归读的是已完成运行而不是真实触发停止/重开/恢复/重试；`research_task` fallback 仍声称 M3 将来提供、工具缓存键不符合计划的四元组契约。

### 逐项根因 → RED → 修复 → GREEN

1. **生产研究接入 + 不得伪造完成**
   - 根因：`ResearchExecutor._call` 对缺失 handler `return {}`，步骤被判 completed；`ResearchAgent._execute` 固化文案 `"研究已完成：已按计划核对范围内来源。"`，verifier 收到空 facts/artifacts 也仍可能 completed。
   - RED：`python3 -m pytest tests/unit/test_research_agent.py -q -k without_production_handlers` → `1 failed`（status 为 completed）；`python3 -m pytest tests/unit/test_server_api.py -q -k research_task_uses_policy_gated` → 失败（无步骤事件、`run["artifacts"]` 为空）。
   - 修复：缺失 handler 抛错；服务端研究分支用生产 RagQA 做 retrieve 桥（Scope/ToolPolicy 原样传入，`_relay_rag_event` 复用既有 `EvidenceNormalizer`/`FactNormalizer`/`detect_conflicts`），normalize/compare/answer 步骤复用这些产物；无可用来源或未形成结论时 `partial` 并发送 `research_blocked`，答案内容只来自步骤产物。
   - GREEN：上述两条回归 `1 passed`；`tests/unit/test_research_agent.py` 全部通过。

2. **逐步持久化 + 默认 Executor 获得回调**
   - 根因：`ResearchAgent` 默认 `ResearchExecutor()` 无 persist；executor 只在 completed/failed/停止时保存，外部调用进行中无记录，重启会重跑。
   - RED：`python3 -m pytest tests/unit/test_research_agent.py -q -k same_persist_callback` → `1 failed`（`persisted` 为空）。
   - 修复：`ResearchAgent` 把同一 persist 注入默认 executor；executor 在外部调用前持久化 running 状态；服务端把 `chat_store.save_research_run` 作为同一回调同时传给 agent 与 executor。
   - GREEN：该回归 `1 passed`；`tests/unit/test_research_store.py` 覆盖跨重启 owner 作用域读取。

3. **并行批次 sibling 失败仍保留已完成结果**
   - 根因：`as_completed` 循环首个异常 `return self._fail(...)`，其他已完成 sibling 的产物未写入 run。
   - RED：`python3 -m pytest tests/unit/test_research_executor.py -q -k parallel_failure_persists` → `1 failed`（`StopIteration`：已完成 sibling 丢失）。
   - 修复：失败先立即持久化并把 run 置 failed，循环继续收集其余 future 的成功结果；结束后统一返回。恢复只重跑未完成步骤。
   - GREEN：该回归 `1 passed`；`... -k resume_does_not_repeat_a_completed_retrieve_step` → `1 passed`（恢复时 RAG 调用 0 次、产物复用）。

4. **统一 public SSE 状态契约与真实浏览器交互**
   - 根因：步骤事件类型为 `research_step_running`（无 run 级 running）；`app.js` 只消费 `research_plan`/`research_done`，流中不渲染 started/failed/blocked；`renderRunStatus` 还渲染禁用的“继续研究”占位；浏览器用例读取已完成运行。
   - RED：`python3 -m pytest tests/browser/test_research_agent_flow.py -q -k 'stop_marks or resume_retries'` → `2 failed`（流中无计划、无停止状态、无恢复按钮）。
   - 修复：新增 `research_running`；`research_step_started` 载荷状态改用持久化词表 `running`；失败/停止统一发 `research_blocked`；`app.js` 在流中渲染计划/步骤/受阻，终态改用持久化 run 渲染并移除临时进度块；渲染中文状态标签；移除禁用占位。
   - GREEN：浏览器两条用例 `2 passed`；`tests/unit/test_chat_rendering_js.py` 18 passed。

5. **fallback 与工具缓存键**
   - 根因：`ToolPolicyResolver` research fallback 写死“详细研究规划将在 M3 提供”；executor 缓存键为 `tool + step_id + scope repr`，与计划要求的 `provider + tool_name + canonical JSON arguments + as_of_date` 不符。
   - RED：`python3 -m pytest tests/unit/test_server_api.py -q -k research_task_uses_policy_gated` → 失败（断言 `"M3" not in fallback`）。
   - 修复：fallback 改为说明当前范围/工具策略边界与无来源时的诚实降级；删除不合规缓存（计划契约没有 provider/arguments/as_of 字段），在 `_call` 注释中写明启用前提。
   - GREEN：该回归 `1 passed`。

### 验证记录

- 相关单测：`python3 -m pytest tests/unit/test_research_models.py tests/unit/test_research_planner.py tests/unit/test_research_executor.py tests/unit/test_research_agent.py tests/unit/test_research_store.py tests/unit/test_chat_rendering_js.py tests/unit/test_server_api.py -q -k 'research or executor or rendering'`：49 passed, 134 deselected。
- 浏览器：`python3 -m pytest tests/browser -q`：31 passed, 1 skipped（含真实停止、历史重开、恢复点击、失败步骤重试、1280x900/768x1000/390x844 三档无溢出与控制台/页面错误为空）。
- 全量：`python3 -m pytest -q`：1035 passed, 2 skipped, 3 warnings。
- `git diff --check`：通过；`python3 scripts/check_css.py`：通过。
- 既有失败记录：全量运行中若残留 headless Chrome 进程，`agent-browser open` 会 15s 超时，出现一次 `ERROR`（`test_financial_structure_visuals` / `test_chat_trust_flow`）；清理后单文件与全量均通过，属环境噪声。

### 偏离与风险

- 工具结果缓存按“字段不完整则不启用”的备选方案整体停用，等受控工具步骤提供 provider/arguments/as_of 后再启用。
- 研究步骤的 `error` 文本仍随 ResearchRun 持久化（owner-scoped endpoint 可读，UI 只展示业务状态）；公开事件只发通用阻塞原因。
- 研究运行使用与普通问答相同的 RagQA 桥接，但不注入补报上下文：research_task 的 ToolPolicy 不授权任何工具，模型无法申请补充财报，缺来源时按 partial/blocked 诚实降级（不改变补报授权端点语义）。
- 未推送、未合并、未实现 M4；`docs/FEATURE-CATALOG.md`/`README.md` 的功能条目更新留在合入 `main` 时执行。
