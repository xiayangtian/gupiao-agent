# 非 P0 改进路线图

本文档沉淀 P0 可靠性修复之后的 P1/P2 backlog。每个条目均给出当前可观察行为、目标状态、前置依赖和可执行的验收标准；完成状态以测试结果、接口字段或运行指标为准。

## P0 基线（已完成，不作为待办）

下列三项是本轮可靠性修复的基线，后续条目直接建立在其上，不重复安排季度身份、任务状态持久化或基础事实模型工作。

| 基线 | 已交付能力 |
| --- | --- |
| 报告身份 | `report_id` 使用 `ticker:真实报告期:report_type`；一季报和三季报不会碰撞；无法从旧季度文件名判断报告期时拒绝猜测。 |
| 任务状态 | Web 任务状态写入 SQLite；服务重启后已完成任务可查询，遗留运行任务转为可重试失败；保留取消、并发和终态清理语义。 |
| 财务事实 | `Evidence`、`FinancialFact`、`ValidationSummary` 已有独立模型；有限数值、年份、收入和比率范围经过程序校验；兼容输出继续提供 `metrics`，并增加 `schema_version=2`、`facts`、`validation`。 |

## 路线图总览

| 领域 | 条目 | 优先级 | 交付顺序 |
| --- | --- | --- | --- |
| Agent 可信度 | P1-TRUST-01 结论级证据链 | P1 | 1 |
| Agent 可信度 | P1-TRUST-02 确定性公式复算 | P1 | 1 |
| Agent 可信度 | P1-TRUST-03 Prompt Injection 防护 | P1 | 1 |
| Agent 可信度 | P1-TRUST-04 低置信度事实定向重试 | P1 | 1 |
| Agent Runtime | P1-RUNTIME-01 可恢复的多轮工具循环 | P1 | 2 |
| Agent Runtime | P1-RUNTIME-02 工具 Schema 与白名单执行边界 | P1 | 2 |
| Agent Runtime | P1-RUNTIME-03 会话隔离与 TTL | P1 | 2 |
| Agent Runtime | P1-RUNTIME-04 分析请求幂等 | P1 | 2 |
| Agent Runtime | P1-RUNTIME-05 Web 任务契约与生命周期回归 | P1 | 2 |
| Agent Runtime | P1-RUNTIME-06 多进程任务 ownership 与 lease | P1 | 2 |
| 工程架构 | P1-ARCH-01 按职责拆分分析模块 | P1 | 3 |
| 工程架构 | P1-ARCH-02 统一配置来源与版本 | P1 | 3 |
| 评测体系 | P1-EVAL-01 固定样本防回归评测 | P1 | 4 |
| 产品能力 | P2-PRODUCT-01 跨期与跨公司对比 | P2 | 5 |
| 产品能力 | P2-PRODUCT-02 扫描型 PDF OCR | P2 | 5 |
| 产品能力 | P2-PRODUCT-03 结构化导出 | P2 | 5 |
| 运维能力 | P1-OPS-01 端到端 trace 与审计事件 | P1 | 6 |
| 运维能力 | P2-OPS-02 成本与用量面板 | P2 | 6 |

---

## 一、Agent 可信度

### P1-TRUST-01：结论级证据链

- **现状：** RAG 问答能从检索片段生成 `[n]` 引用并返回页码字段，但分析报告中的维度结论仍以自由文本为主；P0 允许事实的 `Evidence` 为空，因此不能保证每个数字和风险判断都能定位到原文。
- **目标：** 将分析输出统一为 `Claim`，每条 Claim 至少关联一个已存在 `DocumentChunk` 的 `chunk_id`、页码、短引文和置信度；证据不存在、引用编号非法或引文不匹配时，Claim 标记为 `warning/failed`，报告只能进入 `partial` 或失败态。
- **依赖：** P0 `facts`/`schema_version=2`；分页抽取与 `DocumentChunk` 索引；Validator；Markdown/JSON 引用字段和前端引用卡片。
- **可观察验收标准：** 使用 20 份固定财报样本，所有包含数字或风险等级的 Claim 均输出 `chunk_id`、`page`、`quote`；抽样 100 条 Claim 的引用可在原文逐字定位率达到 100%；伪造 `chunk_id`、越界页码或不在 chunk 内的 quote 会被拒绝，并在 `validation.messages` 中留下稳定错误码。

