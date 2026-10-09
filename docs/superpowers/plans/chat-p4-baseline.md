# P4 离线基线与候选对照（脱敏汇总）

该记录包含运行级基线、检索窗口对照及上下文预算对照。机器报告仅保留安全汇总，不含 prompt、答案正文、来源片段或工具参数：

- `chat-p4-baseline.json`：legacy 前缀窗口 + 有界历史预算基线。
- `chat-p4-retrieval-candidate.json`：查询相关证据窗口候选。
- `chat-p4-context-unbounded.json` / `chat-p4-context-budgeted.json`：同一相关窗口下关闭/启用消息预算的对照。

## 运行身份与边界

- 场景集：`chat-runtime-offline-v1`，9 个合成离线场景；语料版本 `chat-eval-fixture-v3`。金标准事实被刻意放在前 300 字之后。
- 时钟：固定 `2026-10-08T15:00:00+08:00`；替身使用固定模型/检索/行情/MCP 数据。
- 命令：`python3 scripts/run_chat_runtime_evaluation.py --cases tests/fixtures/chat_runtime_eval_cases.json --output <isolated-json> --context-window-strategy <legacy|relevance> --context-budget <enabled|disabled>`。
- 代码身份：报告记录 `HEAD+dirty:<内容哈希>`，避免把未提交工作区误写成纯 HEAD。
- 未调用真实模型、外网、MCP、行情或生产账号；实际 token 与费用仍为 `unknown`。报告中的 prompt 字符数仅为比较指标，不等同 tokenizer token 或成本。

## 基线结果（legacy + budget enabled）

- 9 个场景中 6 个终态符合预期；2 个金标准场景触发 `missing_claim`：`report-number-after-300` 与 `context-budget-long-history`。
- 引用支持共 1/3：市场 K 线 1/1；两个长片段金标准均未支持。
- 对照诊断把上述两例标为 `window_clipped`；其证据文档已进入检索结果，但默认前缀窗口未含目标数字。
- 其余无金标准场景为 N/A，不把 0/0 当作 100% 支持率。5 项自然语言禁止性断言保留人工复核。

## 工作包二：检索对照（relevance window）

- 可比性：通过；仅改变 `context_window_strategy`，语料、冻结时钟、替身和调用上限相同。
- 对目标用例 `report-number-after-300`：终态 `partial → completed`，引用支持 `0/1 → 1/1`，诊断 `window_clipped → supported`。
- 第二个长历史对照用例也由 `partial → completed`、支持 `0/1 → 1/1`。
- 9 个场景物理来源调用数均无增加；比较器未发现阻断/支持度/调用回归，终态符合预期由 6/9 提升至 9/9。
- **采纳**相关窗口作为默认策略；仍保留 `legacy` 显式回退项。该结论只覆盖这组脱敏合成语料，不证明真实 PDF 的跨页定位质量。

## 工作包三：上下文预算与压缩

- 可比性：通过；同一 relevance 策略、同一语料和替身，只比较预算关闭/启用。
- 长历史两轮场景 prompt 字符估计由 24,899 降至 15,877（约 -36.2%）；终态均符合预期，引用支持均为 1/1，物理来源调用数一致。
- 全套 9 场景无阻断或来源调用回归；预算边界保留当前问题、Scope 与必需证据，超限时 fail-closed，并只保留 user/assistant 历史。
- **采纳**上下文预算策略为默认；旧历史截断从服务入口移除，统一由预算投影执行。估算按字符长度，不是模型 tokenizer 的硬上限证明；输出保留量/安全余量仍依赖模型配置。

## 运行观测与限制

- `TestClient` 缓冲 SSE，首帧/首内容时延记不可用；本次不伪造这两项。CLI 的总耗时是本机离线 harness 执行时间，不可外推线上延迟。
- 来源调用数来自固定替身物理入口计数；未知 token/费用不写成零。
- 真实提供方 CLI 只做 dry-run 配置校验，即使给出 `--allow-live` 也明确 `executed=false`；CI、生产账号、非白名单 provider 均拒绝。
- 合入本地 `main` 尚未执行，也未推送远端；交付分支仍需独立合入授权。
