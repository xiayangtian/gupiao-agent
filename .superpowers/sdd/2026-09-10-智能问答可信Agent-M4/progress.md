# SDD ledger — plan: docs/superpowers/plans/2026-09-10-智能问答可信Agent-M4.md

## Preflight

| Tasks / interface | Producer → consumer | Finding / ruling |
| --- | --- | --- |
| T1 → T5/T6/T7 | Workspace index and favorites → API/UI/browser flows | Compatible; T1 must expose owner/session metadata without duplicating raw messages. |
| T2 → T5/T6/T7 | Explicit memory store → protected API/UI/browser flows | Compatible; memory must remain independent from deleted sessions. |
| T3 → T5/T6/T7 | Exporter → export API/UI/browser flows | Compatible; exporter is built from immutable AnswerRun/ResearchRun only. |
| T4 → T5/T7 | Offline evaluator/quality summary → quality API/browser fixture | Compatible; endpoint loads precomputed local summary and must not run a model. |
| T5 → T6/T7 | API contracts → UI/browser flows | Compatible; every run lookup remains session-owner checked. |
| T6 → T7 | Workbench DOM/actions → browser acceptance | Compatible; UI must preserve ordinary chat and mobile no-overflow behavior. |
| T1 | Metadata-only index and text search | Compatible with global constraints; raw PDF/external bodies excluded from text search. |
| T2 | Explicit save eligibility and expiry | Compatible with global constraints; unverified/reference/conflict/stopped facts rejected. |
| T3 | Full evidence export and Markdown escaping | Compatible with global constraints; partial/stopped/failed status stays visible. |
| T4 | Versioned fake-only evaluation | Compatible with global constraints; no real AI/MCP/web/production data. |
| T5 | Ownership and session deletion semantics | Compatible with global constraints; deletion retains independent memories and reports count. |
| T6 | Accessible workbench controls | Compatible with global constraints; confirmation for destructive actions and no color-only state. |
| T7 | Deterministic browser QA | Compatible with global constraints; no browser artifacts committed. |

Ruling: Execute M4 strictly in plan order because Tasks 5–7 consume the contracts produced by Tasks 1–4; all changes remain on feat/trusted-chat-m4 — cost if wrong: later tasks may need contract adaptation, captured by task review.

## 审查修复波（2026-09-17）— 确定取证与 Ruling

监督者已完成的确定取证（直接采用，未重复复现）：

1. 挂起命令：`python3 -m pytest tests/unit/test_server_api.py -q -k 'workspace or memory or export or quality' -o faulthandler_timeout=45`（不加 `-x` 会永久挂起）。
2. faulthandler 栈：5 个线程中 delete 线程停在 `server.py:2954`、save 线程停在 `server.py:2791`，均在等 `_research_memory_lock`。
3. 根因是**测试自身自死锁**（不是生产锁顺序问题）：`test_fact_save_rechecks_session_ownership_inside_the_coordination_lock` 在 `with server._research_memory_lock:` 内发起 `client.delete(...)`，delete 端点需要同一把锁，而 TestClient 请求在该线程的 portal 上同步执行 → 永久自锁。
4. 生产侧 `webapp/server.py` 改动（锁顺序注释、锁内 owner 复验、period 真实日期校验、quality `failure_codes` 白名单）方向正确，保留。

Ruling A（自死锁修复）：两个并发测试改为**不变量驱动的确定性编排**，测试线程绝不持锁发请求；改用 `threading.Event` 闸门 + monkeypatch 在端点内部暂停请求线程，所有等待均为 `Event.wait(timeout)` / `join(timeout)`，失败给出明确断言，结构上不可能永久挂起。代价：测试需要显式编排时序，比 `sleep` 写法更长，但确定性可重复。

Ruling B（记忆列表作用域，监督者裁决 Q1=选项 C）：

1. 写入时以保存请求中**已通过所有权校验**的 `session_id` 作为 owner 持久化，不接受客户端另行指定；
2. sidecar `schema_version` 升级为 2，缺 owner 的旧条目 fail-closed 不进入列表（不得猜测归属）；
3. `GET /api/research/memory` 返回活跃条目，每条带 `owner_session_id`，支持可选 `owner_session_id=` 过滤（本应用无认证、同机单用户，返回本机活跃记忆不构成权限扩大）；
4. UI 按 owner 分组展示，owner 会话已删除的条目进入「来自已删除会话」分组且仍可撤销；`DELETE /api/research/memory/{id}` 保持按 id 撤销；
5. 不新增特权端点、不改变 run/fact/artifact 不可变语义。