### P1-TRUST-02：确定性公式复算

- **现状：** P0 已校验有限数值、单位和范围，但同比、毛利率、净利率、资产负债率等派生指标仍可能直接来自模型文本，尚无统一的计算输入、公式版本和误差校验。
- **目标：** 先扩展 P0 `FinancialFact` 原始字段契约 `facts_schema=v2.1`，至少包含 `revenue`、`net_profit`、`gross_profit`、`total_assets`、`total_liabilities`、`equity_begin`、`equity_end`，并保留每个字段的 `period`、`unit`、Evidence 和 fact ID；新增纯程序 `MetricCalculator`，模型只负责抽取原始事实和解释变化。计算契约 `metric_formula_version=1.0.0` 固定为：收入同比=`(revenue_t-revenue_{t-1})/abs(revenue_{t-1})*100`；净利润同比=`(net_profit_t-net_profit_{t-1})/abs(net_profit_{t-1})*100`；毛利率=`gross_profit_t/revenue_t*100`；净利率=`net_profit_t/revenue_t*100`；ROE=`net_profit_t/((equity_begin_t+equity_end_t)/2)*100`；资产负债率=`total_liabilities_t/total_assets_t*100`。收入同比、净利润同比、毛利率和净利率均使用同一报告期间的流量事实；ROE 使用期初/期末权益平均值，资产负债率使用报告期末时点的资产和负债；所有输出按不超过 4 位小数计算、最终展示保留 1 位小数。
- **依赖：** `FinancialFact` 扩展为 `facts_schema=v2.1`（明确 `gross_profit`、`total_assets`、`total_liabilities`、`equity_begin`、`equity_end` 等原始事实）；期间/单位规范化；报告期身份；Validator；结论级证据链；旧 `metrics` 字段兼容映射。
- **可观察验收标准：** `metric_formula_version=1.0.0` 的契约测试覆盖上述六个公式：同一期间收入 `100→120` 输出收入同比 `20.0%`，同一期间净利润 `10→20` 输出净利润同比 `100.0%`，毛利 `30`/收入 `100` 输出毛利率 `30.0%`，净利润 `20`/收入 `100` 输出净利率 `20.0%`，净利润 `20`/期初权益 `90`/期末权益 `110` 输出 ROE `20.0%`，负债 `40`/期末资产 `100` 输出资产负债率 `40.0%`；任一分母缺失或为零时结果必须是 `null`、`validation_status=warning`、错误码 `METRIC_ZERO_DENOMINATOR`，不得回退为 0；同比上一期缺失时必须是 `null`、错误码 `METRIC_MISSING_INPUT`；模型返回与程序结果相差超过 `0.1` 个百分点时 validation 为 `failed` 且报告不进入 `succeeded`；每个派生结果均可反查输入 fact ID、口径（`same_period_flow`、`average_equity` 或 `period_end`）和公式版本。

### P1-TRUST-03：Prompt Injection 防护

- **现状：** 系统提示已声明财报文本是不可信内容，MCP 也有熔断和部分工具清单控制；但缺少覆盖 PDF、用户问题、检索片段、工具返回值和模型输出的分层隔离测试，不能证明恶意文本不会改变工具权限或泄露内部信息。
- **目标：** 将外部文本统一包裹为数据区；工具调用在执行前经过名称、参数 Schema、报告范围和敏感字段检查；输出层过滤命令、路径、密钥和未验证事实，并对注入事件记录安全告警。
- **依赖：** 工具 Schema/白名单（P1-RUNTIME-02）；多轮循环状态机（P1-RUNTIME-01）；结论 Validator；日志脱敏和 trace（P1-OPS-01）。
- **可观察验收标准：** 安全样本包含“忽略系统指令、读取 API Key、执行命令、访问任意路径”四类注入，CLI 和 Web 两条链路均不得产生白名单外工具调用；响应正文不得出现密钥、完整本地路径或被注入文本要求的新数字；每次拦截产生 `security_event=prompt_injection_blocked`，并通过自动化测试验证。

