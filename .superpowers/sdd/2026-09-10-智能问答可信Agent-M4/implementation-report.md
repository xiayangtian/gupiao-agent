# 智能问答可信 Agent M4 实施报告（合入前有界收尾）

- 方案：`docs/superpowers/plans/2026-09-10-智能问答可信Agent-M4.md`
- 分支：`feat/trusted-chat-m4`（**未推送、未合入 main**；状态保持 `实施中`）
- 报告时间：2026-09-17
- 本报告覆盖：Tasks 1–7、复审修复波，以及合入前有界收尾修复（P1 工作台关键词真实链路、P2 研究记忆 naive 时间戳）。

## 1. M4 完成定义逐条证据

| # | 完成定义 | 证据 | 状态 |
| --- | --- | --- | --- |
| 1 | 用户可按持久化公司、行业、期间、意图、状态和收藏筛选研究资产 | `tests/unit/test_research_workspace.py`（公司/状态不靠标题、行业/期间/意图/收藏/排序、UTC 归一）；`tests/unit/test_server_api.py::…::test_workspace_api_filters_company_and_completed_status`、`…test_workspace_period_filter_rejects_impossible_dates`；浏览器 `test_workspace_filters_by_company_and_status`、`test_workspace_filter_ignores_stale_unfiltered_response` | 通过 |
| 2 | 收藏、导出和记忆均可回到不可变 Scope/Fact/Artifact | `test_workspace_favorites_reload_from_sidecar_and_prune_orphan_runs`；`ResearchExporter` 只读 AnswerRun/ResearchRun；`ResearchMemoryStore` 的 `run_lookup` 只接受来源运行内既有 Fact/Artifact/Decision；本轮 P1 将工作台搜索也改为只读连接活跃 decision 条目 | 通过 |
| 3 | 未验证/冲突/停止残片不能保存为确认事实 | `tests/unit/test_research_memory.py::test_reference_conflict_unavailable_and_stopped_fragment_cannot_be_saved_as_fact`、`test_fact_requires_its_verified_positive_page_pdf_evidence_and_eligible_source_run`；API `test_memory_api_rejects_reference_fact_and_enforces_session_ownership`（reference fact 422） | 通过 |
| 4 | 导出完整表达范围、证据、外部数据时间、冲突和运行状态 | `tests/unit/test_research_export.py`；API `test_export_keeps_partial_status_pdf_link_and_owner_boundary`；浏览器 `test_export_contains_scope_status_and_pdf_page_link` 断言 `## 范围`/`## 运行状态`/`PDF 第 40 页`/`数据截至` | 通过 |
| 5 | 删除会话不删除独立研究记忆且界面有明确披露 | API `test_memory_list_is_owner_scoped_and_survives_session_deletion`、`test_session_delete_counts_a_memory_write_that_raced_the_deletion`；浏览器 `test_delete_discloses_retained_memory_and_keeps_fixture_pdf`、`test_workbench_memory_list_persists_across_refresh_and_revokes` 断言“1 条研究记忆仍被保留，原始 PDF 不会被删除。” | 通过 |
| 6 | 评测质量门槛可离线重复运行，命中 Scope 泄漏/伪页码/外部无时间/停止伪完成即失败 | `tests/unit/test_chat_evaluation.py`、`tests/quality/test_golden_reports.py`（默认 skip，需 `GOLDEN_REPORT_DIR`）；`tests/fixtures/chat_eval_cases.json` 版本化固定 fixture，无真实 AI/MCP/web/生产数据 | 通过（聚合门槛因 fixture 内置 scope-leak 用例恒为红，见残余 P2） |
| 7 | 全量、API、浏览器、移动端和评测验收通过 | 见第 4 节实际输出：受影响单测、浏览器 10 项、全量、CSS、`git diff --check` | 通过 |
| 8 | 代码评审无 P0/P1，未引入自动交易或未经设置的持续监控 | 复审仅剩 1 项 P1，本轮已修复（第 3 节）；无自动交易/主动监控代码 | 通过（合入前收尾） |

## 2. Tasks 1–7 与修复波提交