代价：sidecar schema 升级后旧条目在列表不可见（仍保留在文件中、可按 id 撤销）。

Ruling C（编辑重问/重试恢复/分支，监督者确认 Q2）：(a)「编辑重问」仅把该轮原始问题回填输入框供编辑，发送仍走既有 `/api/chat/stream`（同会话新增 run），不得改写或删除历史轮次；(b)「分支追问」用既有 `POST /api/chat/sessions` 新建会话并预填原问题，不复制证据、不新建端点；(c) 重试/恢复沿用既有「重新生成」与「继续研究」resume 端点，语义不变。三项作用于聊天回答块，工作台行不提供单轮问题操作。

## Task ledger

| Task | Status | Branch | Evidence / notes |
| --- | --- | --- | --- |
| M4 Task 1 — 建立研究工作台索引与元数据筛选 | 已完成（待后续总体验收） | `feat/trusted-chat-m4` | 2026-09-16：提交 `0fb84e2`；RED `python3 -m pytest tests/unit/test_research_workspace.py -q`（缺少模块，2）；GREEN 同命令 4 passed；回归 `tests/unit/test_research_workspace.py tests/unit/test_chat_store.py tests/unit/test_research_store.py -q` 19 passed；`git diff --check` 通过。 |
| M4 Task 2 — 建立用户显式研究记忆 | 已完成（待后续总体验收） | `feat/trusted-chat-m4` | 2026-09-16：提交 `c70f431`；Ruling：`ResearchMemoryStore` 使用可选 immutable `run_lookup(run_id) -> AnswerRun | None`，无 lookup、未知 run 或非 completed / partial-but-verified run 均 fail-closed；代价是未来独立调用方未接线会被安全拒绝。RED `python3 -m pytest tests/unit/test_research_memory.py -q`（缺少模块）；GREEN 同命令 6 passed；回归 `tests/unit/test_research_memory.py tests/unit/test_chat_models.py tests/unit/test_research_workspace.py tests/unit/test_chat_store.py tests/unit/test_research_store.py -q` 55 passed；`compileall` 与 `git diff --check` 通过。 |
| M4 Task 3 — 生成可复核研究纪要导出 | 已完成（待后续总体验收） | `feat/trusted-chat-m4` | 2026-09-17：提交 `53604f9`；RED `python3 -m pytest tests/unit/test_research_export.py -q`（缺少模块，5 failed）；GREEN 同命令 5 passed；回归 `tests/unit/test_research_export.py tests/unit/test_chat_models.py tests/unit/test_research_models.py tests/unit/test_research_store.py -q` 41 passed；`compileall` 与 `git diff --check --cached` 通过。 |
| M4 Task 4 — 建立可重复金融问答评测与质量门槛 | 已完成（待后续总体验收） | `feat/trusted-chat-m4` | 2026-09-17：提交 `f78b69b`；RED `python3 -m pytest tests/unit/test_chat_evaluation.py -q`（缺少模块，10 failed）；GREEN 同命令 10 passed；回归 `tests/unit/test_chat_evaluation.py tests/unit/test_chat_models.py tests/unit/test_chat_verifier.py tests/unit/test_research_models.py -q` 58 passed；`compileall`、`git diff --check` 与提交前 `git diff --check --cached` 通过。 |
| M4 Task 5 — 提供工作台、记忆、导出与评测 API | 已完成（待后续总体验收） | `feat/trusted-chat-m4` | 2026-09-17：提交 `f77bdc2`；RED `python3 -m pytest tests/unit/test_server_api.py -q -k 'workspace or memory or export or quality'`（4 failed）；GREEN 同命令 4 passed；全量 server API `tests/unit/test_server_api.py -q` 156 passed；相关 M4 基础模块回归 25 passed；`compileall` 与 `git diff --check` 通过。质量 API 仅读取预计算本地汇总。 |
| M4 Task 6 — 实现研究工作台前端与可访问交互 | 已完成（待后续总体验收） | `feat/trusted-chat-m4` | 2026-09-17：提交 `3759be5`；RED `python3 -m pytest tests/unit/test_chat_rendering_js.py -q -k 'workspace or memory or export'`（3 failed，缺少工作台/记忆/导出渲染器）；GREEN `python3 -m pytest tests/unit/test_chat_rendering_js.py -q`（22 passed）；API 回归 `python3 -m pytest tests/unit/test_server_api.py -q -k 'workspace or memory or export or quality'`（4 passed）；`node --check`、`python3 scripts/check_css.py`、`git diff --check` 通过。Task 7 仍需真实浏览器与 390px 无横溢验收。全局任务台账接口当前不可用，未登记外部条目。 |
| M4 Task 7 — 工作台浏览器回归与发布验收 | 已完成（待合入 main） | `feat/trusted-chat-m4` | 2026-09-17：提交 `89a8249`；Ruling：真实浏览器验收发现无筛选工作台请求可晚到并覆盖最新筛选，批准以 `loadResearchWorkspace` generation 丢弃旧响应（代价：旧响应仍耗费网络但不可写入 UI）。RED：fixture 未准备时浏览器用例 3 failed；竞态确定性回归 1 failed。GREEN：`tests/browser/test_research_workspace_flow.py -q` 10 passed（7 用例 + 1 个 viewport 参数化 3 项；1280x900、768x1000、390x844，筛选/收藏/保存撤销/导出/删除披露/编辑重问与分支追问/console/errors/network/无横溢）；`git diff --check`、`python3 scripts/check_css.py`、`node --check webapp/static/app.js` 通过。全量数字见下节“复审修复波与合入前有界收尾”的更正。全局任务台账接口当前不可用，未登记外部条目。 |

