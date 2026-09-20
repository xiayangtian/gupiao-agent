# SDD ledger — plan: docs/superpowers/plans/2026-09-18-移除研究记忆与决策.md

## Preflight interface scan

| Tasks / interface | Producer → consumer | Finding / ruling |
| --- | --- | --- |
| Task 1 → Task 2 | Task 1 removes memory routes/store; Task 2 removes their UI callers | Compatible: frontend task must remove all `/api/research/memory/` calls after backend route removal. |
| Task 1 → Task 3 | Task 1 removes store; Task 3 removes fixture store injection | Compatible: Task 3 must not import or instantiate `ResearchMemoryStore`. |
| Task 2 → Task 3 | Task 2 removes UI selectors/actions; Task 3 rewrites browser flows | Compatible: browser checks switch from persistence behavior to absence assertions. |
| Task 3 → post-merge docs | Task 3 validates branch; docs may only advertise merged functionality | Ruling: do not modify README/FEATURE-CATALOG on this unmerged branch; record post-merge update requirements in implementation report — AGENTS.md requires those documents to describe only merged functionality — cost if wrong: capability documentation remains stale until local merge, but avoids claiming an unmerged destructive change is released. |

All task-local tests and described code agree with the design. No implementation conflict found.

## Task 1 implementation — completed locally, unmerged

提交：`f8f8b94 feat: 移除研究记忆后端能力`（未推送/合并）。

**关键 Ruling（原文）：**允许且必须最小解耦 `webapp/research_workspace.py` 及其直接单元/fixture-safety 测试，删除 ResearchMemoryStore 导入、构造/读取逻辑和直接断言，确保删除 `research_memory.py` 后 server 可导入；不得提前删除 Task2 DOM/JS/CSS 或 Task3 浏览器交互，除非消除直接 Python import 失败所必需。

- 已删除后端记忆模块、专属单测、存储/锁/API/删除会话计数；工作台和浏览器 fixture 已作上述最小解耦。
- 已注册启动时精确清理 `data/research_memory.json`；`OSError` 会记录简短诊断并抛出 `RuntimeError`，服务不会静默启动。
- TDD RED：`python3 -m pytest tests/unit/test_server_api.py -k 'legacy_research_memory_sidecar or removed_research_memory_routes' -q`（10 failed）。
- GREEN：同一命令（11 passed）；`python3 -m pytest tests/unit/test_server_api.py -q`（172 passed）；`python3 -m pytest tests/unit/test_research_workspace.py tests/unit/test_browser_test_safety.py tests/unit/test_chat_rendering_js.py -q`（50 passed）；`git diff --check` 通过。
- 默认 `data/research_memory.json` 已确认不存在。Task 2/3 未实施；README、FEATURE-CATALOG 和设计台账按未合入分支规则未更新。

Task 1: complete (commits 81fe093..f8f8b94, review clean)

## Task 2 implementation — completed locally, unmerged

提交：`1babf9f feat: 移除研究记忆工作台入口`（未推送/合并）。

- 已从工作台、聊天渲染与事件处理移除保存事实/证据/决策、记忆读取/撤销、记忆面板、决策对话框及所有 `/api/research/memory/` 前端调用。
- 保留筛选、收藏、打开回答、导出、质量观测、`researchWorkspacePanelIsVisible()` 与工作台 generation 防竞态保护；保留 390px 工作台操作布局。
- TDD RED：`python3 -m pytest tests/unit/test_chat_rendering_js.py -k 'no_memory_or_decision' -q`（1 failed，仍渲染 `save-decision`）。
- GREEN：同一命令（1 passed，26 deselected）；`python3 -m pytest tests/unit/test_chat_rendering_js.py -q`（27 passed）；`node --check webapp/static/app.js && node --check webapp/static/chat_rendering.js`、`python3 scripts/check_css.py` 和 `git diff --check` 均通过。
- Task 3 未实施；浏览器 fixture/流程、合入后文档和全量验收仍保持后续范围。详见 `task-2-report.md`。

Task 2: complete locally (commit 1babf9f, unmerged)

Task 2: Ruling: reviewer P1 browser tests still assert removed memory UI/API — deferred to Task 3 because the approved plan explicitly assigns browser fixture/flow migration to Task 3 — cost if wrong: Task 2 alone cannot pass browser suite, but Task 3 immediately replaces those assertions before any merge.
Task 2: complete (commits f8f8b94..1babf9f, Task 3-owned P1 carried forward)

## Task 3 implementation — in progress

- Began Task 3 on branch `feat/remove-research-memory`; task-ledger integration is unavailable in this environment (未登记).
- Scope is limited to browser fixture/flow migration, Task 3 report, and this progress ledger. Per unmerged-branch ruling, README, FEATURE-CATALOG, and DESIGN-REGISTRY remain unchanged; the report records their post-merge actions.
- TDD RED: `python3 -m pytest tests/unit/test_browser_test_safety.py -k 'workspace_browser_fixture' -q` failed (1 failed) while the browser flow still contained removed UI/API identifiers.
- GREEN: the same command passed (1 passed, 12 deselected); safety suite (13 passed), workspace browser suite (12 passed), server/rendering regression (199 passed), full suite (1145 passed, 2 skipped), `git diff --check`, and CSS check all passed. Browser acceptance recorded no console/page errors, failed same-origin requests, or native downloads.

Task 3: complete locally, unmerged; task report: `task-3-report.md`. README, FEATURE-CATALOG, and DESIGN-REGISTRY require the post-merge actions recorded there. Task-ledger integration remains unavailable (未登记).

## 暂停点（2026-09-20，用户要求）

- 分支 `feat/remove-research-memory`，HEAD `87b34a4`，工作区干净，无未提交改动。
- Task 1（f8f8b94）、Task 2（1babf9f）、Task 3（87b34a4）均已提交并完成独立审查；Task 3 审查结论 OK with notes，遗留 3 个 P2。
- 暂停时正在执行 Task 3 fix round 1（run 38416e4b，已被用户要求中断，未产生提交）。
- 恢复动作：重新派发 Task 3 fix round 1，修 3 个 P2 — (1) test_research_workspace_flow.py:274-294 两个无法失败的 chat 级缺失断言；(2) DESIGN-REGISTRY.md 状态改为 `实施中` 并同步未完成索引；(3) task-3-report.md 修正下载与偏离表述。随后范围复审、最终全分支审查、本地合入与合入后文档更新。

Task 3: fix round 1/5 (3 P2 addressed directly after two runner startup-confirm failures; browser chat absence probe now opens completed session, registry is 实施中, report wording corrected). Ruling: controller applied this bounded fix only after user explicitly approved direct implementation; cost if wrong: fix receives same scoped verification below.
Task 3: fix round 1 re-review P2 corrected — task report now distinguishes unchanged README/FEATURE-CATALOG from registry 实施中 update.
