# Task 4 实施报告：工作台即时一致性与质量观测界面

## 变更

- 工作台打开时只读加载 `GET /api/research/quality`；质量区域使用 `aria-live="polite"` 和可见图标/文字显示健康评测、负向探针的通过或未通过状态。
- 质量摘要缺失、无效或读取失败时诚实降级为“尚无本地质量摘要。可运行 `python3 scripts/run_chat_evaluation.py` 生成。”；渲染器只读取 `available` 与 health/probe 的 `passed` 布尔值，绝不展示失败原文或用例内容。
- 已打开工作台中，成功保存研究决策及成功撤销研究记忆后均调用 `refreshResearchWorkspaceIfOpen()`；该函数复用当前筛选与既有 request-generation 保护。
- Fact 保存操作改为提交稳定 `fact.id`；旧 Fact 的空 ID 不渲染保存操作。
- 质量区域在 390px 改为单列，并对长文本启用换行；浏览器回归覆盖桌面和 390px 的质量状态、即时保存/撤销、筛选保留与无横向溢出。

## 验证

| 阶段 | 命令 | 输出 |
| --- | --- | --- |
| RED | `python3 -m pytest tests/unit/test_chat_rendering_js.py tests/browser/test_research_workspace_flow.py -q -k 'quality or decision_refresh or stable_id'` | `4 failed, 36 deselected`：旧 Fact 仍有保存操作、质量渲染器/状态区域缺失。 |
| GREEN（聚焦） | 同上 | `4 passed, 36 deselected`。 |
| JS 渲染回归 | `python3 -m pytest tests/unit/test_chat_rendering_js.py -q` | `28 passed`。 |
| 浏览器工作台回归 | `python3 -m pytest tests/browser/test_research_workspace_flow.py -q` | `12 passed`；新用例在 `1280x900` 与 `390x844` 覆盖质量状态、保存/撤销即时行刷新、筛选保留和无横向溢出。 |
| 静态检查 | `node --check webapp/static/app.js && node --check webapp/static/chat_rendering.js && python3 scripts/check_css.py && git diff --check` | 通过；CSS 检查输出 `✅ webapp/static/style.css 结构检查通过`。 |

## 风险

- 质量摘要仍为显式本地离线评测产物；本任务不会在服务启动或请求中执行评测。
- 全量项目验收、功能目录/README 更新和合入 main 属于 Task 5，未实施。
- 全局任务台账工具（`create_task_ledger_entry` / `update_task_ledger_entry`）不在当前工具集中，未登记全局台账；本地 SDD 进度台账已更新。

## Fix round 1（计划/设计约束 P2）

- 可用质量摘要现在显示经 `escapeHtml` 转义的 `generated_at`，并与不可用状态一样始终显示精确的本地刷新命令 `python3 scripts/run_chat_evaluation.py`；只消费 health/probe 的聚合 `passed` 字段，不回显失败原文。
- `refreshResearchWorkspaceIfOpen()` 现在只有在 `#research-workspace` 和其父级 `#page-chat` 均存在且均未带 `hidden` 时才重新加载。因此离开聊天页后异步保存或撤销完成，不会刷新不可见工作台。
- 新增 Node 判别回归，分别验证可用摘要的时间戳转义/命令/失败文本隔离，以及聊天页隐藏时不触发刷新。

| 阶段 | 命令 | 输出 |
| --- | --- | --- |
| RED | `python3 -m pytest tests/unit/test_chat_rendering_js.py -q -k 'quality_renderer_shows_safe_health_and_probe_statuses or refresh_workspace_requires_visible_chat_page_and_workspace_panel'` | `2 failed, 27 deselected`：可用摘要未显示时间戳和命令；聊天页隐藏仍触发刷新。 |
| GREEN（聚焦） | 同上 | `2 passed, 27 deselected`。 |
| JS 渲染回归 | `python3 -m pytest tests/unit/test_chat_rendering_js.py -q` | `29 passed`。 |
| 浏览器工作台回归 | `python3 -m pytest tests/browser/test_research_workspace_flow.py -q` | `12 passed`。 |
| 静态检查 | `node --check webapp/static/app.js && node --check webapp/static/chat_rendering.js && git diff --check` | 通过，无输出。 |

本修复未实施 Task 5，未推送或合并。

## Fix round 2（审查 P1）

- 质量区域在可用和不可用状态均转义显示可直接执行的完整本地命令：`python3 scripts/run_chat_evaluation.py --fixture tests/fixtures/chat_eval_cases.json --output data/research_quality_summary.json`。
- 抽取 `researchWorkspacePanelIsVisible()`，让 `refreshResearchWorkspaceIfOpen()` 与 `refreshResearchMemoryPanel()` 复用同一 `#page-chat` 和 `#research-workspace` 可见性谓词；成功保存决策或撤销后若已离开聊天页，不会读取或改写隐藏的研究记忆面板。
- 新增判别回归：完整命令参数必须在两种质量状态下位于转义的 `<code>` 显示中；记忆刷新和工作台刷新必须共享谓词，且聊天页或面板隐藏时均不调用 `loadResearchMemory()`。

| 阶段 | 命令 | 输出 |
| --- | --- | --- |
| RED | `python3 -m pytest tests/unit/test_chat_rendering_js.py -q -k 'quality_renderer_shows_safe_health_and_probe_statuses or refresh_memory_panel_requires_visible_chat_page_and_workspace_panel'` | `2 failed, 28 deselected`：旧命令缺少 fixture/output 参数，且尚无共享可见性谓词。 |
| GREEN（聚焦） | 同上 | `2 passed, 28 deselected`。 |
| JS 渲染回归 | `python3 -m pytest tests/unit/test_chat_rendering_js.py -q` | `30 passed`。 |
| 浏览器工作台回归 | `python3 -m pytest tests/browser/test_research_workspace_flow.py -q` | `12 passed`。 |
| 精确本地命令 | `python3 scripts/run_chat_evaluation.py --fixture tests/fixtures/chat_eval_cases.json --output data/research_quality_summary.json` | 成功；输出 `quality summary written: data/research_quality_summary.json`。 |
| 静态检查 | `node --check webapp/static/app.js && node --check webapp/static/chat_rendering.js && git diff --check` | 通过，无输出。 |

范围：未实施 Task 5，未推送或合并；全局任务台账工具不在当前工具集中，本地 SDD 台账已更新。