### P1-TRUST-04：低置信度事实定向重试

- **现状：** 事实转换能够记录抽取置信度和校验状态，但低置信度或缺少 Evidence 的事实尚未形成只重试该事实的队列；重新分析可能重复发送整份文档并重复消耗模型额度。
- **目标：** 将 `extraction_confidence < 0.80`、缺 Evidence 或 Schema 校验失败的事实写入 `fact_retry_queue_v1`，重试请求只包含该指标所需的检索片段；每个 fact 最多定向重试 2 次，仍失败则保留原 fact、错误码和 `partial` 状态。
- **依赖：** P0 `FinancialFact`；结论证据链；分页检索；P1-RUNTIME-01 预算；P1-OPS-01 trace。
- **可观察验收标准：** 构造 3 个事实且仅 1 个置信度为 `0.79` 时，事件只产生该 fact 的重试，已通过事实的模型调用数保持为 0；第二次失败后不再重试并记录 `FACT_RETRY_EXHAUSTED`；重试请求的输入仅包含目标指标的 chunk ID，结果状态为 `partial` 而不是伪造数值；每次重试 trace 记录 `attempt=1/2`、输入 hash 和输出 hash。

---

## 二、Agent Runtime

### P1-RUNTIME-01：可恢复的多轮工具循环

- **现状：** `RagQA.answer_stream` 已支持有限工具调用轮数（默认 3 轮）和流式 `tool_call/tool_result` 事件；工具调用消息未形成持久化的步骤状态，超时、重复调用、部分失败和进程中断后的恢复语义仍不统一。
- **目标：** 把每轮模型决策、工具调用、工具结果和最终回答建模为 `analysis_steps`/事件；配置契约 `runtime_budget_v1` 固定包含 `max_tool_rounds=3`、`max_total_tokens=12000`、`max_elapsed_seconds=90`、`per_tool_timeout=30`（秒）；工具失败只重试失败步骤，达到预算后输出可解释的 `partial`。
- **依赖：** P0 SQLite 任务状态；工具 Schema/白名单；trace 字段；已有 `max_tool_rounds`、MCP 熔断器和流式事件协议；统一配置对象中的 `runtime_budget_v1`。
- **可观察验收标准：** 模拟“搜索→指标→解释”三轮调用时，事件序列包含轮次、工具名、输入/输出 hash 和耗时；同一 `tool_call_id` 重放不会产生第二次外部副作用；测试设置 `max_tool_rounds=3`、累计 token `12001`、耗时 `91` 秒和单工具 `31` 秒四种边界，均停止新增工具调用并返回错误码 `RUNTIME_BUDGET_EXCEEDED` 或 `TOOL_TIMEOUT`；“非必要工具调用”定义为最终回答已通过 Schema/Claim 校验后、重复的 `name+arguments_hash`，或不能增加新 fact/citation 的调用，测试均拒绝并记录 `TOOL_CALL_NOT_NEEDED`；单个工具超时不会丢失已完成轮次，重试只增加该步骤的重试计数。

### P1-RUNTIME-02：工具 Schema 与白名单执行边界

- **现状：** MCP 原生 `inputSchema` 可转换为 function calling 格式，配置支持 `mcp_tool_whitelist`；仍存在通用工具调用入口，参数在所有调用路径上未统一执行结构化校验，返回值也没有统一大小、类型和敏感信息限制。
- **目标：** 建立版本化工具注册表契约 `tool_registry_v1`：每个工具声明名称、用途、输入/输出 Schema、允许的报告范围、超时和最大返回长度；只有注册且通过 Schema 校验的工具可注入模型和执行，拒绝未知字段、错误类型、越权报告 ID 和超长结果。统一错误码为 `TOOL_NOT_ALLOWLISTED`、`TOOL_SCHEMA_INVALID`、`TOOL_SCOPE_DENIED`、`TOOL_RESULT_TRUNCATED`。
- **依赖：** `StockMCPClient.list_tools()`；`RagConfig.mcp_tool_whitelist`；P1-RUNTIME-01 循环状态机；P1-TRUST-03 安全事件。
- **可观察验收标准：** 工具清单接口声明 `tool_registry_v1`，只返回注册表中的工具且数量不超过配置上限；对未知工具、缺少必填参数、错误类型、额外字段和跨报告参数分别返回 `TOOL_NOT_ALLOWLISTED`、`TOOL_SCHEMA_INVALID` 或 `TOOL_SCOPE_DENIED`；任一工具返回超过 `tool_result_max_chars` 的内容会被截断并记录原始长度及 `TOOL_RESULT_TRUNCATED`；执行日志中的工具名、Schema 版本全部能在 `tool_registry_v1` 反查。

