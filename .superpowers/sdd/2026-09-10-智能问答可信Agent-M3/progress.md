# M3 实施进度

- 状态：实施中（`feat/trusted-chat-m3`）
- 范围：严格执行 M3 Tasks 1–7；不含 M4 收藏、导出、跨会话筛选或长期记忆。

| Task | 状态 | RED | GREEN | 提交 | 偏离 |
|---|---|---|---|---|---|
| 1 | 已完成 | 失败（模块不存在） | 35 passed | `1f0c638` | 无 |
| 2 | 已完成 | 失败（模块不存在） | 9 passed | `adb5d2c` | 无 |
| 3 | 已完成 | 失败（模块不存在） | 3 passed | `a264ecb` | 无 |
| 4 | 已完成 | 失败（模块不存在） | 11 passed | `a13f809` | 无 |
| 5 | 已完成 | 契约缺失 | 5 passed | `4220e57` | 普通流未改用 TaskManager |
| 6 | 已完成 | 渲染函数缺失 | 17 passed | `d0014d9` | 无 |
| 7 | 已完成 | 浏览器用例缺失 | 1029 passed, 2 skipped | `f6934ff` | 本地 fixture，无外网 |
| 最终审查修复 | 已完成 | 见下方 RED | 1035 passed, 2 skipped | `3052017`、`87dbe48` | 无 |

## 命令日志

- RED `python3 -m pytest tests/unit/test_research_models.py -q`：预期收集失败（`ModuleNotFoundError`）。
- GREEN `python3 -m pytest tests/unit/test_research_models.py tests/unit/test_chat_models.py -q`：35 passed。
- RED `python3 -m pytest tests/unit/test_research_planner.py -q`：预期收集失败（`ModuleNotFoundError`）。
- GREEN `python3 -m pytest tests/unit/test_research_planner.py tests/unit/test_research_models.py -q`：9 passed。
- RED `python3 -m pytest tests/unit/test_research_executor.py -q`：预期收集失败（`ModuleNotFoundError`）。
- GREEN `python3 -m pytest tests/unit/test_research_executor.py -q`：3 passed。
- RED `python3 -m pytest tests/unit/test_research_agent.py -q`：预期收集失败（`ModuleNotFoundError`）。
- GREEN `python3 -m pytest tests/unit/test_research_agent.py tests/unit/test_research_executor.py tests/unit/test_research_models.py -q`：11 passed。

- 浏览器 QA：1280x900、768x1000、390x844 均无横向溢出；控制台/页面错误为空。
- 完整验证：`python3 -m pytest -q`：1029 passed, 2 skipped, 3 warnings；`git diff --check` 通过；`python3 scripts/check_css.py` 通过。
- 已按授权修正：研究线程通过 queue 向 SSE 转发，断开时协作取消；历史按会话读取完整研究运行。
- corrective GREEN：`python3 -m pytest tests/browser/test_research_agent_flow.py tests/unit/test_server_api.py -q -k research`：5 passed，143 deselected；CSS/diff 检查通过。

## 最终审查 P1 修复（review → 根因 → RED/GREEN）

