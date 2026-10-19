# 行情回答证据核验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让行情模型能理解多种受控来源格式，同时由服务端核对行情数字引用、对象、时点及可复算关系，且正文数字在核验完成前不经 SSE 暴露。

**Architecture:** `SourceResult` 保留当前 run 的受控行情快照及服务端请求绑定；模型为行情答案返回结构化草稿和数字主张，主张可引用既有标准化 `Fact`，或引用原始来源快照的 JSON Pointer。服务端将通过通用路径、来源、身份、时间、数值、单位和公式检查的来源引用转换为 `Fact`，继续复用现有 `ClaimVerifier` 与 `VerificationReport`；行情草稿缓冲到终态核验完成才发布。财报、网页路径和现有行情图表事实提取保持兼容。

**Tech Stack:** Python 3.11+, dataclasses / JSON, FastAPI SSE, pytest；不新增依赖、不调用新数据源。

**Spec:** `docs/superpowers/specs/2026-10-19-market-answer-evidence-design.md`

## Global Constraints

- 只调整智能问答 `market_quote`、`market_trend`、`market_recap` 的行情数字回答；财报、网页、研究路径的核验合同不变。
- 所有来源仍须经 `SourceRuntime` 与服务端冻结的来源策略；模型不能选择 provider、URL、工具参数或触发新的调用。
- 不突破现有预算、取消、时限、交易日、覆盖率、复权口径、全市场样本及上游授权约束；Sina 保持禁用。
- 模型的结构化主张不是核验结果；来源/路径/数值/主体/时点/单位/公式检查及现有 ClaimVerifier 均通过后，才能发送行情正文数字。
- JSON Pointer 只解析本轮有界来源快照；禁止执行模型生成的路径代码、公式代码或来源数据指令；不持久化原始行情快照或思考过程。
- 已有图表和旧 AnswerRun 继续使用现有 `Fact` 合同；新旧运行兼容，不重验或重写历史运行。
- 使用 TDD；每个任务先运行对应失败用例，再实现并验证。不得顺手清理或覆盖工作区中既有未提交文件。

---

## 文件结构与责任

- Create `webapp/market_answer_evidence.py`：行情草稿的严格解析、来源引用解析、通用证据/单位/主体/时点/公式验证，输出通过核验的 `Fact` 和有限错误码。
- Modify `webapp/source_adapters.py`：把由服务端创建的最小请求身份绑定附加到 `SourceResult`；沿用既有行数与内容上限，不扩大 provider 调用。
- Modify `webapp/source_runtime.py`：为 `SourceResult` 添加默认空的、不可由模型写入的 `request_context` 字段并复制/冻结；限制为行情调用的安全 allowlist（证券代码、日期窗口、operation），其他类别不保留敏感参数。
- Modify `webapp/chat_models.py`：只在既有 Fact 合同不能表达证据引用时增加可选 provenance 字段；保持旧 JSON 可反序列化。
- Modify `financial_report_fetcher/rag/qa.py`：仅 `market_analysis=True` 走 JSON 草稿/缓冲输出路径；普通财报、网页、知识问答继续原有文本流。
- Modify `webapp/chat_verifier.py`：仅扩展通用行情指标别名/单位规则和相关错误类别，保留既有 Fact 验证行为。
- Modify `webapp/server.py`：冻结行情意图/窗口传给 RagQA；接收草稿中的 claims，在现有终态核验前解析成 Fact；核验前不转发草稿；只在来源、覆盖与 ClaimVerifier 检查后发出行情正文。
- Create `tests/unit/test_market_answer_evidence.py`：通用 claim schema、指针解析、身份/时间/单位/数值/公式边界。
- Modify `tests/unit/test_source_adapters.py`、`tests/unit/test_rag_qa.py`、`tests/unit/test_chat_verifier.py`、`tests/unit/test_server_api.py`：来源快照绑定、JSON 模型输出、行情 SSE 门禁及非行情不回归。
- Browser 测试使用 `tests/browser/source_runtime_app.py` 或现有 chat fixture；只在服务器集成通过后按项目要求对 fixture URL 做 browser QA。

## Claim Contract

模型行情草稿是严格 JSON 对象：

```json
{
  "answer": "工商银行现价 8.59 元/股，外部参考。",
  "claims": [
    {
      "metric": "price",
      "value": 8.59,
      "unit": "元/股",
      "period": "2026-10-19",
      "period_kind": "point_in_time",
      "entity_scope": "company",
      "company_code": "601398",
      "source_id": "<本次 SourceResult.call_id>",
      "value_pointer": "/0/price",
      "identity_pointer": "/0/symbol",
      "time_pointer": "/0/time"
    }
  ]
}
```