### P1-RUNTIME-03：会话隔离与 TTL

- **现状：** Web Chat 已接收 `session_id` 并将会话落盘，但缺少跨入口的访问边界、最大轮数/Token 上限和过期清理契约；错误的或过期的 ID 需要统一行为才能避免历史消息串线。
- **目标：** 会话记录使用 `session_schema=v1`，包含 `session_id`、创建/更新时间、所属用户作用域、`max_turns=20`、`max_tokens=32000` 和 `ttl_seconds=86400`；每次读取、追加和流式回答均校验作用域与 TTL，过期会话返回 `SESSION_EXPIRED` 并创建新会话，不读旧消息。
- **依赖：** `ChatStore`；统一配置来源与版本；P1-OPS-01 trace 的 session 字段；现有 `/api/chat/stream` 和会话管理 API。
- **可观察验收标准：** 两个 `user_id` 各自创建 session 并交叉提问时，响应和持久化消息不共享任何 `message_id` 或正文；`user_b` 使用 `user_a` 的 `session_id` 读取或追加均返回 `SESSION_SCOPE_DENIED`，不改变原会话；设置更新时间超过 `86400` 秒的会话读取返回 `SESSION_EXPIRED`，清理作业删除该记录并创建新的 `session_id`，新会话历史为空；第 21 轮或累计 token 超过 `32000` 时返回 `SESSION_LIMIT_EXCEEDED`；Web 重启后有效会话可继续，过期会话不被恢复；审计事件仅记录 `session_id` hash，不记录完整问题正文。

### P1-RUNTIME-04：分析请求幂等

- **现状：** 下载和 RAG 摄取已有部分幂等处理，但分析 API 没有统一的请求幂等键和并发去重契约；相同报告被重复提交时可能重复调用模型和产生重复任务。
- **目标：** 采用服务端派生的 `idempotency_key_v1`：将 `content_hash + report_type + period + prompt_version + model + dimensions` 规范化为 canonical JSON，计算 SHA-256 得到 key，并在任务记录中同时持久化 `canonical_request_hash`；成功结果直接复用，运行中请求返回已有 `run_id`。查到已有 key 时必须重新计算并比较 canonical hash；若存储记录被篡改、来自旧版本或出现 hash 冲突，返回稳定错误码 `IDEMPOTENCY_RECORD_CONFLICT`，不覆盖旧结果。
- **依赖：** P0 报告身份、SQLite 任务 Store 和 `content_hash`；统一配置；P1-OPS-01 trace；旧 `task_id` 到 `run_id` 映射。
- **可观察验收标准：** 并发提交 10 次完全相同请求只创建 1 个 `run_id` 且模型调用数为 1；同一派生 key 命中成功结果时响应带 `reused=true`；测试夹具向已有 key 写入不同的 `canonical_request_hash`（模拟存储冲突/篡改）时返回 `IDEMPOTENCY_RECORD_CONFLICT`，不覆盖原任务；请求超时后再次提交仍能查询原任务，不创建孤儿任务；SQLite 对派生 key 建唯一约束，canonical JSON 字段排序、日期格式和 dimensions 排序的契约测试固定输出 hash。

### P1-RUNTIME-05：Web 任务契约与生命周期回归

