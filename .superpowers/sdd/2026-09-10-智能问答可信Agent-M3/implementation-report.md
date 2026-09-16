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
