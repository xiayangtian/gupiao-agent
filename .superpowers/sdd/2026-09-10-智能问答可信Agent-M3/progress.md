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
| 第二轮复审修复 | 已完成 | 见下方 RED | 1047 passed, 2 skipped | `5cb6674`、`922f8ca`、`eb421e7` | 无 |

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

## 第二轮复审修复（review → 根因 → RED/GREEN）

约束：不实现 M4、不推送/合并、不调度子代理；未放宽 Scope/ToolPolicy，未用 fake 绕过生产 ResearchAgent/RagQA，未新增依赖，普通聊天路径未改动。

| 复审项 | 根因 | RED（最小失败回归） | GREEN | 提交 |
|---|---|---|---|---|
| P1-1 二次恢复必然 409 缺策略 | 恢复端点在第一条持有 run id 的助手消息上读 `tool_policy`，读不到就 `break`；而 `ResearchAgent.resume` 落盘的 AnswerRun 用 `intent/policy=None`，于是首次恢复的轮次写入 `tool_policy: null`，第二次恢复必然 409 | `python3 -m pytest tests/unit/test_server_api.py -q -k repeated_resume` → `1 failed`（第二次 409 `缺少原始工具策略`） | 同命令 → `1 passed`（第二次 200/completed，两次恢复都传到同一冻结策略） | `922f8ca` |
| P1-2 恢复重放错误问题 | 端点用 `_last_user_question(session)`（会话最后一条用户消息）；停止研究后追问其他问题就会把追问当研究问题重放，找不到时也没有 fail-closed | `python3 -m pytest tests/unit/test_server_api.py -q -k 'replays_the_question or fails_closed_when_the_run'` → `2 failed`（重放 `农业银行今天股价是多少？`；无提问轮次时返回 200） | 同命令 → `2 passed`（重放原研究问题、追加轮次也是原问题；无提问轮次 409） | `922f8ca` |
| P2-3 恢复用空 lambda handler | 恢复端点自建 handler，`normalize/compare/verify` 全是 `lambda *_: {}`，冲突检测从不执行，恢复后的 conflicts/verification 与首次运行不一致 | `python3 -m pytest tests/unit/test_server_api.py -q -k same_controlled_handlers` → `1 failed`（`done["conflicts"]` 为空，compare 步骤无 conflicts） | 同命令 → `1 passed`；`test_research_task_uses_policy_gated_rag_evidence_and_public_step_events` 仍通过 | `922f8ca` |
| P2-4 executor 状态机容错 | 并行批次里 sibling 失败先把 run 置 `failed`，随后 stop_event 触发时仍执行 `transition("stopped")`；`execute()` 对 stopped/failed/partial 直接进 `_run`，在 verifying 转换处抛非法转换 | `python3 -m pytest tests/unit/test_research_executor.py -q -k 'sibling_failure_then_stop or routes_resumable_terminal'` → `2 failed`（`cannot transition failed to stopped`） | 同命令 → `2 passed`（停在合法可恢复终态；execute 走 resume 只重跑未完成步骤） | `5cb6674` |
| P2-5 编排层硬编码 retrieve/answer id | `research_agent._execute` 用 `step_id == "answer"` 取结论，server 检索桥/回答 handler 用 `step_id == "retrieve"`；模型自定义 id 的计划会丢失结论 | `python3 -m pytest tests/unit/test_research_agent.py -q -k finds_the_answer_step_by_kind` → `1 failed`（status 为 `partial`） | 同命令 → `1 passed`；`grep 'step_id == "' webapp/` 只剩 planner 的 kind 依赖表 | `5cb6674`、`922f8ca` |
| P2-6a 运行中步骤持久化覆盖 | 既无断言区分「外部调用前持久化」与「仅 completed 后持久化」 | `python3 -m pytest tests/unit/test_research_executor.py -q -k persisted_running` → 直接通过（现状已正确）；去掉 running 落盘即失败，属覆盖加固 | 同命令 → `2 passed`（含并行批次同一条记录内两步 running） | `5cb6674` |
| P2-6b 完成/不可恢复仍渲染死按钮 | `renderResearchRecovery` 只看 AnswerRun 状态（`stopped/partial/failed`）；当研究运行已 completed（如 `_mark_research_stopped` 回写完成的 run）时仍渲染可点击按钮，点击必 409；恢复成功后旧按钮也一直留在页面上 | `python3 -m pytest tests/unit/test_chat_rendering_js.py -q -k dead_button` → `1 failed`（completed 的研究状态仍返回按钮 HTML） | 同命令 → `1 passed`；`tests/browser/test_research_agent_flow.py` 断言已完成会话 0 个按钮、恢复成功后 0 个按钮 | `eb421e7` |
| P2-7 恢复流没有协作取消 | 恢复端点直接 `await asyncio.to_thread(...)`，无 `Request` 参数、无断开观察，客户端停止无法让阻塞中的外部调用按 stopped 落盘 | `python3 -m pytest tests/unit/test_server_api.py -q -k cancels_a_running_step` → `1 failed`（`resume_research_run() takes 2 positional arguments but 3 were given`）；移除断开轮询后同一用例以「恢复流未观察断开」失败 | 同命令 → `1 passed`（外部调用返回后按 stopped 落盘，检索步骤为 stopped，流中有 `research_blocked`） | `922f8ca` |

### 命令日志（第二轮修复）

- 相关单测：`python3 -m pytest tests/unit/test_research_models.py tests/unit/test_research_planner.py tests/unit/test_research_executor.py tests/unit/test_research_agent.py tests/unit/test_research_store.py tests/unit/test_chat_rendering_js.py tests/unit/test_server_api.py -q -k 'research or executor or rendering or resume'`：68 passed, 127 deselected。
- 恢复/研究 API 子集：`python3 -m pytest tests/unit/test_server_api.py -q -k 'research or resume'`：14 passed。
- 浏览器流：`python3 -m pytest tests/browser/test_research_agent_flow.py -q`：4 passed（真实停止、历史重开、失败步骤重试、恢复后面板、1280x900/768x1000/390x844 三档无溢出与控制台/页面错误为空）。
- 全量：`python3 -m pytest -q`：1047 passed, 2 skipped, 3 warnings（skip 为宿主机 `Chrome --dump-dom` 超时与 `GOLDEN_REPORT_DIR` 未设置，与本次改动无关）。
- `git diff --check HEAD~3`：通过；`python3 scripts/check_css.py`：通过。

### 风险与偏离（第二轮）

- 恢复时的检索参数：原 `body.filters`/`priority_report_id` 没有持久化契约，恢复以冻结 Scope + 冻结 ToolPolicy 重新检索（权限边界未变化），但不再携带当时的额外过滤条件；未新增任何持久化字段。
- 无提问轮次的历史运行（只有持有 run id 的助手消息）改为 409 业务化拒绝，是 fail-closed 的刻意行为，不是兼容性回归（此前会拿会话里任意最新问题重放）。
- P2-4 只保证并行批次在停止信号下落到合法可恢复终态；停止瞬间尚未处理的 sibling 结果不纳入本步（失败路径的 sibling 保留仍由既有 `parallel_failure_persists_*` 回归覆盖）。
- `execute()` 对 `stopped/partial/failed` 现在等价于 `resume()`；对 `completed/verifying/awaiting_input` 仍抛 `ValueError`（终态不可执行）。