- **现状：** TaskManager 的取消、重试、并发和服务重启已有基础测试，但部分更新路径可能缺少 `task_id`；生命周期回归未覆盖所有状态转换，旧的 `storage_error` 结果仍可能残留而没有统一处理。
- **目标：** 发布 `task_event_schema=v1`，所有 `create/update/get/cancel/retry/shutdown` 事件都必须携带非空 `task_id`；定义 `pending→running→done|failed|cancelled` 和重启 `running→failed(retryable=true)` 的合法转换；`TaskStore.update(unknown_task_id)` 固定返回 `False` 且不写入；manager 将其映射为 `TASK_NOT_FOUND`。`storage_error` 统一映射为 `TASK_STORAGE_UNAVAILABLE` 并保留可重试标记，下一次存储成功后必须清除旧错误。
- **依赖：** P0 SQLite TaskStore；P1-RUNTIME-01 事件；旧 API 的 `task_id` 兼容字段；服务脚本和测试夹具。
- **可观察验收标准：** 生命周期测试覆盖每条合法转换及非法逆向转换，所有事件的 `task_id` 与任务查询 ID 一致；直接调用 `TaskStore.update("missing", status="done")` 返回 `False`、数据库行数不变，manager 层返回 `TASK_NOT_FOUND`；模拟 SQLite 写失败时 API 返回 `TASK_STORAGE_UNAVAILABLE`、不返回 200 成功态且日志含数据库路径，随后恢复写入并验证任务 `error=null` 且不再返回旧 `storage_error`；取消、并发排队、shutdown、重启恢复各执行至少 1 次回归测试；任一事件缺 task ID 使 Schema 校验失败并阻断写入。

### P1-RUNTIME-06：多进程任务 ownership 与 lease

- **现状与部署约束：** 当前 TaskManager 的实例锁只保护单个 Python 进程内的线程，SQLite 任务记录没有 `owner_id`、lease、心跳或 fencing token。正式服务必须只运行 **1 个服务进程、1 个 TaskManager**，禁止多个 worker、多个 manager 或多个服务实例共享同一个 `tasks.sqlite3`；否则一个 manager 启动时可能把另一个仍在执行的任务恢复为失败，且无法证明外部副作用只执行一次。
- **目标：** 为任务记录增加不可空 `owner_id`、`lease_expires_at` 和单调递增 `fencing_token`；领取、续租、终态写入和过期接管均使用 SQLite 条件更新，只有当前 owner 且 fencing token 匹配时才能推进状态。恢复逻辑只接管 lease 已过期的 `pending/running`，不得修改其他存活 owner 的任务。
- **依赖：** P0 SQLite TaskStore；P1-RUNTIME-04 幂等键；P1-RUNTIME-05 状态机与事件契约；可注入时钟、进程级 owner ID 和服务健康检查。
- **可观察验收标准：** 两个独立进程的 TaskManager 共享同一临时 SQLite 并并发领取 20 个任务时，每个任务恰好只有一个 owner、业务 callable 与外部副作用各执行一次；owner A 持续续租时 owner B 启动和执行恢复不得改变 A 的任务；停止 A 的心跳并推进时钟越过 `lease_expires_at` 后，B 只能通过一次条件更新接管且 `fencing_token` 严格增加；A 使用旧 token 的进度或终态写入返回稳定错误码 `TASK_OWNER_FENCED` 且数据库不变；多进程测试完成后不存在同时有效的双 owner、重复终态或长期 `running` 孤儿任务。完成该验收并在启动配置中显式支持 worker 数之前，不得解除单服务进程约束。

---

## 三、工程架构

### P1-ARCH-01：按职责拆分分析模块

- **现状：** `ReportAnalyzer` 仍承担 Prompt 模板、全文/RAG 上下文、模型调用、指标抽取、报告序列化和导出等职责；新增分析维度或校验规则需要触及大型模块，局部失败边界不清晰。
- **目标：** 按 `DocumentIngestion`、`SearchTool`、`FactExtraction`、`MetricCalculator`、`SpecialistAgent`、`Validator`、`Synthesizer`、`RunManager` 拆分职责；`ReportAnalyzer` 保留为兼容门面，只负责组装依赖和映射旧字段。
- **依赖：** P0 事实模型和任务 Store；结论证据链；多轮 Runtime；现有 CLI、Web API、Markdown/JSON 兼容测试。
- **可观察验收标准：** 每个组件可在单元测试中使用替身独立运行；新增一个分析维度只需注册其 Schema、Prompt 和工具权限，不修改 `ReportAnalyzer` 控制流；一次维度失败只将该维度标记失败而不丢失其他维度；旧入口的 `metrics`、`dimensions` 和 `task_id` 回归测试全部保持通过。

