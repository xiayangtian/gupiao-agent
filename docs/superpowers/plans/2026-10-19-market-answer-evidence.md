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

- [ ] **Step 1: 写缓冲输出测试**：在 `tests/unit/test_rag_qa.py` 构造 fake AI client，输入分块 JSON delta + done，调用 `answer_from_context(..., market_analysis=True, market_intent="market_quote")`；断言事件不含 `delta`，且唯一 done answer 是 JSON 的 `answer` 文本、`market_claims` 保持结构化数组。
- [ ] **Step 2: 运行单测确认失败**：`pytest tests/unit/test_rag_qa.py -k market -q`；预期现有方法仍把 JSON delta 当正文转发。
- [ ] **Step 3: 实现行情专用生成分支**：在 `answer_from_context` 的证据构造中为行情来源增加 `call_id` 和有界 `payload`（不超过现有 `MAX_SOURCE_CHARS`/行数策略），并附服务端 request context；使用 AI client JSON object response format；累积行情模型 delta 到本地 buffer，不 yield 正文，解析后只把 answer/claims 放到 done。非法 JSON/shape 返回安全 `market_draft_error`，不转回普通自由文本或 continuation。
- [ ] **Step 4: 测试来源 prompt injection 和 malformed output**：原始行情字段值中包含“忽略此前指令”时，prompt 明确作为不可信数据；非 JSON、claims 非数组、答案超长、连续结束/截断、不支持 JSON mode 的 fake 响应均不产生 `delta`，不调用任何工具。
- [ ] **Step 5: 测试非行情兼容**：现有财报回答、web 来源回答、知识模式、续写及授权补充用例继续得到原有 delta/done 事件。若 fake client 测试不含 market flags，不改变结果。
- [ ] **Step 6: 运行聚焦测试**：`pytest tests/unit/test_rag_qa.py -q`；预期全部通过。
- [ ] **Step 7: 提交**：只 stage `qa.py` 与其单测；commit `feat: buffer structured market answer drafts`。

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

- [ ] **Step 1: 写端到端 SSE 防泄露测试**：用既有 chat API fake dependency 返回 `source_result` 和行情结构化 `done` 草稿；记录 SSE 全帧。对通过草稿，断言正文 `delta` 只在 `verification` 帧之后出现且其文本与最终 done.answer 一致；对伪造数字、错主体、非法 pointer、模型写“已核验”但没有 claim、未知字段，断言草稿数字从未出现在任何帧或持久化内容，最终 run.status 为 partial/failed 且核验错误码有限。
- [ ] **Step 2: 运行特定测试确认失败**：`pytest tests/unit/test_server_api.py -k market_answer_evidence -q`；预期当前 server 不接收 claims，或者行情正文 delta 在终态核验前已泄露。
- [ ] **Step 3: 接入结构化 claim 验证**：`_relay_rag_event` 保留 done payload 的 claims；在 server 的行情终态里调用 `parse_market_answer_draft` / `validate_market_answer_claims`，将已通过的 Facts 与现有来源 Facts 去重合并。无 draft 或任一 required claim 无法绑定时设置 blocked/partial，不用模型自述替代验证。
- [ ] **Step 4: 串接 ClaimVerifier 与 market coverage**：用 validated answer + validated Facts 调用既有 `ClaimVerifier`；复用上一实际交易日、时间窗、覆盖状态和来源冲突检查。失败答案使用已有行情安全降级；不允许 `_verified_market_fallback` 为 draft 中未核验的数字兜底。
- [ ] **Step 5: 延迟行情 SSE**：行情模式不从 `_relay_rag_event` 输出模型正文；在 verification/coverage 最终可接受后，仅发一次经审查的 answer delta，再发 done。失败、停止、超时及来源预算耗尽都不得将 buffer 写入最终 answer；保留已完成的进度/source/artifact 帧。
- [ ] **Step 6: 保持通用 verifier 退化可解释**：添加有限 issue code（例如 `invalid_market_draft`、`unresolved_source_reference`、`source_value_mismatch`、`market_identity_mismatch`、`market_time_mismatch`、`market_unit_mismatch`、`ambiguous_metric_semantics`）；错误对象不保存 prompt、完整原始 payload 或凭证。
- [ ] **Step 7: 测试财报/网页和旧 run**：沿用现有 server API 单测证明非行情 delta 顺序、旧 JSON 历史 run 解码、财报/网页 verification 未变化；行情图表仍只消费现有结构化 Facts，不消费未核验 claims。
- [ ] **Step 8: 运行聚焦测试**：`pytest tests/unit/test_server_api.py tests/unit/test_chat_verifier.py tests/unit/test_market_answer_evidence.py -q`；预期全部通过。
- [ ] **Step 9: 提交**：只 stage server/verifier/models 和相关测试；commit `feat: gate market answer stream on evidence checks`。

### Task 4: 做直接影响面回归和用户文档更新

**Files:**
- Modify: `tests/browser/test_chat_trust_flow.py`（若现有 fixture 可复用）
- Modify: `docs/FEATURE-CATALOG.md`
- Modify: `README.md`（仅当用户可见的行情回答/来源说明改变）

- [ ] **Step 1: 运行相关单测回归**：`pytest tests/unit/test_chat_execution.py tests/unit/test_chat_facts.py tests/unit/test_chat_verifier.py tests/unit/test_rag_qa.py tests/unit/test_source_adapters.py tests/unit/test_source_runtime.py tests/unit/test_server_api.py -q`；逐项记录通过/失败，不将不相关 baseline failure 归因给本变更。
- [ ] **Step 2: 运行浏览器可信流回归**：`pytest tests/browser/test_chat_trust_flow.py -q`；如 fixture 需额外 server，使用测试 fixture 的本地 URL，不用生产账号或真实用户会话。
- [ ] **Step 3: 真实浏览器聚焦 QA**：启动本地测试 fixture，运行 `browser_qa` 对聊天页桌面、平板、移动端验证布局；用 snapshot 检查完成/核验失败消息；查看 console、uncaught errors、失败 network 请求。报告 URL、实际视口、错误详情。截图只留在 browser-goblin 产物目录，不提交仓库。
- [ ] **Step 4: 补回归边界**：确认多来源不同 JSON 形状均只因 model-selected pointers 可读取而无需新 provider-specific Fact mapper；字段歧义、缺时点、身份冲突仍降级；取消后无下游调用；财报/网页 SSE 不延迟/不回归。
- [ ] **Step 5: 更新功能目录**：只在实现最终验证且合入本地 `main` 后，将实际合并短 SHA、用户能力变化及验证入口加入对应功能条目；README 仅在使用方式或用户可见行为变化时更新。
- [ ] **Step 6: 合并门槛**：先完成全部适用测试，再按用户授权本地合入 `main`；不推送远端。合并后检查 `git status`、合并 SHA，按项目规则重启配置好的本地服务并检查 `/api/health`；失败需报告，不声称已生效。

## Plan Self-Review

- 设计背景、范围、授权预算/取消、快照边界、模型解释、通用来源校验、公式、SSE 门禁、失败与旧数据兼容、验收和文档更新均对应 Task 1–4。
- 两个重要残余风险保持显式：原始数字精确匹配不能单独证明指标语义；没有可信字段语义锚点必须降级。实现不得把模型 claim 当作来源事实。
- 先单测安全 resolver 和绑定，再生成层，再 server 终态/SSE，最后集成及浏览器；不并行改动同一工作区。
- 计划待审阅；执行前应创建独立实施分支，并登记该功能的实现任务。完成验证并合入 `main` 后才更新设计台账为 `已实现`。
