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
