# 个股与大盘通用期限行情分析实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持个股和大盘对任意受控自然/交易期限及模糊短中长期的行情分析，以日/周/月 K 线满足提供方上限，并用固定目标 query 矩阵和全量测试验收。

**Architecture:** `chat_time` 统一将用户期限解析为冻结的上海时区窗口；路由根据对象与时间窗确定 market_trend/market_recap，执行计划只使用可用且时间兼容的来源；执行器按跨度选择 day/week/month K 线，归一层保留粒度/覆盖区间，事实和 verifier 使用同一窗口。模糊期限策略集中管理，来源边界通过 AnswerRun、SSE 与安全错误码披露。

**Tech Stack:** Python 3、FastAPI/SSE、pytest；无新依赖；Tencent K 线 day/week/month，最多 800 bars/调用。

**Spec:** `docs/superpowers/specs/2026-10-09-individual-and-market-horizon-analysis-design.md`

## Global Constraints

- 所有市场窗口使用 `Asia/Shanghai`，起止日期均按 inclusive 处理。
- 模糊默认：短期 5 个实际交易日、中期 6 个自然月、长期 3 个自然年；显式期限优先。
- Tencent 单次 K 线最多 800 根，超安全范围拒绝/澄清；实际覆盖不足必须返回 partial，不静默缩短窗口。
- 个股行情只绑定冻结的公司代码；大盘只描述实际返回的固定四大指数。
- 多月/多年大盘不得调用今日/近5日 MCP 广度并冒称其匹配；明确披露不含长周期全市场广度。
- 未核验的模型数字不得输出；确定性 fallback 只能基于本轮来源事实，并再次经过同一 ClaimVerifier。
- 不新增依赖；测试替身不得出站访问行情、MCP、网页或生产账号。
- 全部 pytest 是验收门槛；单独分类记录与本变更无关的既有失败，不能隐藏直接回归。

---

## Repository Map

| File | Responsibility / planned changes |
| --- | --- |
| `tests/fixtures/market_horizon_queries.json` | 固定 Q1–Q16 query、固定时钟、预期对象/route/window/grain/source/coverage/status。 |
| `webapp/chat_time.py` | 通用整数/中文数字与日、交易日、周、月、年期限语法；日历安全回退；模糊默认；窗口跨度边界。 |
| `webapp/chat_policy.py` | 依据 market 对象及通用期限标记为趋势；财务与行情边界保持优先级。 |
| `webapp/chat_request_resolution.py` | 将对象、期限和 fuzzy_default/explicit 来源统一冻结，传递到 planner。 |
| `webapp/execution_executor.py` | 根据跨度在 day/week/month 选择粒度、计算不超过 800 的 count，所有走势使用同一选择器。 |
| `webapp/server.py` | 在计划前排除时间不匹配的 MCP 广度；执行并保存选用 grain/window；市场答案失败时按事实安全回退。 |
| `webapp/chat_facts.py` | 从 day/week/month K 线提取同主体价格/指数事实，保存实际 period_kind 和 source identity。 |
| `webapp/chat_execution.py` | 验证实际日期覆盖、grain、主体；中长期 market_recap 可用指数回答但保持明确的 partial 边界。 |
| `webapp/chat_verifier.py` | 对周/月期间事实按正确期间和范围核验，不将其视为日线。 |
| `financial_report_fetcher/rag/qa.py` | market fallback 使用通用粒度/标签，仍只引用已归一事实。 |
| `tests/unit/test_chat_time.py`, `test_chat_policy.py`, `test_chat_request_resolution.py` | 解析与对象/意图矩阵。 |
| `tests/unit/test_execution_executor.py`, `test_execution_plan_recap.py` | grain 选择、800 根边界、预算/广度可用性。 |
| `tests/unit/test_chat_facts.py`, `test_chat_verifier.py`, `test_chat_execution.py` | 多粒度证据、范围核验、覆盖状态。 |
| `tests/unit/test_server_api.py`, new `tests/unit/test_chat_horizon_matrix.py` | 确定性替身驱动的真实 SSE 目标矩阵。 |
| `docs/FEATURE-CATALOG.md`, `docs/superpowers/DESIGN-REGISTRY.md` | 经全量验证和合并后记录准确 SHA 与测试证据。 |

---

### Task 1: 固化 query matrix 并通用解析时间窗/路由

