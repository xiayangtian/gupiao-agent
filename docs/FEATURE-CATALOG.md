# 功能目录

> **用途：** 让后续迭代能从“用户可见能力”快速定位到当前代码、契约、测试和变更原因，而不是仅靠提交日志猜测。  
> **适用范围：** 已合入 `main` 的功能；未合入分支的设计和实现不得写成既有能力。

## 使用方式

1. 先按下表找到要修改的功能；阅读其“实现入口”“不变量”和“验证”。
2. 再打开关联的设计/计划，了解为何采用当前方案。
3. 每次功能变更验证并合入后，执行者自动更新本目录中受影响功能的“最近变更”；若入口、契约或不变量改变，也同步更新对应字段。

本目录是**导航与维护索引**，不是实现真相来源：最终以 `main` 上的代码、测试和 API 为准。已确认设计的执行状态仍以 [方案设计台账](superpowers/DESIGN-REGISTRY.md) 为准。

## 功能总览

| 功能 | 用户能力 | 实现入口 | 验证入口 | 最近变更 |
| --- | --- | --- | --- | --- |
| [财报发现、下载与预览](#report-download) | 查询公司、下载本地 PDF、在浏览器预览 | `datasource.py`、`downloader.py`、`webapp/server.py` | `test_downloader.py`、`test_download_flow.py` | 见设计台账登记证据 |
| [证据化财报分析](#evidence-analysis) | 对单份财报生成可回溯分析、事实和证据 | `analysis_pipeline.py`、`evidence/`、`facts.py` | `test_analysis_pipeline.py`、`test_evidence_*.py` | 见设计台账登记证据 |
| [分析阅读、历史与可视化](#analysis-reading) | 渐进式主题阅读、历史报告、财务结构图表 | `analysis_workflow.js`、`analysis_visualizations.js`、`history.py` | `test_progressive_analysis_ui.py`、`test_visualizations.py` | `17e4dab`、`1107509` |
| [RAG 知识库与检索](#rag) | 摄取 PDF、跨报告或限定范围问答、可选重排序 | `financial_report_fetcher/rag/ingest.py`、`financial_report_fetcher/rag/qa.py`、`financial_report_fetcher/rag/reranker.py` | `test_rag_*.py` | 见设计台账登记证据 |
| [可信智能问答](#trusted-chat) | 多轮聊天、范围约束、证据链接、会话历史与紧凑来源区 | `webapp/chat_scope.py`、`financial_report_fetcher/rag/qa.py`、`webapp/static/app.js`、`webapp/static/chat_rendering.js` | `test_chat_*.py`、`test_chat_trust_flow.py` | M1 与紧凑证据展示已合入 |
| [行情与 MCP 基本面](#market-mcp) | 实时行情、K 线、基本面/MCP 工具 | `financial_report_fetcher/market/tencent.py`、`financial_report_fetcher/market/mcp_client.py`、`webapp/mcp_guard.py` | `test_market_*.py`、`test_mcp_*.py` | 见设计台账登记证据 |
| [任务与运行可靠性](#runtime) | 分析任务后台执行、取消、恢复和 SSE/轮询 | `webapp/tasks.py`、`task_store.py`、`server.py` | `test_tasks.py`、`test_server_api.py` | 见设计台账登记证据 |

---

<a id="report-download"></a>
## 财报发现、下载与预览

**用户行为**：按股票代码或名称检索可披露报告；下载到本地 `reports/`；Web 可预览 PDF。本地已有有效文件应复用，不重复下载。

| 项目 | 定位信息 |
| --- | --- |
| 核心代码 | `financial_report_fetcher/datasource.py`（报告元数据）、`downloader.py`（下载与落盘）、`report_identity.py`（报告身份）、`webapp/autocomplete.py`、`webapp/server.py` |
| Web/API | 报告列表、PDF 预览及下载相关路由定义在 `webapp/server.py`；启动与配置见 [`docs/startup.md`](startup.md) |
| 关键不变量 | `report_id` 采用 `ticker:真实报告期:report_type`；无法可靠识别报告期时拒绝猜测；下载必须原子落盘，不能以半成品替换有效 PDF |
| 验证 | `tests/unit/test_downloader.py`、`test_models.py`、`test_server_api.py`、`tests/integration/test_download_flow.py` |
| 设计与记录 | [财报分析前端界面](superpowers/specs/2026-08-03-财报分析前端界面-design.md)、[P0 可靠性修复](superpowers/specs/2026-08-30-P0可靠性修复设计.md) |

**变更记录**

- 历史实现已在设计台账中确认；原始合并记录缺失，详见[RAG 知识库与通用问答](superpowers/DESIGN-REGISTRY.md#rag-knowledge-qa)等台账条目。

<a id="trusted-chat"></a>
## 证据化财报分析

**用户行为**：对一份 PDF 生成财务、风险、经营等分析；输出 Markdown/JSON，并为事实、数值和结论保留可核验的 PDF/结构化数据证据。文本不足的页可按需进入 OCR，不阻塞已有结论。

| 项目 | 定位信息 |
| --- | --- |
| 核心代码 | `financial_report_fetcher/analyzer.py`、`analysis_ai.py`、`analysis_pipeline.py`、`analysis_result.py`、`facts.py`、`financial_report_fetcher/evidence/models.py`、`financial_report_fetcher/evidence/resolver.py`、`financial_report_fetcher/evidence/ocr.py`、`financial_report_fetcher/evidence/structured.py` |
| 前端 | `webapp/static/analysis_workflow.js`、`webapp/static/analysis_visualizations.js`、`webapp/static/app.js` |
| 数据契约 | `Evidence`、`FinancialFact`、`ValidationSummary`；当前分析文档为 `schema_version=3`（含可视化时为 `4`），携带 `evidence_catalog` / `evidence_summary` 与 `facts` / `validation`；`schema_version=2` 仅为旧版 `AnalysisReport` 兼容路径，读取时归入 `LegacyAnalysisDocument` |
| 关键不变量 | 数值事实须经过有限值、期间、单位和范围校验；证据冲突不得伪造单一确定结论；OCR 失败时保留已完成内容并标记降级 |
| 验证 | `tests/unit/test_analysis_pipeline.py`、`test_analysis_result.py`、`test_facts.py`、`test_evidence_models.py`、`test_evidence_resolver.py`、`test_ocr_enrichment.py`、`test_structured_data.py` |
| 设计与记录 | [动态证据化财报分析](superpowers/specs/2026-09-01-动态证据化财报分析-design.md)、[财务结构可视化](superpowers/specs/2026-09-10-财务结构可视化-design.md) |

**变更记录**

- 历史实现已在设计台账中确认；原始合并记录缺失，详见[动态证据化财报分析](superpowers/DESIGN-REGISTRY.md#dynamic-evidence-analysis)等台账条目。

<a id="analysis-reading"></a>
## 分析阅读、历史与可视化

**用户行为**：分析过程通过 SSE 渐进展示；快速结论优先，详细主题按证据质量动态出现并排序。历史结果可重开；结构性财务数据以图表/卡片展示；PDF 引用可定位页码。

| 项目 | 定位信息 |
| --- | --- |
| 核心代码 | `webapp/history.py`、`webapp/server.py`、`financial_report_fetcher/visualizations.py` |
| 前端 | `webapp/static/analysis_workflow.js`、`analysis_visualizations.js`、`app.js`、`style.css` |
| 关键不变量 | 主题不能因固定 Tab 占位而展示空内容；结论标签必须反映实际完成/降级状态；PDF 页码链接仅在可信、本地服务路径可用时生成 |
| 验证 | `tests/unit/test_progressive_analysis_ui.py`、`test_analysis_workflow_js.py`、`test_analysis_visualizations_js.py`、`test_visualizations.py`、`tests/browser/test_analysis_tab_priority.py`、`test_financial_structure_visuals.py` |
| 设计与记录 | [渐进式财报报告阅读体验](superpowers/specs/2026-09-08-渐进式财报报告阅读体验-design.md)、[主题 Tab 优先级](superpowers/specs/2026-09-10-分析主题Tab优先级-design.md) |

**变更记录**

- `17e4dab`：主题 Tab 优先级。
- `1107509`：财务结构可视化。

<a id="rag"></a>
## RAG 知识库与检索

**用户行为**：将本地 PDF 及其分析结果摄取到知识库；可按公司、报告期等条件检索并问答；在启用配置时，为分析提供检索上下文；检索质量不足时可选本地 reranker 精排。

| 项目 | 定位信息 |
| --- | --- |
| 核心代码 | `financial_report_fetcher/rag/chunking.py`、`financial_report_fetcher/rag/embedding.py`、`financial_report_fetcher/rag/ingest.py`、`financial_report_fetcher/rag/store.py`、`financial_report_fetcher/rag/qa.py`、`financial_report_fetcher/rag/analysis.py`、`financial_report_fetcher/rag/reranker.py`、`financial_report_fetcher/rag/web_search.py` |
| CLI/API | CLI 入口在 `financial_report_fetcher/__main__.py`；Web RAG/聊天路由在 `webapp/server.py` |
| 关键不变量 | `report_id` 过滤必须贯穿检索；索引缺失或检索为空时诚实降级，不能补造引用；重排序不可用时回退向量检索而不改变接口结构 |
| 验证 | `tests/unit/test_rag_chunking.py`、`test_rag_embedding.py`、`test_rag_ingest.py`、`test_rag_store.py`、`test_rag_qa.py`、`test_rag_analysis.py`、`test_rag_reranker.py`、`test_web_search.py` |
| 设计与记录 | [RAG 知识库与通用问答](superpowers/specs/2026-08-17-RAG知识库与通用问答设计.md)、[RAG 增强多维度分析](superpowers/specs/2026-08-18-RAG增强多维度分析设计.md) |

<a id="trusted-chat"></a>
## 可信智能问答

**用户行为**：从财报或聊天页发起多轮流式问答；可新建/重开会话、停止生成；回答显示范围、运行状态和紧凑的“证据与来源”折叠区。PDF 可跳页，网页可打开来源，实时数据保留来源与数据截至时间。

| 项目 | 定位信息 |
| --- | --- |
| 核心代码 | `webapp/chat_models.py`（运行/证据契约）、`webapp/chat_scope.py`（范围解析）、`webapp/chat_evidence.py`（证据标准化）、`webapp/chat_store.py`（持久化）、`webapp/server.py`（SSE/API）、`financial_report_fetcher/rag/qa.py`（范围过滤与生成） |
| 前端 | `webapp/static/app.js`（会话、范围栏和流消费）、`chat_rendering.js`（回答/证据渲染）、`style.css` |
| API/数据契约 | `POST /api/chat/stream`；`AnswerRun` 持久化 `scope`、`artifacts`、`tool_artifacts` 与运行状态；旧答案可能不含证据包 |
| 关键不变量 | `company_only` 是硬 `report_id` 边界，不能静默扩展到跨公司/跨期间；仅显式同业/比较问题才可扩至 `company_industry`，并标注“本地可检索同业样本”；PDF 事实必须有页码证据；外部事实必须保留来源与 `as_of` |
| 证据呈现规则 | 默认收起、无摘要/片段/工具参数结果；同 PDF 同页、同网页 URL、同来源同 `as_of` 的实时数据合并；单 PDF 超过 3 个不同页码仅保留首页链接；无可信 PDF URL 时只显示不可用状态，绝不伪造跳转 |
| 验证 | `tests/unit/test_chat_models.py`、`tests/unit/test_chat_scope.py`、`tests/unit/test_chat_evidence.py`、`tests/unit/test_chat_store.py`、`tests/unit/test_chat_rendering_js.py`、`tests/unit/test_server_api.py`、`tests/browser/test_chat_trust_flow.py` |
| 设计与记录 | [可信 Agent 设计](superpowers/specs/2026-09-10-智能问答可信Agent升级-design.md)、[M1 计划](superpowers/plans/2026-09-10-智能问答可信Agent-M1.md) |

**变更记录**

- `6276701`：可信问答 M1，加入范围、证据包、运行状态和持久化。
- `60f281c`：紧凑证据与来源展示；新增折叠、去重、三页阈值和无头浏览器回归。
- `b1cd505`：过滤模型误写进正文的 DSML 工具调用标记，保留其余回答文字。
- `9e9f2ff`：兼容真实提供方的双分隔符 `｜｜DSML｜｜` 格式，并以现场结构回归覆盖。
- `64d62b9`：复用 `AnswerRun.id` 作为可复制诊断 ID，并以该 ID 关联问答开始/终态日志。
- `55c3189`：诊断失败日志仅保留 `run_id`、状态和异常类型，不记录问题正文或 traceback。
- `9e8abed`：流式异常响应改为安全提示与诊断 ID；旧单报告问答回退日志补充报告代码与期间。
- 后续可信 Agent M2–M4 尚未实现；状态以[设计台账](superpowers/DESIGN-REGISTRY.md#trusted-chat-agent)为准。

<a id="market-mcp"></a>
## 行情与 MCP 基本面

**用户行为**：查看实时行情、指数、K 线；调用受控的股票基本面和市场数据工具。RAG 问答可在配置允许时使用 MCP 工具，并显示其运行证据。

| 项目 | 定位信息 |
| --- | --- |
| 核心代码 | `financial_report_fetcher/market/tencent.py`、`financial_report_fetcher/market/mcp_client.py`、`financial_report_fetcher/rag/mcp_tools.py`、`webapp/mcp_guard.py` |
| API/CLI | Web 端 `/api/quote`、`/api/quote/kline`、`/api/quote/index`、`/api/stock/*`、`/api/stock/mcp/call`；CLI 的 `quote`、`mcp` 子命令 |
| 关键不变量 | MCP 仅执行配置白名单内工具；失败遵循熔断/冷却语义；外部工具文本在可信问答中只能作为 `reference`，不能自动成为未核验事实 |
| 验证 | `tests/unit/test_market_api.py`、`test_market_tencent.py`、`test_market_mcp_client.py`、`test_mcp_client.py`、`test_mcp_guard.py`、`test_mcp_tools.py` |
| 设计与记录 | [问答接入 MCP 工具](superpowers/specs/2026-08-19-问答接入MCP工具设计.md)、[股票相关能力调研](stock-capabilities.md) |

**变更记录**

- 历史实现已在设计台账中确认（[问答接入 MCP 工具](superpowers/DESIGN-REGISTRY.md#chat-mcp-tools)）；原始合并记录缺失。

<a id="runtime"></a>
## 任务与运行可靠性

**用户行为**：Web 分析在后台运行，可查看进度、取消、重试并在刷新/短暂断线后恢复；任务和聊天会话各自持久化。

| 项目 | 定位信息 |
| --- | --- |
| 核心代码 | `webapp/tasks.py`、`task_store.py`、`server.py`、`browser_preflight.py`；聊天持久化见 `chat_store.py` |
| 关键不变量 | 合法状态转换受控；未知任务不得写入；服务重启后遗留运行任务需要诚实转为可重试失败；当前多进程 ownership/lease 尚未实现，生产部署必须保持单服务进程/单 TaskManager |
| 验证 | `tests/unit/test_tasks.py`、`test_server_api.py`、`test_service_scripts.py`、`test_browser_test_safety.py` |
| 设计与记录 | [P0 可靠性修复](superpowers/specs/2026-08-30-P0可靠性修复设计.md)、[改进路线图](IMPROVEMENT_ROADMAP.md) |

**变更记录**

- 历史实现已在设计台账中确认（[P0 可靠性修复](superpowers/DESIGN-REGISTRY.md#p0-reliability)）；原始合并记录缺失。

## 维护规则

- 影响用户可见功能、API 契约、数据模型、实现入口、不变量或测试入口的合并，必须更新对应功能条目。
- 影响用户可见的功能列表、使用方式、配置或启动方式的变更，必须同时更新 `README.md`（详见 `AGENTS.md`）。
- “最近变更”只记录**已合入 `main`** 的提交：短 SHA、改动目的和必要的兼容性说明；临时调试、重格式化和未合入分支不登记。历史功能无可核对合并记录时，写“见设计台账登记证据”并链向台账条目，不伪造 SHA。
- 新功能先在 [设计台账](superpowers/DESIGN-REGISTRY.md)登记设计状态；合入后再在本目录新增或扩展功能条目。
- 删除或替换功能时，保留最近变更记录，并明确迁移入口；不要让旧路径静默失效。
- 每次修改功能前，先阅读本目录对应条目和其列出的关键不变量；若文档与代码不一致，先以代码/测试修正文档，再继续迭代。
