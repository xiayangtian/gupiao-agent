# P4 工作包一：运行级离线评测基线（脱敏汇总）

本文件是 P4 实施计划工作包一的**真实运行产物摘要**，不是设计替代品。原始 JSON 报告为同目录 `chat-p4-baseline.json`（白名单聚合，不含 prompt、答案正文、来源片段或工具参数）。

## 运行身份

- 场景集：`chat-runtime-offline-v1`（`tests/fixtures/chat_runtime_eval_cases.json`）
- 语料版本：`chat-eval-fixture-v2`（`tests/fixtures/chat_runtime_eval_cases.json` 同源脱敏合成语料，非真实披露）
- 冻结时钟：`2026-10-08T15:00:00+08:00`
- 替身版本：`chat-runtime-offline-v1`（固定模型/检索/行情/MCP 替身，默认拒绝出站 TCP/HTTP）
- 代码版本：见 `chat-p4-baseline.json` 的 `code_revision`
- 运行命令：`python3 scripts/run_chat_runtime_evaluation.py --cases tests/fixtures/chat_runtime_eval_cases.json --output docs/superpowers/plans/chat-p4-baseline.json`

## 结果（分子/分母）

| 场景 | 类别 | 终态 | 与预期一致 | 阻断码 | 引用支持 | 物理调用 |
| --- | --- | --- | --- | --- | --- | --- |
| report-number-after-300 | report | completed | 是 | 无 | 1/1 | model 2, retrieval 1 |
| market-kline | market | completed | 是 | 无 | 1/1 | model 2, market 1 |
| market-recap-window | recap | completed | 是 | 无 | 0/0（无金标准，记 N/A） | model 2, mcp 3 |
| general-knowledge | knowledge | completed | 是 | 无 | 0/0（N/A） | model 1 |
| explicit-research | research | completed | 是 | 无 | 0/0（N/A） | model 2, retrieval 1 |
| followup-scope | followup | completed | 是 | 无 | 0/0（N/A） | model 4, retrieval 2 |
| source-failure | failure | partial | 是 | 无 | 0/0（N/A） | model 2, retrieval 1 |
| research-recovery | recovery | completed | 是 | 无 | 0/0（N/A） | model 2, retrieval 1 |

- 汇总：8 用例，8 个终态符合预期，0 个安全阻断，0 个 harness 错误；4 个用例含人工复核项（禁止性断言）。
- 延迟：客户端整体耗时合计约 0.06 秒。**未报告首帧/首内容时延**：Starlette `TestClient` 会缓冲 `StreamingResponse`，运行级时延在此实现下不可得，故记为不可用而非伪造观测值。
- token/费用：全部记为 `unknown`（固定替身不报告真实用量），未以字符数换算成本。

## 安全与真实性边界

- 出站 TCP/HTTP 在整轮评测中默认被拒绝；未注入的来源会直接失败，不回退真实 provider。
- Scope 越界、未授权来源调用、无证据数值、过度声称的终态都会产生阻断码；本基线为 0。
- 语料为合成内容，仅含既有评测 fixture 的报告身份；页面/证据定位为评测标注，不代表真实 PDF 页码语义。
- 真实的"证据是否支持结论"仍以 Python 断言（主体/期间/单位/数值/证据 ID 一致）判定；`forbidden_claims` 的自然语言表述保留为人工复核项，不自动通过。

## 未测得/未执行

- 未运行真实模型、真实行情、真实 MCP、真实网页或生产账号。
- 未测首帧/首内容时延（原因见上）；未测得可信 token 用量与费用。
- 工作包二的检索候选比较、工作包三的上下文预算对照尚未执行。
- P3 并发容量饱和与研究停止/恢复的浏览器端到端检查仍待工作包一 Task 4 补充。