| 审查项 | 根因 | RED（最小失败回归） | GREEN | 提交 |
|---|---|---|---|---|
| P1-1 生产研究必须接入既有 RAG/工具/事实归一与证据产物，且无 handler 不得空产物 completed | `ResearchExecutor._call` 对缺失 handler 直接 `return {}`，`ResearchAgent` 用固定文案“已按计划核对来源”标 completed | `python3 -m pytest tests/unit/test_research_agent.py -q -k without_production_handlers` → `0 passed, 1 failed`（status 为 completed）；`python3 -m pytest tests/unit/test_server_api.py -q -k research_task_uses_policy_gated` → 失败（无 `research_step_started`/产物） | 同命令 → `1 passed`；服务端研究流桥接 RagQA 后 → `1 passed` | `3052017` |
| P1-2 每步立即经同一 persist 回调写入 ChatStore，默认 Executor 必须拿到回调，重启/恢复不重跑已完成步骤 | `ResearchAgent` 默认构造裸 `ResearchExecutor()`，只有终态写入；运行中步骤不落盘 | `python3 -m pytest tests/unit/test_research_agent.py -q -k same_persist_callback` → `1 failed`（`persisted` 为空） | 同命令 → `1 passed` | `3052017` |
| P1-3 并行批次任一 sibling 失败仍须收集并立即持久化其他成功 sibling，恢复不得重复已完成外部调用 | 并行 `as_completed` 循环中首个异常直接 `return self._fail(...)`，未持久化的兄弟结果丢失 | `python3 -m pytest tests/unit/test_research_executor.py -q -k parallel_failure_persists` → `1 failed`（`StopIteration`：已完成 sibling 丢失） | 同命令 → `1 passed`；`tests/unit/test_server_api.py -q -k resume_does_not_repeat` → `1 passed`（恢复时 RAG 调用次数为 0） | `3052017` |
| P1-4 统一 public SSE 契约（running/started/completed/failed/blocked）并在流中渲染；浏览器测试必须真实停止/重开/恢复/重试 | 步骤事件类型为 `research_step_running`、无 run 级 running，`app.js` 只消费 plan/done，且运行状态残留禁用“继续研究”占位 | `python3 -m pytest tests/browser/test_research_agent_flow.py -q -k 'stop_marks or resume_retries'` → `2 failed`（流中没有计划/停止状态、没有恢复按钮） | 同命令 → `2 passed`；`tests/unit/test_chat_rendering_js.py -q` → `18 passed` | `87dbe48` |
| P1-5 `research_task` fallback 不得声称 M3 将来提供；工具缓存键须符合 provider+tool_name+canonical arguments+as_of_date，或在字段不完整时不启用 | `ToolPolicyResolver` fallback 文案写死“将在 M3 提供”；executor 缓存键为 `tool+step_id+scope repr` | `python3 -m pytest tests/unit/test_server_api.py -q -k research_task_uses_policy_gated` → 失败（fallback 含 `M3`） | 同命令 → `1 passed`；缓存整体移除并在代码注释说明启用条件 | `3052017` |

### 命令日志（最终审查修复）

- 相关单测：`python3 -m pytest tests/unit/test_research_models.py tests/unit/test_research_planner.py tests/unit/test_research_executor.py tests/unit/test_research_agent.py tests/unit/test_research_store.py tests/unit/test_chat_rendering_js.py tests/unit/test_server_api.py -q -k 'research or executor or rendering'`：49 passed, 134 deselected。
- 浏览器流：`python3 -m pytest tests/browser -q`：31 passed, 1 skipped；含真实停止、历史重开、恢复点击、失败步骤重试与三档 viewport。
- 全量：`python3 -m pytest -q`：1035 passed, 2 skipped, 3 warnings。
- `git diff --check`：通过；`python3 scripts/check_css.py`：通过。
- 环境注记：残留的 headless Chrome（`agent-browser-chrome-*`）会让 `agent-browser open` 15s 超时并产生一次 `ERROR`（`test_financial_structure_visuals` / `test_chat_trust_flow`）；清理残留进程后单文件与全量均通过，属环境噪声而非代码失败。

### 风险与偏离

- 工具结果缓存按要求整体停用（计划契约中不存在 provider/arguments/as_of 字段）；待 M4 或受控工具步骤扩展字段后再启用，避免以不合规键复用外部结果。
- 研究步骤失败时错误文本仍随 ResearchRun 持久化（仅会话内 endpoint 可读，UI 只展示业务状态）；公开流只发通用阻塞原因，不泄漏内部参数。
- 浏览器 fixture 仍为确定性本地协作方（新增停止/失败/恢复分支），不调用 AI、MCP、网页或真实 RAG 摄取。