- 支持两种证据锚点：已存在并由服务端生成的 `fact_id`，或当前 run 的 `source_id` 加 `value_pointer`。二者只能选一；原始来源引用可带身份/时间指针，若来源响应不含这些字段则用服务端绑定的请求主体和来源 `as_of`，二者均不能确认时拒绝该 claim。
- schema 校验禁止未知顶层/claim 字段、非有限数值、布尔数值、空 ID、非 ISO 日期、未知 `period_kind`/`entity_scope`、过长字符串/数组、缺少单位或来源锚点。claim 的可选公式只能是服务端白名单标识；只有 `fact_id` 锚点可携带公式，原始来源 pointer 不能声称已计算派生指标。
- JSON Pointer 支持 RFC 6901 `~0` / `~1` 解码，仅遍历 dict/list；拒绝空路径、越界、标量中间节点、`__proto__`/`prototype`/`constructor`、超深路径及指向字符串/缺失值。数值节点必须是有限 `int`/`float` 且非 `bool`。原始来源路径只在模型可见的有界 `SourceResult.content` 完整 JSON 解码后与 `payload` 完全相同时可引用；内容截断/摘要化时必须使用模型可见的服务端 Fact ID，否则拒绝引用。
- 通用检验只比较原始数值与 claim 值（允许单位固定换算和有限舍入容差）；检查请求绑定/返回身份与冻结 Scope、来源 `as_of`/数据日期与请求窗口、market intent 的通用 metric/unit 组合。字段含义本身若没有来源元数据或稳定语义锚点，不能因为数字相同就推断语义正确，须返回 partial/unsupported。
- 原始来源引用仅可生成非派生数值 Fact。涨跌额、涨跌幅等派生指标只能引用同一 run 已由服务端公式计算、且输入 Fact/公式 lineage 完整的 Fact；不得把供应商直接返回的 `change`/`change_pct` 字段当作已由本程序复算。已存在的派生 Fact 引用需确认其所有 `derived_from_ids` 均在当前 Fact 集内。最终答案仍必须通过 `ClaimVerifier` 对正文数字、范围及市场意图的检查。

---

### Task 1: 建立行情证据引用与通用核验器

**Files:**
- Create: `webapp/market_answer_evidence.py`
- Modify: `webapp/source_runtime.py`
- Modify: `webapp/source_adapters.py`
- Test: `tests/unit/test_market_answer_evidence.py`
- Test: `tests/unit/test_source_adapters.py`

**Interfaces:**
- `SourceResult.request_context: Mapping[str, str] = {}`：只由 `normalize_source(SourceCall, ...)` 从固定行情调用参数的 allowlist 生成；现有构造方式无需修改。
- `parse_market_answer_draft(raw: str) -> MarketAnswerDraft`：严格解析 JSON schema；输入不是合法对象则返回带 `invalid_market_draft` 的失败结果，不抛给 SSE handler。
- `validate_market_answer_claims(draft, *, sources, existing_facts, scope, window, intent) -> MarketClaimValidation`：仅返回通过核对的 `Fact`、被拒条目错误码和 `passed|partial|blocked`；不会调用来源。
- `MarketAnswerDraft(answer: str, claims: tuple[MarketClaim, ...])`；`MarketClaimValidation(facts: tuple[Fact, ...], rejected: tuple[str, ...], status: str)` 是新模块内部冻结 dataclass。

- [x] **Step 1: 写拒绝无效证据引用的失败测试**：新建 `tests/unit/test_market_answer_evidence.py`，使用现有 `SourceResult`/`SourceCoverage` fixture 建立最小腾讯报价和错误响应。覆盖非法 JSON、未知字段、claim 缺字段、`NaN`、bool value、坏 JSON Pointer、超界数组、危险属性名、未知 source ID、失败来源、来源外路径。
- [x] **Step 2: 运行单测确认失败**：`pytest tests/unit/test_market_answer_evidence.py -q`；预期因模块/接口不存在失败。
- [x] **Step 3: 先测试来源请求绑定**：在 `tests/unit/test_source_adapters.py` 增加一条固定 `SourceCall(provider="tencent", operation="quote", arguments={"symbol": "sh601398"})` 通过 `normalize_source` 后携带 `request_context["symbol"] == "sh601398"` 的测试；测试 web/search 来源不保存 query 字符串。
- [x] **Step 4: 最小实现来源绑定及 JSON Pointer 解析**：增加 `SourceResult.request_context` 默认空映射，在创建来源结果时仅复制行情 allowlist（`symbol`/`symbols`/`start_date`/`end_date`/`period`）；调用结果 payload 行数与字符限制保持原值；新模块实现受限路径解析和严格 schema。
- [x] **Step 5: 写通过/拒绝 claim 核验测试**：给定 source `call_id="call-1"`, `as_of="2026-10-19 14:55:00"`, 请求 symbol `sh601398`, payload `({"symbol":"sh601398","price":8.59,"time":"2026-10-19 14:55:00"},)`，验证 `price=8.59` 和 code/date 一致可构造 Fact；再逐一替换成 `600519`、`2026-10-16`、`8.95`、`元/手`、无身份字段且无 request binding，均拒绝。覆盖来源格式变更为 `{"ticker":"601398","last":8.59,"quote_time":"2026-10-19 14:55:00"}` 时模型可用不同 pointers 指向同值；但字段语义没有可靠锚点时按设计降级，而不是以相同数值放行。
- [x] **Step 6: 实现单位、主体、时点和派生 lineage 白名单**：只接受 metric-specific 允许单位和有限容差；原始来源 pointer 不生成涨跌额/涨跌幅等派生指标 Fact，也不接受供应商直接返回的派生字段作为复算结果；这些指标只允许引用当前 run 已生成、使用实际前交易日同主体同口径输入、且所有 `derived_from_ids` 均可解析的服务端 Fact；模型传入的公式字符串不执行。
- [x] **Step 7: 运行两个聚焦文件**：`pytest tests/unit/test_market_answer_evidence.py tests/unit/test_source_adapters.py -q`；预期全部通过。
- [x] **Step 8: 提交该任务**：Task 1 已提交 `e61f723`，并追加来源快照可见性回归 `fix: require visible market source snapshot for claims`。