## 复审修复波与合入前有界收尾（2026-09-17）

| 项 | 状态 | 分支/提交 | 证据 |
| --- | --- | --- | --- |
| 复审修复波（6 项 fix） | 已完成（待合入） | `feat/trusted-chat-m4`：`0bc539d`、`db47f35`、`b25ad98`、`cbed0ca`、`97d48f6`、`1c2af6b` | UTC 归一混合时间戳、记忆引用不可变运行与正页码 PDF 证据、导出校验研究步骤与回答运行一致、评测重放版本化 fixture、并发测试自死锁修复、删除会话后重建记忆分组。 |
| 合入前收尾 P1（工作台关键词真实链路） | 已完成（待合入） | `e6fd453` | RED `tests/unit/test_server_api.py::TestResearchWorkspaceMemoryExportQualityApi::test_workspace_keyword_search_hits_a_decision_saved_through_the_real_api` 断言 `[] == ['r1']` 失败（真实 `POST /api/research/memory/decisions` 后搜索为空）；GREEN 该用例通过，撤销后不再命中。 |
| 合入前收尾 P2（研究记忆 naive 时间戳） | 已完成（待合入） | `e6fd453` | RED `tests/unit/test_research_memory.py::test_naive_sidecar_timestamps_are_read_as_utc_instead_of_raising` 与 `tests/unit/test_server_api.py::TestResearchWorkspaceMemoryExportQualityApi::test_memory_api_reads_a_sidecar_with_naive_timestamps_without_failing` 均因 `TypeError: can't compare offset-naive and offset-aware datetimes` 失败；GREEN 通过，读接口对异常 sidecar 返回 200。 |

全量数字更正（本行以最新复跑为准，替代 Task 7 当时的 `1089 passed`）：

- 监督者复跑（修复前基线）：`python3 -m pytest -q` → 1123 passed, 2 skipped, 1 error；该 error 为既有 `tests/browser/test_chat_pdf_supplement.py` 的 agent-browser 20s 超时环境噪声。
- 本轮收尾后复跑：`python3 -m pytest -q` → 1128 passed, 1 skipped, 3 warnings in 176.31s；`-rs` 复跑 → 1127 passed, 2 skipped（环境噪声致 1 项浏览器 skip，`test_analysis_dialog_layout.py` 的 Chrome `--dump-dom` 15s 未退出；`test_golden_reports.py` 默认 skip）。
- 浏览器模块计数更正：`tests/browser/test_research_workspace_flow.py` 现为 **10 项**（含 viewport 参数化），非此前的 8 项。
- 受影响单测：`tests/unit/test_research_workspace.py tests/unit/test_research_memory.py -q` → 24 passed；`tests/unit/test_server_api.py -q -k 'workspace or memory or export or quality'` → 12 passed。
- `git diff --check` 通过；`python3 scripts/check_css.py` 通过。
- 合入前有界收尾报告：`.superpowers/sdd/2026-09-10-智能问答可信Agent-M4/implementation-report.md`。
- 未推送/未合入；DESIGN-REGISTRY 条目保持 `实施中`。

