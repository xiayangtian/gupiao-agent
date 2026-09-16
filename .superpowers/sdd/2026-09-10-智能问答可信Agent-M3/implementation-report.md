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
