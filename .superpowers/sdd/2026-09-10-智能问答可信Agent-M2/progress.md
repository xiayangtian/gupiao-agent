# SDD ledger — plan: docs/superpowers/plans/2026-09-10-智能问答可信Agent-M2.md

## Workspace and baseline
- Worktree: /Users/xiayangtian/Desktop/code/gp-agent/.worktrees/trusted-chat-m2
- Branch: feat/trusted-chat-m2
- Merge base: 2429844
- Baseline: `python3 -m pytest tests/unit/test_chat_models.py tests/unit/test_chat_scope.py tests/unit/test_chat_evidence.py tests/unit/test_chat_store.py tests/unit/test_rag_qa.py -q` — 109 passed, 3 pre-existing dependency warnings.

## Preflight interface scan
| Tasks/interfaces | Producer → consumer | Finding / ruling |
| --- | --- | --- |
| T1 → T4/T5 | `IntentDecision`, `ToolPolicy` → policy-gated RAG and server orchestration | Compatible: T1 establishes the contract before its consumers. |
| T2 → T3/T5/T6 | `Fact`, `FactConflict` → verifier, SSE/persistence, rendering | Compatible: T2 precedes all consumers; `FactConflict` is added compatibly to models. |
| T3 → T5/T6 | `VerificationReport` → server degradation and renderer status | Compatible: T3 precedes T5 and T6. |
| T4 → T5 | policy-resolved/structured-tool events → server normalization | Compatible: event names and backwards compatibility are explicitly required. |
| T5 → T6/T7 | persisted run/SSE fields → frontend and browser harness | Compatible: UI and acceptance depend on server lifecycle. |
| T6 → T7 | user-visible states → browser assertions | Compatible: UI precedes browser acceptance. |
| T1 intent lexical rules | `event_attribution` words overlap realtime markers | Ruling: use plan-stated lexical priority for realtime markers; event terms classify event attribution only when no realtime marker is present. Cost if wrong: event questions containing “近期” may use quote policy; verifier still marks external evidence as reference. |
| T2 verification wording | plan says permitted external structured facts are `reference`; initial precondition text says `verified` requires controlled structured external data | Ruling: follow Task 2's explicit and stricter behavior: PDF facts only become `verified`; permitted external structured facts remain `reference`. Cost if wrong: a future provider trust tier may require a compatible extension. |

## Task 1 — completed
- RED: `python3 -m pytest tests/unit/test_chat_policy.py -q` → collection failure (`ModuleNotFoundError: webapp.chat_policy`), as expected before implementation.
- GREEN: `python3 -m pytest tests/unit/test_chat_policy.py tests/unit/test_chat_models.py -q` → `34 passed in 0.13s`.
- Evidence: lexical, fail-closed `IntentDecision`; availability-filtered bounded `ToolPolicy`; model/AnswerRun-compatible JSON contracts.
- Commit: `8b2866d` (`feat: 为智能问答增加受控意图与工具策略`).

## Task 2 — completed
- RED: `python3 -m pytest tests/unit/test_chat_facts.py -q` → collection failure (`ModuleNotFoundError: webapp.chat_facts`), as expected before implementation.
- GREEN: `python3 -m pytest tests/unit/test_chat_facts.py tests/unit/test_chat_models.py -q` → `34 passed in 0.16s`.
- Evidence: accepted-unit normalization preserves original value/unit; incomplete/web facts fail closed; external structured values require provider/as_of; scoped conflicts are explicit.
- Commit: `0ba8f38` (`feat: 归一智能问答事实并识别口径冲突`).

## Task 3 — completed
- RED: `python3 -m pytest tests/unit/test_chat_verifier.py -q` → collection failure (`ModuleNotFoundError: webapp.chat_verifier`), as expected before implementation.
- GREEN: `python3 -m pytest tests/unit/test_chat_verifier.py tests/unit/test_chat_models.py -q` → `34 passed in 0.12s`.
- Evidence: numeric/unit matching, Scope/PDF URL checks, reference wording and conflict disclosure are deterministic and fail closed.
- Commit: `fcd04db` (`feat: 核验智能问答关键论断与证据覆盖`).

## Task 4 — completed
- RED: `python3 -m pytest tests/unit/test_rag_policy_m2.py -q` → `3 failed` (`answer_stream()` had no `tool_policy` argument).
- GREEN: `python3 -m pytest tests/unit/test_rag_policy_m2.py tests/unit/test_rag_qa.py -q` → `45 passed, 3 warnings in 1.24s`.
- Evidence: policies filter OpenAI definitions, disabled requests never execute, budget/round caps apply, JSON object results emit structured events, done payload preserves policy intent/timing.
- Commit: `542bedf` (`feat: 让问答工具调用服从意图与预算策略`).

## Task 5 — completed
- RED: server lifecycle test initially produced no M2 verifier event because the injected adapter did not accept the new policy argument; reproduced as an SSE error event and fixed only the M1 adapter compatibility seam.
- GREEN: `python3 -m pytest tests/unit/test_server_api.py -q -k 'chat_stream or policy or conflict'` → `19 passed, 121 deselected, 3 warnings in 1.42s`; `python3 -m pytest tests/unit/test_server_api.py -q` → `140 passed, 3 warnings in 8.25s`.
- Evidence: Scope is classified before execution; policy, facts/conflicts and verification are SSE/persisted; blocked numeric output is deterministically replaced with the required safe sentence.
- Commit: `be8c7c0` (`feat: 在问答运行中持久化事实冲突与核验结果`).

## Task 6 — completed with pre-existing CSS check concern
- RED: `python3 -m pytest tests/unit/test_chat_rendering_js.py -q` → `1 failed` because the three M2 renderer functions did not exist.
- GREEN: `python3 -m pytest tests/unit/test_chat_rendering_js.py -q` → `16 passed in 0.58s`.
- CSS: `python3 scripts/check_css.py` executed and failed on pre-existing duplicate selector `.history-view-pane` (the M2 selectors are not named in the diagnostic).
- Evidence: collapsed business-language policy, explicit PDF/reference facts and text-based conflict labels, role=status verifier feedback; streamed M2 events are consumed without exposing JSON/tool names.
- Commit: `0ab83ad` (`feat: 在智能问答标示策略、外部参考与事实冲突`).

## Task 7 — completed with process/CSS concerns
- Evaluation fixture: `python3 -m pytest tests/unit/test_chat_policy_eval_cases.py -q` → `1 passed in 0.11s`; coverage includes report fact, trend, local industry, realtime, event, Scope violation, conflict and tool failure.
- Browser GREEN: `python3 -m pytest tests/browser/test_chat_policy_flow.py -q` → `1 passed in 4.63s`; `python3 -m pytest tests/browser/test_chat_trust_flow.py -q` → `6 passed in 18.70s`. These run real FastAPI fixture app and agent-browser, including 1280x900, 768x1000 and 390x844 checks.
- Full verification: `python3 -m pytest -q` → `968 passed, 1 skipped, 3 warnings in 108.08s`; `git diff --check` → passed; `python3 scripts/check_css.py` → failed only on existing `.history-view-pane` duplicate selector.
- Manual agent-browser QA: actual fixture URL `http://127.0.0.1:58732/#/chat`; DOM showed `本次查证方式本地财报查证`, the safe numeric explanation, and `overflow: false`.
- Process note: Task 7 fixture schema did not retain an independent pre-fixture RED command; do not represent that as compliant TDD evidence.
- Commit: `1209913` (`test: 覆盖智能问答策略与事实核验闭环`).