### Task 2: 为行情回答增加结构化草稿，并保持非行情流原样

**Files:**
- Modify: `financial_report_fetcher/rag/qa.py`
- Modify: `tests/unit/test_rag_qa.py`
- Test: `tests/unit/test_market_answer_evidence.py`

**Interfaces:**
- 扩展 `RagQA.answer_from_context(..., market_analysis=False, market_intent="", market_window=None)`；market intent 只能是 `market_quote|market_trend|market_recap`。非行情调用继续原路径。
- 行情模式输出单一 `done` payload：`{"answer": <已解析文本>, "market_claims": <严格校验前 claims>, "market_draft_error": ""}`；它不得输出任何行情正文 `delta`。结构化 claim 仍是未信任模型输入，待 Task 3 服务端解析/校验。

- [x] **Step 1: 写缓冲输出测试**：fake AI client 输入分块 JSON delta + done；断言事件不含 `delta`，唯一 done 含解析出的 answer 与结构化 claims，并传递 server intent/window。
- [x] **Step 2: 运行单测确认失败**：新增缓冲与 malformed 测试先因正文 delta 外泄而失败。
- [x] **Step 3: 实现行情专用生成分支**：来源证据携带稳定 `call_id`、服务端请求绑定、模型可见来源内容及最多 40 条结构化 Fact（含派生 lineage）；使用 JSON object response format，buffer 所有正文 delta，只有解析成功后将 answer/claims 放入 done。失败不回落普通文本/continuation。
- [x] **Step 4: 测试来源 prompt injection 和 malformed output**：覆盖不可信行情字段、非 JSON、claims 非数组、答案超长、截断 JSON 和不支持 JSON mode；均不产生行情 delta、不调用工具。
- [x] **Step 5: 测试非行情兼容**：完整 `test_rag_qa.py` 回归通过，覆盖财报/web、知识模式、续写和授权补充原行为。
- [x] **Step 6: 运行聚焦测试**：`python3 -m pytest tests/unit/test_rag_qa.py -q`：64 passed，3 dependency deprecation warnings；`compileall`、`git diff --check` 通过。
- [x] **Step 7: 提交**：已提交 `financial_report_fetcher/rag/qa.py`、其单测与本计划进度；commit `7259052 feat: buffer structured market answer drafts`。

### Task 3: 将验证结果接入既有问答终态与 SSE 门禁

**Files:**
- Modify: `webapp/server.py`
- Modify: `webapp/chat_verifier.py`
- Modify: `webapp/chat_models.py`（仅需要兼容性 provenance 字段时）
- Modify: `tests/unit/test_chat_verifier.py`
- Modify: `tests/unit/test_server_api.py`

**Interfaces:**
- server 调用 RagQA 时，将服务端冻结的 `route.intent` 与 `market_window` 传入；不得从模型 draft 中取 intent/window。
- `_RagRunState` 增加仅本次运行内使用的 `market_claims`/`market_draft_error`；从 `done_payload` 收取，不生成 SSE 正文。
- 行情终态处理次序：来源结果累积 → claim schema/source validation → 合格 Facts 写入 run state → `ClaimVerifier.verify` → coverage/market status → 发布核验后答案 → `_persist`/最终 done。