### P1-ARCH-02：统一配置来源与版本

- **现状：** AI 配置、RAG 配置、MCP 工具参数和任务数据库路径分散在环境变量、`config.yaml`、模块默认值和启动脚本中；同一参数在 CLI、Web 和测试中的默认来源可能不同。
- **目标：** 使用一个带 `config_version` 的应用配置对象声明 AI、RAG、工具、任务、评测和可观测性配置；明确优先级为“显式构造参数 > 环境变量 > 配置文件 > 默认值”；启动时一次解析并向各组件注入只读配置快照。
- **依赖：** `ConfigLoader`、`AppConfig`、`RagConfig`；启动脚本；P0 任务数据库路径；现有 `config.example.yaml` 和配置校验测试。
- **可观察验收标准：** CLI 与 Web 对同一配置文件解析出相同 JSON 快照；四级来源冲突时结果严格符合优先级；缺失、错误类型和非法范围在启动前报告字段路径且不启动服务；日志只输出配置键和脱敏值，不输出 API Key 的任何片段；快照包含 `config_version` 并写入每次 run 的 trace。

---

## 四、评测体系

### P1-EVAL-01：固定样本防回归评测

- **现状：** 仓库已有单元、集成和属性测试，覆盖身份、任务和事实校验；尚无带人工标注事实、证据、风险判断和固定 Prompt/模型版本的端到端质量基线，无法在模型或检索变更后比较质量。
- **目标：** 按 `eval_contract_v1` 建立两阶段版本化评测集。阶段 A 先使用至少 20 份可抽取文本（年报、半年报、一季报、三季报、表格密集和含恶意指令样本）建立质量基线；阶段 B 在 OCR 接入前收集至少 5 份扫描 PDF 的人工转录 baseline，记录 OCR 前可用性但不将其混入门禁；当 P2-PRODUCT-02 通过 `ocr_eval_v1` 后，扫描样本才纳入同一门禁。统一记录 7 项指标：事实准确率、证据命中率、公式一致率、风险判断一致率、拒答准确率、P95 延迟、单报告 token 成本。
- **依赖：** 结论级证据链；公式复算；OCR（P2-PRODUCT-02，先 baseline 后门禁）；trace（P1-OPS-01）；可固定的 Prompt、模型、检索参数和工具版本。
- **可观察验收标准：** 每次代码、Prompt、模型或检索参数变更都能用同一命令生成带 `eval_contract_v1` 和数据集版本的报告。阶段 A 的 7 项门禁分别为：事实准确率≥95%，证据命中率≥98%，公式一致率=100%，风险判断一致率≥90%，拒答准确率≥95%，P95 延迟不超过文本基线的 120%，单报告 token 成本不超过文本基线的 120%；每项均与上一个基线逐项比较，任一指标低于阈值或相对基线下降超过 2 个百分点（延迟/成本超过 20%）即阻断发布并列出失败样本。阶段 B 先输出扫描样本 baseline；OCR 接入且 `ocr_eval_v1` 达到“关键数字/标题识别率≥95%、低置信度页不产出事实”后，扫描样本的上述 7 项指标才进入发布门禁。

---

## 五、产品能力

### P2-PRODUCT-01：跨期与跨公司对比

- **现状：** 单报告问答可按 `report_id` 过滤检索，通用 RAG 已可跨报告检索；分析页没有统一的跨期/跨公司选择器、指标对齐、单位转换和对比结果结构。
- **目标：** 提供可选公司、报告期和报告类型的对比请求；由程序按 `report_id` 读取已校验事实，统一期间、单位和缺失值，输出趋势表、差异百分比、数据来源和“未披露”标记，不让模型自行补齐缺失值。
- **依赖：** P0 报告身份；P0 `FinancialFact`；公式复算；结论证据链；通用 RAG 过滤和前端图表组件。
- **可观察验收标准：** 选择两家公司和至少两个报告期时，API 返回稳定的 `series[company][period][metric]` 结构及每个值的 fact/citation；缺失指标返回 `null` 和 `status=undisclosed`；相同输入重复请求结果的排序、数值和引用完全一致；用手算样本验证同比和公司间差异均在 `0.1` 个百分点或 `0.01` 原始单位误差内。