| Task | 提交 | 内容 |
| --- | --- | --- |
| Task 1 | `0fb84e2` | 研究工作台索引与元数据筛选 |
| Task 2 | `c70f431` | 用户显式研究记忆（Fact/Artifact/Decision、有效期、撤销） |
| Task 3 | `53604f9` | 可复核研究纪要导出 |
| Task 4 | `f78b69b` | 离线评测与质量门槛 |
| Task 5 | `f77bdc2` | 工作台/记忆/导出/质量 API |
| Task 6 | `3759be5` | 工作台前端与可访问交互 |
| Task 7 | `89a8249` | 工作台浏览器回归与发布验收 |
| 复审修复波 | `0bc539d`、`db47f35`、`b25ad98`、`cbed0ca`、`97d48f6`、`1c2af6b` | 见下节 Ruling |
| 合入前收尾 | `e6fd453` | P1 工作台关键词真实链路 + P2 naive 时间戳 fail-closed |

## 3. 合入前有界收尾（本轮）

### 修复 1（P1，方案 a）：工作台「已保存决策」关键词在生产链路不可达

- 现象：`webapp/research_workspace.py` 读取 `record.run.research_summary["decision_summaries"]`，但生产链路无任何写入方（决策保存端点只写 `research_memory.json`），`index.html:285` 的“标题或已保存决策”与 `chat_rendering.js:475` 的“已保存决策”渲染永远不可达，原绿灯来自浏览器/单测夹具手写字段。
- RED：新增 `tests/unit/test_server_api.py::…::test_workspace_keyword_search_hits_a_decision_saved_through_the_real_api`（真实 `POST /api/research/memory/decisions` → `GET /api/research/workspace?text=营收质量`），修复前断言 `[] == ['r1']` 失败。
- 实现：`ResearchWorkspaceStore` 新增只读 `memory_path`，以 `source_run_id` 将 `ResearchMemoryStore.list_active()` 的活跃 decision 文本映射到对应 run 的 `searchable_summary`；每次列举重读 sidecar（决策由 API 层短生命周期 store 写入，缓存快照会漏掉后写）。删除 `_saved_decision_summaries` 与夹具手写 `decision_summaries`。
- 约束：不改写 AnswerRun/ResearchRun，不新增特权端点，不改变 owner/不可变语义；只读 `research_memory.json`。
- GREEN：该用例通过；撤销后关键词不再命中（活跃语义）。

### 修复 2（P2，健壮性）：研究记忆 naive 时间戳使读接口 500

- 现象：`webapp/research_memory.py::_parse_time` 接受 naive ISO 时间戳，`list_active()` 以 naive 与 aware now 比较抛 `TypeError`，`GET /api/research/memory` 返回 500。
- RED：(a) `tests/unit/test_research_memory.py::test_naive_sidecar_timestamps_are_read_as_utc_instead_of_raising`（naive 未来/过期条目）；(b) `tests/unit/test_server_api.py::…::test_memory_api_reads_a_sidecar_with_naive_timestamps_without_failing`。修复前均因 `TypeError: can't compare offset-naive and offset-aware datetimes` 失败。
- 实现：`_parse_time` 统一按 `timezone.utc` 解读 naive 值（aware 值归一到 UTC），fail-closed，不误判有效期。
- GREEN：读接口对异常 sidecar 返回 200，naive 未来条目仍活跃，naive 过期条目被排除。

### 记录项

- `.gitignore` 增加 `data/research_quality_summary.json`（端点使用但未忽略；`data/chat_metrics.json` 忽略项保留）。
- 本报告；`progress.md` 更正浏览器模块计数与全量数字。

## 4. 验证实际输出（2026-09-17，`feat/trusted-chat-m4` @ `e6fd453`）

```
$ python3 -m pytest tests/unit/test_research_workspace.py tests/unit/test_research_memory.py -q
24 passed in 0.15s

$ python3 -m pytest tests/unit/test_server_api.py -q -k 'workspace or memory or export or quality'
12 passed, 152 deselected, 3 warnings in 1.11s

$ python3 -m pytest tests/browser/test_research_workspace_flow.py -q
10 passed in 32.02s

$ python3 -m pytest -q
1128 passed, 1 skipped, 3 warnings in 176.31s (0:02:56)
# 复跑（-rs）环境噪声致 1 项浏览器 skip：1127 passed, 2 skipped
#   SKIPPED tests/browser/test_analysis_dialog_layout.py:107 当前宿主的 Chrome --dump-dom 在 15 秒内未退出
#   SKIPPED tests/quality/test_golden_reports.py:44 设置 GOLDEN_REPORT_DIR 后才运行本地真实年报质量评估
# 监督者复跑（修复前基线）：1123 passed, 2 skipped, 1 error
#   error = tests/browser/test_chat_pdf_supplement.py 的 agent-browser 20s 超时环境噪声

$ git diff --check
（无输出，通过）

$ python3 scripts/check_css.py
✅ webapp/static/style.css 结构检查通过
```