- [x] **Step 1: 写端到端 SSE 防泄露测试**：新增通过草稿的延迟 delta 测试，以及伪造数字、错主体、非法 pointer、未知字段和缺失 claim 不泄露到 SSE/存储的测试。
- [x] **Step 2: 运行特定测试**：实现前的红测输出未单独保留；实现后防泄露聚焦回归通过。
- [x] **Step 3: 接入结构化 claim 验证**：`_relay_rag_event` 缓存 claims；行情终态调用 `parse_market_answer_draft` / `validate_market_answer_claims`，合格 Facts 与现有 Facts 去重合并；来源 Facts 按趋势均匀采样/单日复盘端点与 lineage 有界投影。
- [x] **Step 4: 串接 ClaimVerifier 与 market coverage**：validated answer + Facts 接入既有 `ClaimVerifier`；无效草稿不回退到草稿数字，安全 fallback 仅用服务端 Facts 并重新核验。
- [x] **Step 5: 延迟行情 SSE**：行情模式屏蔽模型 delta；仅终态核验和覆盖状态完成后发一次审核答案，再发 done；进度/source/artifact 帧保留。
- [x] **Step 6: 保持通用 verifier 退化可解释**：使用有限 issue code；错误内容不保存完整 payload 或凭证。
- [x] **Step 7: 测试财报/网页和旧 run**：非行情 RagQA/server API 回归、旧 run 序列化兼容与 Fact 事件测试均在聚焦测试集通过。
- [x] **Step 8: 运行聚焦测试**：`python3 -m pytest tests/unit/test_chat_execution.py tests/unit/test_chat_facts.py tests/unit/test_chat_verifier.py tests/unit/test_rag_qa.py tests/unit/test_source_adapters.py tests/unit/test_source_runtime.py tests/unit/test_server_api.py -q`：383 passed，3 dependency deprecation warnings；compileall 与 `git diff --check` 通过。
- [x] **Step 9: 提交**：提交 `feat: gate market answer stream on evidence checks`，包括 server/RagQA 集成、相关单测和计划进度。

### Task 4: 做直接影响面回归和用户文档更新

**Files:**
- Modify: `tests/browser/test_chat_trust_flow.py`（若现有 fixture 可复用）
- Modify: `docs/FEATURE-CATALOG.md`
- Modify: `README.md`（仅当用户可见的行情回答/来源说明改变）

- [x] **Step 1: 运行相关单测回归**：`python3 -m pytest tests/unit/test_chat_execution.py tests/unit/test_chat_facts.py tests/unit/test_chat_verifier.py tests/unit/test_rag_qa.py tests/unit/test_source_adapters.py tests/unit/test_source_runtime.py tests/unit/test_server_api.py -q`：383 passed，3 dependency deprecation warnings。
- [x] **Step 2: 运行浏览器可信流回归**：`python3 -m pytest tests/browser/test_chat_trust_flow.py -q`：9 passed、3 failed。失败根因复现为 fixture 将占位 key 用于 DeepSeek 意图分类，收到 401 并返回 clarification/run=null；本次未修改无关测试基础设施。
- [x] **Step 3: 真实浏览器聚焦 QA**：`http://127.0.0.1:8799/#/chat`，桌面/平板/移动；无 console/page errors，browser_qa 汇总 1 个 4xx 但没有捕获对应请求；手动 network 查询也无失败请求明细。snapshot 确认聊天页范围控件与历史消息结构。临时 fixture 服务已停止；截图仅留在 browser-goblin 产物目录。
- [x] **Step 4: 补回归边界**：行情 JSON pointer/Facts 投影、错主体/错值/缺 claim 门禁、长趋势采样、日复盘派生 lineage、coverage fallback 和非行情 SSE/Fact 兼容由聚焦单测与 horizon fixture matrix 覆盖；未新增 provider 或 source call。
- [ ] **Step 5: 更新功能目录**：只在实现最终验证且合入本地 `main` 后，将实际合并短 SHA、用户能力变化及验证入口加入对应功能条目；README 仅在使用方式或用户可见行为变化时更新。
- [ ] **Step 6: 合并门槛**：先完成全部适用测试，再按用户授权本地合入 `main`；不推送远端。合并后检查 `git status`、合并 SHA，按项目规则重启配置好的本地服务并检查 `/api/health`；失败需报告，不声称已生效。

## Plan Self-Review

- 设计背景、范围、授权预算/取消、快照边界、模型解释、通用来源校验、公式、SSE 门禁、失败与旧数据兼容、验收和文档更新均对应 Task 1–4。
- 两个重要残余风险保持显式：原始数字精确匹配不能单独证明指标语义；没有可信字段语义锚点必须降级。实现不得把模型 claim 当作来源事实。
- 先单测安全 resolver 和绑定，再生成层，再 server 终态/SSE，最后集成及浏览器；不并行改动同一工作区。
- 计划待审阅；执行前应创建独立实施分支，并登记该功能的实现任务。完成验证并合入 `main` 后才更新设计台账为 `已实现`。
