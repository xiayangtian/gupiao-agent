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

## M2 最终审查 Important 修复

最终审查的四项 Important finding 已在 `c4eaf72`（`fix: 加固可信问答 M2 状态契约`）修复，未实现 M3/M4、未推送或合并。

1. **Scope 工具参数绕过**：问题不在工具白名单，而在已批准工具的参数未绑定冻结 Scope。`RagQA` 现拒绝 `symbol/code/company_code/report_id/report_ids` 中显式出现的越界公司身份，拒绝事件不会调用 executor。RED `python3 -m pytest tests/unit/test_rag_policy_m2.py -q`：`1 failed, 3 passed`；GREEN：`4 passed, 3 warnings`。
2. **M1 宽松 Fact 绕过 M2 归一**：server 曾将任意 raw JSON tool-result 经 M1 宽松路径直接持久化。现在 raw 文本仅为 reference artifact；Fact 只接受 policy-gated `structured_tool_result`，并由 `FactNormalizer` 校验完整字段。执行 artifact 覆盖 payload 声称的 provider/as_of。RED `python3 -m pytest tests/unit/test_server_api.py -q -k raw_tool_json_does_not_bypass_fact_normalization`：`1 failed, 140 deselected`；GREEN：`1 passed, 140 deselected, 3 warnings`。
3. **ClaimVerifier 指标与冲突误判**：同单位同数值曾可跨指标匹配，英文 `revenue` 冲突也无法识别中文“营业收入”。现在以受控别名（营收、净利润、经营现金流、价格）绑定数值论断和冲突相关性。RED `python3 -m pytest tests/unit/test_chat_verifier.py -q`：`2 failed, 4 passed`；GREEN：`6 passed`。
4. **Task 7 验收覆盖不足**：原 fixture 只校验元数据，浏览器 fake 只有通用回答路径。现在真实 FastAPI SSE + agent-browser 覆盖行业本地样本、实时 reference/as_of、公告事件网页来源、冲突披露、工具失败 partial 以及越界事实拒绝；重开会话逐项断言文本和无横向溢出/console/page errors。RED `python3 -m pytest tests/browser/test_chat_policy_flow.py -q`：`1 failed, 1 passed`；GREEN：`2 passed`。

## 修复后验证与真实浏览器 QA

- 受影响单测：`python3 -m pytest tests/unit/test_chat_verifier.py tests/unit/test_chat_facts.py tests/unit/test_chat_evidence.py tests/unit/test_rag_policy_m2.py tests/unit/test_rag_qa.py tests/unit/test_server_api.py -q` → `208 passed, 3 warnings in 9.03s`。
- M2 SSE/真实浏览器：`python3 -m pytest tests/browser/test_chat_policy_flow.py tests/browser/test_chat_trust_flow.py -q` → `8 passed in 31.29s`。测试用 agent-browser 打开真实 fixture FastAPI URL，重开行业、实时、事件和冲突会话并验证无横向溢出、console errors 或 page errors。
- 手工真实浏览器 QA：实际启动 URL `http://127.0.0.1:58765/#/chat`，经 SSE 创建冲突 run 后以 agent-browser 重开。DOM 显示两条外部 reference、`存在口径/时间差异`、`⚠️ 部分完成`；测量 `overflow: false`。
- 全量：`python3 -m pytest -q` → `972 passed, 2 skipped, 3 warnings in 126.06s`。
- `git diff --check`、`git diff --cached --check` → passed；修复提交前没有 staged files。
- `python3 scripts/check_css.py` → failed only on既有 `.history-view-pane` duplicate selector；`git show HEAD:webapp/static/style.css | grep -n '\.history-view-pane' | wc -l` → `2`，本修复没有触及 CSS。

## 修复后自评

`DONE_WITH_CSS_BASELINE_CONCERN`：四项 Important finding 均有最小 RED/GREEN 回归和全量/浏览器验证。残留风险仅为无关既有 CSS checker 失败；仍需独立 reviewer 执行门禁，且本分支尚未合并 main。