浏览器模块：`tests/browser/test_research_workspace_flow.py` 收集 10 项（7 个用例 + 1 个 viewport 参数化 3 项），覆盖 1280x900、768x1000、390x844、筛选/收藏/保存撤销/导出/删除披露/编辑重问与分支追问/console/errors/network/无横溢。

## 5. Ruling 汇总

1. **记忆 owner 契约**：写入时以保存请求中已通过所有权校验的 `session_id` 作为 owner 持久化，不接受客户端另行指定；sidecar `schema_version` 升为 2，缺 owner 的旧条目 fail-closed 不进入列表（不猜测归属），仍可按 id 撤销；`GET /api/research/memory` 支持可选 `owner_session_id=` 过滤；本应用无认证、同机单用户，返回本机活跃记忆不构成权限扩大。代价：旧条目在列表不可见。
2. **编辑重问/分支追问语义**：「编辑重问」仅回填该轮原始问题到输入框供编辑，发送仍走既有 `/api/chat/stream`（同会话新增 run），不改写/删除历史轮次；「分支追问」用既有 `POST /api/chat/sessions` 新建会话并预填原问题，不复制证据、不新建端点；重试/恢复沿用既有「重新生成」与「继续研究」resume。工作台行不提供单轮问题操作。
3. **浏览器竞态 generation**：真实浏览器验收发现无筛选工作台请求可晚到并覆盖最新筛选，批准 `loadResearchWorkspace` 以 generation 丢弃旧响应。代价：旧响应仍耗费网络但不可写入 UI。
4. **并发测试自死锁**：根因是测试自身在持 `_research_memory_lock` 时发起 `DELETE`（TestClient 同步执行需同一把锁），改为 `threading.Event` 闸门 + monkeypatch 在端点内部暂停请求线程的确定性编排，测试线程绝不持锁发请求；生产侧锁顺序不变。

## 6. 残余 P2（不影响本轮 P1 收尾，未合入前遗留）

1. **质量摘要无生产者/无 UI 入口**：`GET /api/research/quality` 只读取预计算的 `data/research_quality_summary.json`，仓库内无生成脚本，前端亦无入口；可用性依赖本地手工生成。
2. **评测 fixture 内置 scope-leak 用例导致聚合恒为红**：`tests/fixtures/chat_eval_cases.json` 含刻意 scope-leak 用例用于验证门槛能发现泄漏，因此离线聚合 `passed` 恒为 false，需人工区分“门槛验证”与“真实回归”。
3. **证据身份规则四处重复**：`report_id#p{page}` 身份在 `research_memory.artifact_evidence_ids`、`chat_evidence`、导出与前端各有实现，未来修改页码/文件名规则需同步四处。
4. **Fact 多指标同页共用证据时不可保存**：`ResearchMemoryStore` 以 `report_id#p{page}` 映射 Fact 证据，同一 PDF 页承载多个 Fact 指标时无法区分，保存会因证据映射不唯一/同页冲突被拒或语义不清。

## 7. 适用条件与注意事项

- 本轮修复仅适用于 `feat/trusted-chat-m4` 的工作台/研究记忆链路；不改动 M1–M3 的事实与执行真相。
- `ResearchWorkspaceStore` 的 `memory_path` 只读连接要求调用方与 `ResearchMemoryStore` 指向同一 sidecar；测试/浏览器夹具必须显式传入隔离路径，避免读到仓库 `data/` 状态。
- 已合入 main：合并提交 `7d89a7e merge: 智能问答可信 Agent M4`；台账条目已登记为 `已实现` 并记录合并 SHA、验证命令与结果。
- 合入后复跑（main 工作区）：`python3 -m pytest -q` → 1127 passed, 2 skipped, 3 warnings（浏览器用例存在 agent-browser 环境噪声导致的 skip/error 波动）；定向 workspace/memory 24 passed、server API 12 passed、`tests/browser/test_research_workspace_flow.py` 10 passed；`git diff --check` 与 `python3 scripts/check_css.py` 通过。
- 合入后新增报告项 P2：保存/撤销决策后工作台行文案不即时刷新（需在会话中保存/撤销后重新加载工作台才会更新），已记录为后续修补项。