### P2-PRODUCT-02：扫描型 PDF OCR

- **现状：** 文本抽取对扫描型 PDF 可能得到空文本；系统能识别 `ocr_required` 或返回明确错误，但无法在现有分析和 RAG 流程中提供 OCR 文本及页码证据。
- **目标：** 引入可配置 OCR 适配器和状态契约 `ocr_status_v1`。`ocr_required` 表示是否需要 OCR，独立于 `ocr_status`；状态严格为 `pending→running→complete` 或 `pending→running→failed`，逐页生成带页码、语言和置信度的文本；低置信度页进入人工可见的待核验状态，OCR 原文与原始 PDF hash 绑定后才能进入检索和事实抽取。
- **依赖：** `DocumentIngestion` 拆分；结论证据链；统一配置；评测集 `eval_contract_v1` 的扫描 baseline；部署环境中明确版本的 OCR 引擎和资源限制；OCR 识别契约 `ocr_eval_v1`。
- **可观察验收标准：** 文本型 PDF 验收为 `ocr_required=false`、`ocr_status=complete` 且不调用 OCR；扫描型成功样本验收为 `ocr_required=true`，状态事件依次为 `pending/running/complete`，每页有文本 hash，`ocr_eval_v1` 的关键数字和表格标题识别率≥95%；扫描型失败样本验收为 `ocr_required=true`，状态依次为 `pending/running/failed`，记录错误码 `OCR_FAILED` 和失败页码，不产生确定性事实，报告为 `partial`；置信度低于 `0.80` 的页记录 `OCR_LOW_CONFIDENCE` 并排除事实抽取；同一 PDF hash 重复摄取命中缓存且不重复计费。

### P2-PRODUCT-03：结构化导出

- **现状：** `ReportAnalyzer` 已支持 Markdown 和 JSON 导出，JSON 兼容 `metrics` 并新增事实/校验字段；尚无面向对比表、证据清单和审计信息的统一 CSV/XLSX 导出契约，也没有大结果集的导出进度反馈。
- **目标：** 增加按报告或对比查询导出的 JSON、CSV 和 XLSX；导出包含 schema 版本、报告身份、期间、单位、事实、公式结果、引用、校验状态和生成时间；敏感字段脱敏，文件名由 `report_id` 安全生成。
- **依赖：** 统一事实/Claim Schema；跨期跨公司查询；结论证据链；统一配置中的导出目录和大小限制；Web 任务状态与下载接口。
- **可观察验收标准：** 同一输入生成的 CSV 列顺序、单位和行排序稳定；XLSX 符合 `export_schema=v1`，可由 `openpyxl>=3.1` 重新读取且数值与 JSON 一致；每条导出事实都能回指 `fact_id` 和 citation；超过大小限制时返回 `EXPORT_SIZE_LIMIT_EXCEEDED` 而不写半成品；导出任务在 API 中经历 `queued/running/done/failed` 并可查询错误原因。

---

## 六、运维能力

### P1-OPS-01：端到端 trace 与审计事件