**Files:**
- Create: `tests/fixtures/market_horizon_queries.json`
- Create: `tests/unit/test_chat_horizon_matrix.py`
- Modify: `webapp/chat_time.py`
- Modify: `webapp/chat_policy.py`
- Modify: `webapp/chat_request_resolution.py`
- Test: `tests/unit/test_chat_time.py`, `tests/unit/test_chat_policy.py`, `tests/unit/test_chat_request_resolution.py`

**Interfaces:**
- `resolve_market_window(question: str, now: datetime | None = None) -> MarketWindow | None` 返回 immutable 起止日期/rolling trading-day 窗口；独立的 `has_market_period_expression(question: str) -> bool` 识别超限期限，避免超限 query 被路由为 quote。
- `ResolvedRequest.window_origin` 使用 `explicit|fuzzy_default|clarification|none`，只由服务端解析输入决定，不取模型输出。
- `ResolvedRequest` 冻结 route、window、window_origin；`market_trend` 用 `market_kline`，`market_recap` 用 `market_indices`。

- [ ] **Step 1: 写失败 query contract 测试**：先创建 fixture，包含设计文档 Q1–Q16；测试读取 fixture 后分别断言每个 query 的 `QuestionRouter` 意图、company requirement、`resolve_market_window()` 的精确边界/kind/trading_days 及来源类型。另覆盖“近一年营收趋势”必须 financial_trend、“半年内行情”不得 market_quote、孤立“中期/长期”先澄清、自然日和交易日区分、闰日/月末、上限拒绝。
- [ ] **Step 2: 跑 RED**：`python3 -m pytest -q tests/unit/test_chat_horizon_matrix.py tests/unit/test_chat_time.py tests/unit/test_chat_policy.py tests/unit/test_chat_request_resolution.py`；期望失败是“半年内”走 quote、近一年/18个月无窗口、短中长期无默认，而非测试错误。
- [ ] **Step 3: 实现通用解析**：用受限 regex 捕获中文/阿拉伯数字和明确单位；交易日产生 rolling-trading-day 窗口，自然日/周/月/年计算 inclusive date bounds；半年转换 6 个自然月；数值先安全校验再做 date arithmetic，超过 800 个月时 `has_market_period_expression()` 仍返回 true、窗口解析返回受控超范围结果，route 仍是趋势并给明确超限澄清、不调用 provider。显式窗口优先。无显式期限时只匹配明确的短期/中期/长期词，分别给 5TD/6mo/3y。保留财务词与行情词先后规则。
- [ ] **Step 4: 实现路由/解析统一**：`QuestionRouter` 对市场对象+通用 timeframe 返回 market_trend/market_recap；quote 只用于当下/最新快照。`resolve_chat_request()` 将 window 与 origin 一次冻结并透传 effective question，避免模型 planner 改写期限。
- [ ] **Step 5: 跑 GREEN 与防回归**：重复 Step 2 命令，并运行 `python3 -m pytest -q tests/unit/test_chat_time.py tests/unit/test_chat_policy.py tests/unit/test_chat_request_resolution.py tests/unit/test_chat_execution_plan.py`；所有既有财务/澄清和短期 query 均保持语义。
- [ ] **Step 6: 提交**：仅提交 parser/router/fixture/tests，commit `feat: parse generic horizons for market questions`。

### Task 2: 动态 K 线粒度、同粒度事实与真实覆盖

**Files:**
- Modify: `webapp/execution_executor.py`
- Modify: `webapp/chat_facts.py`
- Modify: `webapp/chat_execution.py`
- Modify: `webapp/chat_verifier.py`
- Modify: `financial_report_fetcher/rag/qa.py`
- Test: `tests/unit/test_execution_executor.py`, `tests/unit/test_chat_facts.py`, `tests/unit/test_chat_verifier.py`, `tests/unit/test_chat_execution.py`

**Interfaces:**
- `market_kline_grain(window: MarketWindow) -> Literal['day','week','month']`：依自然跨度估算根数，优先最细可用粒度。
- `market_kline_request_count(window: MarketWindow, grain: str) -> int`：结果始终在 1..800，且足以覆盖所选窗口估算区间。
- `facts_from_market_result()` 产生日/周/月事实，Fact `period_kind` 明确粒度，`evidence_ids` 仍带源调用 ID/对象/period。

