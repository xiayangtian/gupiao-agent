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
