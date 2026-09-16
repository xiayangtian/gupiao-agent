# 智能问答可信 Agent M2 实施报告

## 阶段与进展

M2 Task 1–7 已在 `feat/trusted-chat-m2` 完成；未实现 M3 Planner、恢复步骤或 M4 工作台。

| Task | 状态 | 提交 |
| --- | --- | --- |
| 1 意图与工具策略 | 完成 | `8b2866d` |
| 2 事实归一与冲突 | 完成 | `0ba8f38` |
| 3 论断核验 | 完成 | `fcd04db` |
| 4 RAG 策略门控 | 完成 | `542bedf` |
| 5 Server 编排/持久化 | 完成 | `be8c7c0` |
| 6 前端来源边界展示 | 完成 | `0ab83ad` |
| 7 评测与浏览器闭环 | 完成 | `1209913` |

## TDD RED/GREEN 证据

- T1 RED：`python3 -m pytest tests/unit/test_chat_policy.py -q` → `ModuleNotFoundError: webapp.chat_policy`；GREEN：policy + chat model tests `34 passed`。
- T2 RED：`python3 -m pytest tests/unit/test_chat_facts.py -q` → `ModuleNotFoundError: webapp.chat_facts`；GREEN：fact + model tests `34 passed`。
- T3 RED：`python3 -m pytest tests/unit/test_chat_verifier.py -q` → `ModuleNotFoundError: webapp.chat_verifier`；GREEN：verifier + model tests `34 passed`。
- T4 RED：`python3 -m pytest tests/unit/test_rag_policy_m2.py -q` → 3 failed（`tool_policy` 参数缺失）；GREEN：policy/RAG tests `45 passed, 3 warnings`。
- T5 RED：M2 adapter compatibility first reproduced an SSE error event; GREEN：server targeted `19 passed`、full server API `140 passed`。
- T6 RED：rendering test 1 failed（M2 renderer functions absent）；GREEN：`16 passed`。
- T7：fixture schema test and real browser flow were added and passed; this task did not retain an independent pre-fixture RED command, which is a process-evidence gap.

## 验证

- `python3 -m pytest -q` → `968 passed, 1 skipped, 3 warnings in 108.08s`。
- `python3 -m pytest tests/browser/test_chat_policy_flow.py -q` → `1 passed in 4.63s`。
- `python3 -m pytest tests/browser/test_chat_trust_flow.py -q` → `6 passed in 18.70s`。
- `python3 -m pytest tests/unit/test_browser_test_safety.py tests/unit/test_chat_policy_eval_cases.py -q` → `9 passed in 0.58s`。
- `git diff --check` → passed.
- `python3 scripts/check_css.py` → failed only for existing `.history-view-pane` duplicate selector; no M2 selector was named.

## 真实浏览器 QA

通过 `agent-browser` 实际打开 fixture app `http://127.0.0.1:58732/#/chat`，重开真实 SSE 创建的会话。实际 DOM 结果：`本次查证方式本地财报查证`、`未找到可核验的披露，不能确认该数值。`、`overflow: false`。浏览器回归同时覆盖 1280x900、768x1000、390x844，且断言无 console/page errors。

## 偏离/障碍

- CSS checker 的 `.history-view-pane` 重复选择器是现有文件问题；未扩大 M2 范围修改它。
- Task 7 fixture schema 未保留独立 RED 证据，其他新增行为均有 RED/GREEN 记录。
- 需要独立 reviewer 完成计划要求的 P0/P1 代码评审门禁；本实施线程未自行宣布该门禁通过。

## 自评

功能契约、持久化、SSE、前端和浏览器闭环已实现且全量 pytest 通过。因 CSS 检查的既有失败、Task 7 RED 证据缺口以及待独立评审，本次自评为 `DONE_WITH_CONCERNS`。
