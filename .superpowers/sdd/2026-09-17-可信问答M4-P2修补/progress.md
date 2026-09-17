# SDD ledger — plan: docs/superpowers/plans/2026-09-17-可信问答M4-P2修补.md

## Preflight

| Tasks / interface | Producer → consumer | Finding / ruling |
| --- | --- | --- |
| T1 → T2/T4 | Fact.id and evidence identity → memory/API/UI consumers | Compatible; old Fact must read with empty ID and fail closed only for memory save. |
| T2 → T3/T4 | Shared evidence identity and Fact-ID memory API → evaluation/UI | Compatible; server/API must preserve owner checks. |
| T3 → T4/T5 | Health/probe safe summary → API/UI/validation | Compatible; command is explicit and atomic, never runs in a request. |
| T4 → T5 | UI refresh and quality status → browser QA | Compatible; refresh must retain filters and generation guard. |
| T5 | Full validation/documentation | Compatible; registry stays 实施中 until merged. |

Ruling: Execute in plan order because Fact ID/evidence identity is the compatibility boundary for memory, evaluator, API and UI — cost if wrong: downstream changes must be revised before merge.

Task 1: complete (commits bfea3f3..95487aa, producer review clean for Task 1 scope)
Task 1: Ruling: reviewer P1 that memory/export/evaluator still use legacy evidence helpers and old Fact remains savable is plan-mandated Task 2 work, not a Task 1 defect — Task 2 must migrate all consumers and add old-Fact/unsafe-URL integration coverage; cost if wrong: an uncompleted Task 2 would leave unsafe saves enabled and block merge.
Task 1: minor deferred: add an explicit full supplied `fact_` ID round-trip test in Task 2; cost if wrong: preservation behavior could regress without direct coverage.

## Task ledger

| Task | Status | Commit | Validation | Notes |
| --- | --- | --- | --- | --- |
| Task 1 — 稳定 Fact ID 与统一证据身份 | 已完成 | `95487aa` | RED: focused collection failed as expected (`ModuleNotFoundError`); GREEN: `36 passed`; related regression: `44 passed`; `git diff --check` passed | 新 Fact 生成 `fact_` ID；旧序列化 Fact 保持空 ID。完整记录见 `task-1-report.md`。 |
| Task 2 — 使用 Fact ID 保存研究记忆并迁移后端消费者 | 已完成 | `HEAD` | RED: `2 failed, 1 passed`（legacy Fact 未拒绝、Fact ID API 查找失败）；GREEN: focused `10 passed`; Task 2 regression `210 passed`; related contracts `246 passed`; `git diff --check` passed | memory 拒绝空 ID legacy Fact；API 精确匹配 Fact ID；memory/export/evaluator 复用 evidence_identity；覆盖 unsafe PDF URL、同页多 Fact 与完整 supplied `fact_` ID round-trip。完整记录见 `task-2-report.md`。 |

## Task 2 Fix round 1（审查 P1）

- 新增共享 web 身份 monkeypatch 回归后，确认旧的 evaluator 本地 URL 拼接会失败（`external_facts_missing_source == 1`）。
- `_external_evidence_ids()` 现为每个 web artifact 调用 `artifact_evidence_ids()`；仅 evaluator 自有 tool identity 保留本地拼接。
- 验证：`python3 -m pytest tests/unit/test_research_memory.py tests/unit/test_research_export.py tests/unit/test_chat_evaluation.py tests/unit/test_server_api.py -q` → `211 passed, 3 warnings`；`git diff --check` 通过。
- 范围：未实施 Task 3--5，未推送或合并；详细记录见 `task-2-report.md`。