- **现状：** 运行日志和任务事件分散在 Web、RAG、MCP 和分析模块；部分记录已有任务 ID、工具状态或 token，但尚未用同一个 trace 关联检索、模型、工具、校验和导出，排障需要拼接多份日志。
- **目标：** 发布 `trace_schema=v1`，统一规定每个分析/问答执行的规范查询键为 `run_id`，并强制 `trace_id == run_id`（Web `task_id` 仅作为一对一兼容别名）；每个步骤使用 `span_id` 和 `parent_span_id`。记录输入 hash、report_id、config/prompt/model 版本、检索命中、工具调用、耗时、token、重试、输出 hash、校验状态和错误码；日志默认脱敏并支持按 `run_id` 或等值 `trace_id` 查询。
- **依赖：** P0 SQLite 任务事件；多轮 Runtime；统一配置；工具 Schema；成本计量字段；现有日志格式和健康检查接口。
- **可观察验收标准：** `trace_schema=v1` 契约测试验证 `run_id == trace_id`，并验证 `task_id` 只能映射一个 run；一次含检索、两次模型调用和一次工具调用的 run 可通过 `GET /api/traces/{run_id}` 查询完整有序事件；每个 span 有开始/结束时间且耗时非负，模型事件包含 input/output token 或明确 `unknown` 原因。脱敏字段清单固定为 `api_key`、`authorization`、`pdf_text`、`user_prompt`、`tool_result_raw`、`local_path`，测试样本包含这些键和真实样例值，日志扫描不得命中其值；给定 `run_id` 能在 1 个接口响应中定位失败步骤和错误码。

### P2-OPS-02：成本与用量面板

- **现状：** AI 响应会携带部分 usage，配置已有模型、token 和工具轮数限制，但没有按报告、用户会话、模型、Prompt 版本聚合的成本记录，也没有预算超限告警或前端面板。
- **目标：** 发布 `cost_schema=v1`，单价单位固定为 CNY/1,000,000 tokens；估算公式固定为 `cost_cny = input_tokens*input_price_cny_per_million/1_000_000 + output_tokens*output_price_cny_per_million/1_000_000`，中间值不舍入，展示和聚合结果统一 `round(value, 6)`。基于 trace 的 token、调用次数、模型单价配置和工具耗时，计算每次 run、每个 `session_id`、每日报告、每个模型和每个 `prompt_version` 的输入/输出 token、估算成本、缓存命中率、重试率、P50/P95 延迟；Web 面板提供筛选、预算阈值和超限事件。
- **依赖：** P1-OPS-01 trace；统一配置中的模型价格和预算；多轮 Runtime 的调用计数；缓存和重试事件；`session_schema=v1`；Web 历史/图表组件。
- **可观察验收标准：** `cost_schema=v1` 手算样本使用两次调用（每次输入 1,000、输出 200 token，输入价 2.00 CNY/百万、输出价 8.00 CNY/百万），run 详情必须展示 2,400 token、成本 `0.007200` CNY（`2*1000*2/1e6 + 2*200*8/1e6`）；两次调用设为同一 `session_id=s1`、同一 `prompt_version=p1`、同一 `model=m1`、同一 `report_id=r1` 且发生在 `2026-08-30`，聚合 API 的 `by_day[2026-08-30]`、`by_report_id[r1]`、`by_session_id[s1]`、`by_prompt_version[p1]`、`by_model[m1]` 均必须展示 2,400 token 和 `0.007200` CNY，且各维度求和与原始 trace 一致；无 usage 的调用标记 `cost_status=unknown` 而不记为零；超过单 run 或日预算时产生 `COST_BUDGET_EXCEEDED` 并阻止下一轮非必要工具调用；面板显示至少 7 天数据和 P50/P95 延迟。

## 依赖与实施门槛

1. 先完成 P1-TRUST-01、P1-TRUST-02、P1-TRUST-04 和 P1-RUNTIME-02，再开放更多 MCP 工具或跨报告自动分析；未通过证据、公式和 Schema 校验的结果不得进入新产品能力。
2. P1-RUNTIME-01、P1-RUNTIME-03、P1-RUNTIME-04、P1-RUNTIME-05、P1-ARCH-01 和 P1-OPS-01 共享 run/step 事件契约；事件字段一旦发布，后续只增不删，并保留各契约的版本号。
3. P1-EVAL-01 阶段 A 先锁定文本样本门禁，阶段 B 只记录扫描样本 baseline；P2-PRODUCT-02 通过 `ocr_eval_v1` 后才将扫描样本纳入全部 7 项发布门禁；任何指标回退必须在评测报告中列出样本、差异和处理结论。
4. P2 产品项按“跨期/跨公司对比 → OCR → 结构化导出”顺序交付：对比依赖已校验事实，OCR 依赖证据链和评测集，导出依赖最终事实/Claim 契约。