- [ ] **Step 1: 写失败执行器测试**：冻结时间验证 5TD、1年选 day；5年选 week；超过 800 周估算才选 month；大于 800 月份拒绝；每种 count ≤800。替身 K 线乱序、重复、越窗时断言输出窗口与 grain 严格匹配。
- [ ] **Step 2: 写失败事实/验证测试**：同一 payload 分别标记 day/week/month；事实保存 `period_kind` 且 source_category=market；周/月事实支持相应窗口/period 的数字核验，错误 grain、日期或主体阻断；fallback 使用“周线/月线区间”，不得输出“交易日”假标签。
- [ ] **Step 3: 跑 RED**：`python3 -m pytest -q tests/unit/test_execution_executor.py tests/unit/test_chat_facts.py tests/unit/test_chat_verifier.py tests/unit/test_chat_execution.py`；期待发现固定日线和 `period_kind=daily` 专用行为。
- [ ] **Step 4: 实现中央粒度选择**：将 server 的个股/指数 K 线 handler 与 executor 共享 `market_kline_grain/count`；每次调用最多 800 bars。对返回 rows 按同一 MarketWindow 过滤排序，coverage 中保留 query window 与实际 `min/max period`；校验两侧边界缺失时 partial，不按 HTTP/provider success alone 判完整。
- [ ] **Step 5: 扩展事实链**：facts normalizer 记录 grain 和原 period；verifier 按 day/week/month interval semantics 关联声明与事实；派生计算只对兼容频率的输入执行；market fallback 使用事实原 grain、首末日期与收盘价，验证器再次核验。
- [ ] **Step 6: 跑 GREEN**：重复 Step 3，另运行 `python3 -m pytest -q tests/unit/test_source_adapters.py tests/unit/test_chat_models.py`；所有已有日 K tests 保持通过。
- [ ] **Step 7: 提交**：仅提交粒度、facts、coverage 与对应 tests，commit `feat: select bounded kline grain for market horizons`。

### Task 3: 大盘长期来源合同、答案回退与 SSE 状态

**Files:**
- Modify: `webapp/server.py`
- Modify: `webapp/execution_planner.py`
- Modify: `webapp/execution_plan.py`
- Modify: `webapp/chat_execution.py`
- Test: `tests/unit/test_chat_execution_plan.py`, `tests/unit/test_execution_plan_recap.py`, `tests/unit/test_server_api.py`

**Interfaces:**
- market_recap 计划必须含四指数来源；`market_overview` 仅当其实际支持时间窗口（今日/5TD）且当前授权/预算可用时进入可用 steps。
- 中长期大盘 K 线即使无长期广度也可产出主要指数受限回答；状态/正文明确缺少长期广度，不伪称完整全市场。
- market trend/recap 在 `answer_completion_exhausted` 或 numeric verifier blocked 时仅用已绑定的 run facts 生成并复验 fallback。

- [ ] **Step 1: 写失败计划与大盘状态测试**：Q9 包含 time-matched `market_overview`；Q10–Q14 计划只请求四指数，不为长窗口调用今日/5日广度，API 调用预算正确，最终回答明确“不含长期广度”，status 不得 failed 也不得 completed-as-full-market。财务、本地 retrieval 不得进入 market plan。
- [ ] **Step 2: 写失败 SSE 错误路径测试**：使用 deterministic Tencent 替身为个股和四指数提供周/月 bars；AI 对初答+两次续写都 `finish_reason=length`，服务器仍以事实 fallback 返回可核验答案；强制错误数值时不泄露错误数字；空/错主体/过期数据返回 partial/failed 的真实原因。
- [ ] **Step 3: 跑 RED**：`python3 -m pytest -q tests/unit/test_chat_execution_plan.py tests/unit/test_execution_plan_recap.py tests/unit/test_server_api.py -k 'market or horizon or exhausted'`；失败必须能定位到 overview 时间不匹配、answer-only/quote route 或补全耗尽早退。
- [ ] **Step 4: 改执行计划能力**：为非今日/近5TD请求，在构造 `available_kinds` 前过滤 `market_overview`；保留 validate contract 对所有实际可用兼容来源的必需规则；safe fallback plan 不能把 quote 当 K 线。
- [ ] **Step 5: 统一 market fallback 错误出口**：server completion-exhausted 与 verifier-blocked 调用同一函数；它只收本轮匹配 MarketWindow/Scope 的事实，运行 ClaimVerifier，只有 passed 才输出；否则给出简短领域化解释。run 持久化 `window_origin`、实际 grain、source coverage 与安全 fail code。
- [ ] **Step 6: 运行 GREEN**：重复 Step 3，并运行 `python3 -m pytest -q tests/unit/test_chat_runs.py tests/unit/test_chat_store.py tests/unit/test_source_runtime.py`；检查 done SSE、AnswerRun、facts/artifact 和日志 run ID 一致。
- [ ] **Step 7: 提交**：仅提交执行计划、SSE 与 tests，commit `fix: bind market plans and fallback to horizon evidence`。

### Task 4: 持久化 query 验收矩阵、全套测试和交付记录

**Files:**
- Modify: `tests/fixtures/market_horizon_queries.json`
- Modify: `tests/unit/test_chat_horizon_matrix.py`
- Modify: `tests/unit/test_server_api.py`
- Modify after full validation/merge: `docs/FEATURE-CATALOG.md`, `docs/superpowers/DESIGN-REGISTRY.md`

**Interfaces:** query fixture schema 包含 `id`, `query`, `expected_intent`, `subject_kind`, `window_origin`, `window_start`, `window_end`, `grain`, `required_steps`, `expected_coverage_note`, `expected_status`；测试通过现有 TestClient/SSE 与固定外部数据替身加载，不能跳过整条 request resolution → planner → executor → facts → verifier → persistence 链。

- [ ] **Step 1: 扩展 matrix SSE 回放**：对 Q1–Q16 每个 query 发送真实 API SSE 请求；按 fixture 断言 route、冻结 window、plan/source、实际 provider operation/count/grain、源结果日期、facts 粒度、verification、答案边界及 terminal status。
- [ ] **Step 2: 加入变体与负向**：Q3“半年内”原 run_id 问题；显式 `近1年`、`过去18个月`、`近两年`；年边界/闰日；不完整 Q1/Q3、超界 Q16；短期广度 source failure 不抹除 index facts；错误 provider rows 不得通过。
- [ ] **Step 3: 实际离线回放 RED/GREEN**：运行 `python3 -m pytest -q tests/unit/test_chat_horizon_matrix.py tests/unit/test_server_api.py -k 'horizon or market'`；每个固定 query 都给可审计失败/通过结果。
- [ ] **Step 4: 跑功能聚焦回归**：`python3 -m pytest -q tests/unit/test_chat_time.py tests/unit/test_chat_policy.py tests/unit/test_chat_request_resolution.py tests/unit/test_execution_executor.py tests/unit/test_chat_facts.py tests/unit/test_chat_verifier.py tests/unit/test_chat_execution.py tests/unit/test_chat_execution_plan.py tests/unit/test_execution_plan_recap.py tests/unit/test_execution_market_recap.py tests/unit/test_source_adapters.py tests/unit/test_server_api.py tests/unit/test_rag_qa.py tests/unit/test_chat_horizon_matrix.py`。
- [ ] **Step 5: 跑完整测试**：`python3 -m pytest -q`，记录 pass/skip/fail；任何本变更直接失败必须修复，其他失败单独重跑并给证据，不以范围缩小掩盖。
- [ ] **Step 6: 本地服务 query 验证**：完成所有离线测试后重启本地服务。按 Q1–Q15 逐条调用 `/api/chat/stream`，记录 query、run ID、source op/grain、覆盖窗口、verification、终态与可见答案；Q16 期望明确范围拒绝。仅在本地开发实例，不操作生产账号。provider outage 要记录为外部阻断，不能伪称功能通过。
- [ ] **Step 7: Review/diff**：`git diff --check`；验证 Q3 和 Q4 都不再路由 `market_quote`；核对所有使用的 answer facts 与来源日期、用户不受支持的内容有明确限制。
- [ ] **Step 8: 合并前文档**：在功能目录追加真实合并 SHA 和 query/测试证据；将 DESIGN-REGISTRY 对应条目从实施中更新为已实现并附测试命令/结果。只在代码合并后执行此步骤。

## Acceptance Checklist

- Q1–Q16 在离线真实 SSE pipeline 中逐条达到 fixture 预期。
- 半年/一年/18个月/两年/三年/五年等非固定词例都走时间窗而非 quote；通用值不靠每个 horizon 手写一个 branch。
- 不超过 800 根的可用窗口按 day/week/month 粒度得到完整覆盖；超过来源能力时可用粒度降采样或明确 partial/reject，绝不静默换短窗口。
- 中长期大盘四指数答案诚实标注“主要指数观察，不含对应长期全市场广度”。
- 所有数值事实由同对象/时间/粒度来源证据支持，fallback 同样过验证。
- `python3 -m pytest -q` 的结果和任何例外明确记录。
- 按 Q1–Q15 真实本地服务验证；不以 fixture 通过冒充外部行情真实可用。
