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
| [可信智能问答](#trusted-chat) | 多轮聊天、受限研究计划、研究工作台、研究纪要导出、离线质量观测与运行级评测、检索证据窗口与上下文预算、冻结范围与时间、运行级来源授权/预算/去重、证据持久化与诚实失败状态、数字/期间核验、来源链接、短期行情正文表格、多指数相对走势与个股原价折线图、上下文展示追问及 WebSearch 查询词溯源、会话历史与授权补充财报、受限未检索常识回答、异步执行/进度/协作取消 | `webapp/chat_scope.py`、`chat_policy.py`、`chat_facts.py`、`chat_verifier.py`、`webapp/chat_followups.py`、`chat_request_resolution.py`、`execution_plan.py`、`execution_planner.py`、`webapp/chat_runs.py`、`webapp/source_runtime.py`、`source_adapters.py`、`chat_execution.py`、`webapp/execution_executor.py`、`webapp/evidence_identity.py`、`webapp/research_*.py`、`webapp/chat_evaluation.py`、`webapp/chat_runtime_eval_cases.py`、`webapp/chat_runtime_eval_runner.py`、`webapp/chat_runtime_eval_report.py`、`webapp/chat_retrieval_window.py`、`webapp/chat_context_budget.py`、`scripts/run_chat_evaluation.py`、`scripts/run_chat_runtime_evaluation.py`、`scripts/probe_chat_live.py`、`financial_report_fetcher/rag/qa.py`、`webapp/static/app.js`、`webapp/static/chat_rendering.js`、`webapp/static/chat_market_charts.js`、`webapp/static/style.css` | `test_chat_*.py`、`test_chat_runs.py`、`test_execution_executor.py`、`test_source_runtime.py`、`test_source_adapters.py`、`test_rag_context.py`、`test_research_*.py`、`test_evidence_identity.py`、`test_rag_policy_m2.py`、`test_chat_source_runtime.py`、`test_chat_reliability_p2.py`、`test_chat_reliability_p3.py`、`test_chat_runtime_eval_*.py`、`test_chat_retrieval_window.py`、`test_chat_context_budget.py`、`test_chat_live_probe.py`、`test_chat_trust_flow.py`、`test_research_agent_flow.py`、`test_research_workspace_flow.py` | `4d79eb8`：短期行情正文表格；`03df820`：第四阶段运行级离线评测、检索证据窗口与上下文预算；第三阶段异步执行见 `ee3c3d6`；`1188365`：多指数行情图表 |
| [行情与 MCP 基本面](#market-mcp) | 实时行情、K 线、基本面/MCP 工具与 A 股日/周复盘 | `financial_report_fetcher/market/tencent.py`、`financial_report_fetcher/market/mcp_client.py`、`financial_report_fetcher/rag/mcp_tools.py`、`webapp/execution_executor.py`、`webapp/mcp_guard.py` | `test_market_*.py`、`test_mcp_*.py`、`test_execution_*recap.py` | `61135a2`：受控 A 股复盘市场概览 |
| [投资资讯](#investment-news) | 本地市场与最多 20 只共享自选股资讯、原文回链、定时任务窗口自动刷新、分组筛选/排序（市场资讯无个股筛选；自选股资讯限已添加股票）、按日回看的 AI 综述 | `webapp/investment_news_store.py`、`investment_news_sources.py`、`investment_news.py`、`investment_news_scheduler.py`、`webapp/server.py`、`webapp/static/investment_news_polling.js`、`webapp/static/investment_news.js` | `test_investment_news_*.py`、`test_investment_news_polling_js.py`、`test_server_api.py`、`test_investment_news_status_ui.py`、browser QA fixture `investment_news_test_app.py` | `c1899a4`：定时运行按时窗轮询并自动刷新，修正筛选/排序布局；`956728a`：综述日期入口置顶、分开市场/自选股快捷日期，并服务端限制自选股要点引用范围；`21b3d2a`：采集后定位最新资讯日、自选股日期区间与历史综述回看；`53fc9e5`：突出显示最近成功运行时间并自动隐藏终态提示；`7df28fa`：单机投资资讯与独立综述流程 |
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

**用户行为**：从财报或聊天页发起多轮流式问答；复杂研究任务会展示受限计划和实时步骤状态，可停止、重开并仅恢复未完成步骤；普通问答可新建/重开会话、停止生成；有界后台 worker 执行规划/取数，SSE 首帧提供运行身份和进度，支持协作取消与整轮超时。回答显示范围、运行状态和紧凑的“证据与来源”折叠区。PDF 可跳页，网页可打开来源，实时数据保留来源与数据截至时间；外部事实默认收起、按来源归组，展开后以表格显示对象/指标/期间/数值与单位，每组先显示 20 条。个股与大盘行情问题支持通用期限解析和短/中/长期默认窗口；短期行情在答案正文展示逐日收盘、相邻采样涨跌幅和区间变化，证据仍保留在来源区，冲突/单点不生成趋势；大盘复盘在多指数共享至少两个采样日期时展示相对涨跌图，个股至少两个合格日期采样时展示原始收盘价图，并保留收盘点位表和来源证据；上一轮图表/来源展示追问使用有界会话上下文且跳过检索，市场因果问题仍可搜索，来源详情展示安全转义后的实际搜索词；长区间按覆盖选择日/周/月 K 线，大盘中长期只描述主要指数并披露缺少同期全市场广度，覆盖不足时标记为部分完成。单公司问题缺少必要原文时，系统最多列出 5 份候选财报，用户明确选择并授权后才下载、索引并恢复原问题；重开会话可查看授权与补充结果。研究工作台按持久化的公司、行业、报告期、意图、状态和收藏筛选研究资产，支持收藏、编辑重问、分支追问和导出研究纪要；工作台显示最近一次本地离线质量评测的健康与负向探针状态。

| 项目 | 定位信息 |
| --- | --- |
| 核心代码 | `webapp/chat_models.py`（运行/证据契约）、`webapp/chat_scope.py`（范围解析）、`webapp/chat_policy.py`（意图与工具策略）、`webapp/chat_time.py`（上海时区及通用行情期限窗口解析）、`webapp/chat_runs.py`（有界 worker/事件通道/超时/取消）、`webapp/source_runtime.py`（运行级授权、调用预算与去重）、`webapp/source_adapters.py`（来源规范化）、`webapp/chat_execution.py`（证据投影与终态）、`webapp/execution_executor.py`（计划步骤/进度）、`webapp/chat_facts.py`（事实归一与冲突）、`webapp/chat_verifier.py`（论断核验）、`webapp/chat_evidence.py`（证据标准化）、`webapp/chat_store.py`（持久化）、`webapp/chat_supplement.py`（一次性授权与受控下载）、`webapp/evidence_identity.py`（证据身份与受信页码规则）、`webapp/research_workspace.py`、`research_export.py`（工作台/导出）、`webapp/chat_evaluation.py` 与 `scripts/run_chat_evaluation.py`（固定产物回放）、`webapp/chat_runtime_eval_*.py` 与 `scripts/run_chat_runtime_evaluation.py`（运行级离线评测）、`webapp/chat_context_budget.py` 与 `webapp/chat_retrieval_window.py`（上下文预算和证据窗口）、`webapp/server.py`（SSE/API）、`financial_report_fetcher/rag/qa.py`（范围过滤、策略门控与上下文生成）、`financial_report_fetcher/rag/web_search.py`（出站查询规范化）、行情答案、个股/多指数图表和查询词详情由 `webapp/static/chat_rendering.js`、`chat_market_charts.js` 与 `style.css` 管理 Chart.js 实例 |
| 前端 | `webapp/static/app.js`（会话、范围栏和流消费）、`chat_rendering.js`（回答/证据渲染）、`chat_market_charts.js`（个股/多指数图表配置与生命周期）、`style.css` |
| API/数据契约 | `POST /api/chat/stream`；`POST /api/chat/runs/{run_id}/cancel`；`POST /api/chat/supplements/{supplement_id}/resolve`；`POST /api/chat/research/{run_id}/resume`；`GET /api/research/workspace`、`PATCH /api/research/runs/{run_id}/favorite`、`GET /api/research/runs/{run_id}/export`、`GET /api/research/quality`；本地质量命令 `scripts/run_chat_evaluation.py` 仍只回放固定 AnswerRun fixture；运行级离线评测另用 `scripts/run_chat_runtime_evaluation.py --cases tests/fixtures/chat_runtime_eval_cases.json --output <isolated-json>`；真实提供方入口 `scripts/probe_chat_live.py` 仅 dry-run 校验、不联网；`AnswerRun` 持久化 `scope`、`intent_decision`、`tool_policy`、`facts`、`conflicts`、`verification_report`、`artifacts`、`tool_artifacts`、补充摘要与运行状态；研究运行另持久化受限计划和步骤状态；工作台与质量侧车（`data/research_workspace.json`、`data/research_quality_summary.json`）不入库；旧答案可能不含证据包；执行计划 WebSearch query 最长 300 字，安全摘要写入 `ToolArtifact.arguments_summary` |
| 关键不变量 | worker 并发、事件缓冲和整轮期限均有上限；协作取消不强杀在途同步调用，超时/取消后不再启动下一来源；`company_only` 是硬 `report_id` 与外部工具身份边界，不能静默扩展到跨公司/跨期间；仅显式同业/比较问题才可扩至 `company_industry`，并标注“本地可检索同业样本”；工具只能来自意图策略允许集，网页查询/行情代码均绑定冻结 Scope；外部调用按实际尝试计入同一运行预算并去重，禁止执行计划和生成阶段重复自主取数；来源数据时间与获取时间分开保存，失败/未知时间不能伪装成成功；required 来源缺失时停止生成确定性结论，来源失败必须影响终态；研究计划不得扩权，步骤产物须立即持久化，恢复只重跑未完成步骤且复用原问题与冻结策略；工作台筛选只依赖持久化元数据，收藏/导出/记忆只引用不可变运行与证据，删除会话保留独立记忆并披露数量；仅用户显式保存且已核验的 PDF 事实（正页码证据）、可用证据或含证据的决策可入记忆，`reference/conflict/unavailable` 与 stopped 残片一律拒绝，记忆读接口对异常侧车 fail-closed；质量端点只读预计算 health/probe 汇总且仅返回白名单 failure 码与计数，不回传原始用例、prompt 或失败文本；稳定 Fact ID 用于证据身份引用；缺少期间、口径、主体、单位或受控来源/时间的事实不得进入确定性结论，冲突必须披露；补充下载仅在同公司原问题获得一次性明确授权后执行，候选与选择均不超过 5，跨会话/跨问题/重放一律拒绝；仅 `source="pdf"` 索引成功的报告可进入恢复范围；短期行情表格只从同一对象的 market Fact 生成，至少两个一致日期采样才计算变化，不用价格序列证明涨跌原因；多指数图表仅使用当前 AnswerRun 的可信共同日期事实，至少两个指数且至少两个共同采样点，按首个共同点归一至 0%，冲突/混周期系列排除、不插值、不新增行情请求，图表失败时隐藏空 canvas 并保留表格与来源证据；个股图仅使用当前 run 中与冻结范围相符的合格 security price Facts，至少两个有效日期，纵轴保留原收盘价且 tooltip 明确显示较首点变化。仅上一轮展示/来源追问走单步上下文回答并禁用检索和外部工具；其他问题仍按本轮意图规划，实际出站 WebSearch query 经限长/脱敏后按调用持久化并在来源详情安全转义展示 |
| 证据呈现规则 | 外部事实与来源默认收起，不显示原始摘要、片段或工具参数；按 provider/tool/数据时间/状态/实际搜索词归组，WebSearch query 仅以安全摘要持久化并以转义纯文本展示；利用 `source_id`/`evidence_ids` 关联事实，数据时间或状态不同不合并，无法匹配时明确标为未关联；结构化表格呈现对象、指标、期间、数值与单位，每组先显示 20 条，其余原生折叠查看更多；同 PDF 同页、同网页 URL 去重；单 PDF 超过 3 个不同页码仅保留首页链接；无可信 PDF URL 时只显示不可用状态，绝不伪造跳转 |
| 验证 | `tests/unit/test_chat_models.py`、`test_chat_scope.py`、`test_chat_policy.py`、`test_chat_facts.py`、`test_chat_verifier.py`、`test_source_runtime.py`、`test_source_adapters.py`、`test_rag_context.py`、`test_rag_policy_m2.py`、`test_chat_policy_eval_cases.py`、`test_chat_evidence.py`、`test_chat_store.py`、`test_chat_supplement.py`、`test_chat_supplement_ui.py`、`test_chat_rendering_js.py`、`test_server_api.py`、`tests/browser/test_chat_source_runtime.py`、`tests/browser/test_chat_reliability_p2.py`、`tests/browser/test_chat_reliability_p3.py`、`tests/browser/test_chat_trust_flow.py`、`tests/unit/test_chat_runtime_eval_cases.py`、`test_chat_runtime_eval_runner.py`、`test_chat_runtime_eval_report.py`、`test_chat_retrieval_window.py`、`test_chat_context_budget.py`、`test_chat_live_probe.py`、`test_chat_policy_flow.py`、`test_chat_pdf_supplement.py`、`test_chat_horizon_matrix.py`、`test_chat_market_charts_js.py`、`test_chat_followups.py`、`test_execution_plan.py`、`test_execution_planner.py` |
| 设计与记录 | [可信 Agent 设计](superpowers/specs/2026-09-10-智能问答可信Agent升级-design.md)、[M1 计划](superpowers/plans/2026-09-10-智能问答可信Agent-M1.md)、[问答可靠性优化总设计](superpowers/specs/2026-09-24-chat-reliability.md)、[第一阶段实施计划](superpowers/plans/2026-09-24-chat-p1.md)、[第二阶段设计](superpowers/specs/chat-reliability-p2.md)与[计划](superpowers/plans/2026-09-24-chat-p2.md)、[第三阶段设计](superpowers/specs/chat-reliability-p3.md)与[计划](superpowers/plans/2026-10-08-chat-p3.md)、[第四阶段设计](superpowers/specs/chat-reliability-p4.md)与[计划](superpowers/plans/2026-10-08-chat-p4.md)、[市场链路修复设计](superpowers/specs/2026-10-09-chat-market-routing-repair-design.md)与[计划](superpowers/plans/2026-10-09-chat-market-routing-repair.md)、[通用期限行情设计](superpowers/specs/2026-10-09-individual-and-market-horizon-analysis-design.md)与[计划](superpowers/plans/2026-10-09-generic-market-horizon-analysis.md)、[外部参考展示设计](superpowers/specs/2026-10-12-ref-display.md)与[计划](superpowers/plans/2026-10-12-ref-display.md)、[短期行情答案设计](superpowers/specs/2026-10-12-short-market-answer.md)与[计划](superpowers/plans/2026-10-12-short-market-answer.md)、[多指数行情图表设计](superpowers/specs/2026-10-12-multi-index-market-chart-design.md)与[计划](superpowers/plans/2026-10-12-multi-index-market-chart.md)、[个股价格图表设计](superpowers/specs/2026-10-12-individual-stock-price-chart-design.md)与[计划](superpowers/plans/2026-10-12-individual-stock-price-chart.md)、[上下文追问与搜索溯源设计](superpowers/specs/2026-10-12-context-aware-followup-search-audit-design.md)与[计划](superpowers/plans/2026-10-12-context-aware-followup-search-audit.md) |

**变更记录**

- `6276701`：可信问答 M1，加入范围、证据包、运行状态和持久化。
- `60f281c`：紧凑证据与来源展示；新增折叠、去重、三页阈值和无头浏览器回归。
- `b1cd505`：过滤模型误写进正文的 DSML 工具调用标记，保留其余回答文字。
- `9e9f2ff`：兼容真实提供方的双分隔符 `｜｜DSML｜｜` 格式，并以现场结构回归覆盖。
- `64d62b9`：复用 `AnswerRun.id` 作为可复制诊断 ID，并以该 ID 关联问答开始/终态日志。
- `55c3189`：诊断失败日志仅保留 `run_id`、状态和异常类型，不记录问题正文或 traceback。
- `9e8abed`：流式异常响应改为安全提示与诊断 ID；旧单报告问答回退日志补充报告代码与期间。
- `1132bc6`：单公司问答缺少原文时提供最多 5 份可选候选；仅经一次性授权后下载并以 PDF 索引证据恢复原问题，持久化补充摘要并在重开会话展示；兼容性要求为单进程部署，补充登记表不跨进程共享。
- `3c64060`：可信问答 M2，增加受控意图与工具策略、范围绑定的实时/网页查询、事实归一/冲突和确定性论断核验；外部数据仅作带来源与时间的参考，评测通过真实 RAG 策略门控。
- `dca62e4`：可信问答 M3，研究任务展示受限计划和流式步骤状态；真实 RAG 产物逐步持久化，支持停止、历史重开和仅重跑未完成步骤的安全恢复。
- `7d89a7e`：可信问答 M4，新增研究工作台（元数据筛选、收藏、导出、编辑重问与分支追问）、可复核纪要导出与离线金融问答评测门槛。
- `e0dcde6`：可信问答 M4 P2，新增稳定 Fact ID 与统一后端证据身份规则；质量评测分离健康集与负向探针，使用显式本地命令原子生成仅含安全聚合的摘要，工作台显示质量状态。
- `f9472e6`：移除研究记忆与研究决策；删除保存证据/决策、记忆 API/UI/侧车及会话删除保留计数。兼容性：历史 `data/research_memory.json` 在启动时被精确删除，不再提供迁移、导出或只读入口。
- `c1c9a78`：智能问答可靠性第一阶段；运行级来源授权、调用预算与去重贯通计划执行、RagQA 上下文生成和 SSE 持久化；UI 保留成功/部分/失败来源及分离的数据时间/获取时间。兼容边界：旧 CLI `answer_stream` 默认调用不变；不新增数据源；数字/时间口径与异步/取消仍属后续阶段。
- `d197b1d`：智能问答可靠性第二阶段；公司身份与本地报告解耦，未知公司/时间先澄清；意图及上海时区市场窗口确定性冻结；复盘只披露实际返回日期与来源覆盖，50 条池截断不称市场总量；数字按主体、单位、期间和证据类别核验，派生计算保留证据；稳定常识允许明确标记的无检索单步回答。复盘市场调用≤7、总调用≤9、网页调用≤2，仍受更低配置和来源授权限制。

- `ee3c3d6`：智能问答可靠性第三阶段；后台 worker 并发上限 4、每轮事件缓冲 64，普通/研究超时默认 120/300 秒；提供步骤进度、协作取消和来源调用门控，显式研究意图固定进入 ResearchAgent。取消不会强杀在途同步调用，之后不再启动新来源。
- `03df820`：智能问答可靠性第四阶段；新增 9 场景真实 SSE 离线运行评测和脱敏对照报告；相关证据窗口使目标引用支持由 0/1 提升至 1/1，长历史预算比较降低约 36% 字符估计且来源调用数不变。真实提供方探测入口仅 dry-run，不访问生产环境。
- `91a34d8`：流式答案透传模型 `finish_reason`，对长度终止、明显断尾及分析表格缺少结论执行最多两次同证据续写（续写禁用工具并限制输出）；续写耗尽时，行情仅用已验证的同主体日 K 事实生成完整短结论，其余回答以 `partial` 提示失败，不把残片标为完成。
- `bcf854c`：行情回答数字校验失败时，若有同主体、同窗口的行情事实，回退生成并重新核验确定性短答；区分答案校验失败与来源覆盖不足，并将安全 issue code 关联诊断 ID。
- `fd7d38a`：补全重试耗尽时也复用已取得并核验的行情事实生成短答，不再提前返回通用重试提示；无可用事实时使用行情专属提示。聚焦回归 286 passed；原 query `分析福耀玻璃近期行情` 本地端到端返回 completed、verification passed、5 条 K 线事实。
- `536043d`：在同一 fast-forward 中合入市场路由修复分支历史提交及个股/大盘通用行情期限、模糊短中长期默认值、日/周/月 K 线适配；大盘广度缺失时输出已核验的指数 fallback。聚焦回归 296 passed；Q1–Q15 本地接口验证已记录，覆盖不足按 partial 呈现。
- `4d79eb8`：短期个股/大盘答案正文展示逐日收盘、相邻采样涨跌和区间变化，避免无请求的中长期警示；单点/冲突事实不推导趋势。聚焦单测 125 passed，含行情表格的 desktop/tablet/mobile 布局回归 3 passed；browser QA 三视口无控制台/页面错误，仅 favicon.ico 404。
- `070002f`：外部参考事实改为默认折叠并按来源归组；通过证据 ID 绑定来源，以结构化表格显示数据，每组 20 条后可展开查看更多；保留不同数据时间/状态、冲突提示及安全链接。渲染与台账单测 43 passed，来源失败场景浏览器回归 1 passed。
- `1188365`：大盘复盘在共享日期充足时增加按共同首日归一到 0% 的多指数折线图；tooltip 同时提供原始点位和区间变化，颜色之外以线型/点型区分序列，图表实例随聊天 DOM 清理；原有点位表和来源证据保持不变。聚焦 unit tests 43 passed，行情布局与多指数浏览器回归 6 passed；browser QA desktop/tablet/mobile 无 console/page errors，仅 favicon.ico 404。
- `c1d2e99`：新增个股原价图（仅同范围、合格的当前 run 行情 Facts，tooltip 显示较首采样变化，保留点位表）；上一轮展示追问复用有界安全上下文且跳过检索，因果追问仍可 WebSearch；实际 WebSearch query 经限长/脱敏后写入来源证据并在折叠来源详情安全展示。聚焦单测 426 passed，相关浏览器回归 6 passed；Browser QA desktop/tablet/mobile 无 console/page errors，仅 favicon.ico 404。

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
- `61135a2`：A 股日/周复盘新增 `market_overview`，服务端以固定时间窗口聚合指数、涨跌停池与行业资金流 MCP 产物；模型不能传递工具名、日期或参数，复盘默认不使用 RAG。

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

<a id="investment-news"></a>
## 投资资讯

**用户行为**：查看本地已采集的 A 股市场和最多 20 只共享自选股资讯并跳转原文；市场资讯可切换今天/昨天但不支持按股票筛选（市场快讯不含个股关联）；自选股资讯可切换今日/近 7 天/近 30 天并按已添加的共享自选股筛选；页面上方可独立按日期回看每日 AI 综述。

| 项目 | 定位信息 |
| --- | --- |
| 核心代码 | `webapp/investment_news_sources.py`（Eastmoney/AKShare 字段规范化）、`webapp/investment_news_store.py`（SQLite、分页、保留、指纹与最近成功运行查询）、`webapp/investment_news.py`（运行/采集/摘要协调）、`webapp/investment_news_scheduler.py`（Asia/Shanghai 调度）、`webapp/server.py`（API/生命周期/静态资源版本）、`webapp/static/investment_news_polling.js`（Asia/Shanghai 自动任务监听窗口） |
| 前端与 API | `webapp/static/investment_news.js`；`GET /api/investment-news`（单日 `day` 或包含端点的 `start_day`/`end_day`）、`/watchlist`、`/digest?day=...`、`/runs`、`/status`；`POST /collect`、`/digest`、`/watchlist`；`DELETE /watchlist/{code}` |
| 关键不变量 | 仅限用户选定的本机个人非商用范围；Eastmoney/AKShare 可用不证明授权、SLA 或覆盖。手动采集不调 AI、手动摘要不采集；自动采集每日 09:00–16:00 整点，自动摘要仅 09:00/16:00 且等待当时采集完成。最多 20 只共享自选股；市场只提供今天/昨天，个人自选股只提供今天及含当天计算的 7/30 日历天区间。摘要日期选择与资讯日期筛选相互独立；综述按日期保存，每日仅保留最新一份。自动综述成功后，页面日期选择与展示内容同步切换至最新成功综述日期；用户手动回看历史日期不受影响。自选股重点只允许引用当前自选股关联的 `kind=watchlist` 条目，服务端在分批生成和最终合并阶段都执行范围校验。个股筛选只对自选股资讯生效，范围限定为当前共享自选股；市场快讯没有个股关联，不能按股票筛选，界面以禁用样式和说明明确告知。前端只在每小时自动任务前后各 5 分钟检查状态，间隔 30 秒；发现活跃任务/完成后持续跟踪，页面隐藏时不请求，返回前台立即检查。筛选范围/日期控件与股票筛选/排序采用明确分组行，避免响应式 flex 自动重排。页面每页最多 20 条不限制收集和摘要输入。资讯片段与内容型日综述最多保留 90 天，运行状态元数据与摘要内容分开保存。页面持续展示最近一次成功采集与综述时间；终态操作提示短暂展示后清空，运行中进度保持可见。来源失败不清空旧数据；摘要要点须引用有效条目；来源时间未知不得伪装为新鲜；同题转载不表示独立佐证；不提供投资建议。 |
| 验证 | `tests/unit/test_investment_news_store.py`、`test_investment_news_sources.py`、`test_investment_news.py`、`test_investment_news_digest.py`、`test_investment_news_scheduler.py`、`test_investment_news_api.py`、`test_investment_news_ui.py`、`test_investment_news_polling_js.py`、`tests/unit/test_server_api.py`、`tests/browser/test_investment_news_status_ui.py`；浏览器仅使用 `tests/browser/investment_news_test_app.py` 假来源/假 AI |
| 设计与记录 | [投资资讯页设计](superpowers/specs/2026-10-14-investment-news-page-design.md)、[实施计划](superpowers/plans/2026-10-14-investment-news-page.md) |

**变更记录**

- `7df28fa`：新增本机 A 股投资资讯页、Eastmoney/AKShare 来源规范化、90 天 SQLite 保留、最多 20 只共享自选股及 09/16 自动 AI 综述；采集与摘要可分开触发，摘要要点保留可验证原文链接。浏览器/测试只使用本地 fake source 与 fake AI。当前来源选择不代表 Eastmoney 许可，适用范围仅本机个人非商用。
- `53fc9e5`：页面长期突出显示最近成功采集/综述的本地时间，成功、失败等终态操作提示 5 秒后自动隐藏，运行中进度保持可见；不自动推断数据过期或要求手动触发。
- `21b3d2a`：采集成功/部分成功后日期自动切至最新资讯所属日；自选股资讯增加按所选结束日计算的近 7/30 天筛选；切换日期同步查看当日综述，综述保留每日最新一份。
- `956728a`：综述日期选择器置于页面上方且独立于资讯日期；市场资讯快捷切换今天/昨天，自选股快捷切换今日/近 7/30 天；服务端拒绝将市场或非当前自选股资讯用于“自选股重点”。
- `c1899a4`：前端仅在 Asia/Shanghai 每小时任务前后 5 分钟、每 30 秒检查自动运行状态；发现活跃任务持续跟踪，任务完成后更新列表/综述/状态，页面隐藏时暂停并在回到前台时检查。资讯范围与日期、股票筛选与排序改为明确分组行布局。
- `541c137`：市场资讯的股票筛选显式禁用并说明原因；切换到自选股资讯后启用并限定于已添加自选股，补充无障碍描述与浏览器回归测试。
- `6c2a532`：自动综述成功后，日期选择器与综述内容自动切换到最新完成的综述日期；保留用户主动回看历史日期的行为。补充真实浏览器回归覆盖自动任务完成后的刷新。

## 维护规则

- 影响用户可见功能、API 契约、数据模型、实现入口、不变量或测试入口的合并，必须更新对应功能条目。
- 影响用户可见的功能列表、使用方式、配置或启动方式的变更，必须同时更新 `README.md`（详见 `AGENTS.md`）。
- “最近变更”只记录**已合入 `main`** 的提交：短 SHA、改动目的和必要的兼容性说明；临时调试、重格式化和未合入分支不登记。历史功能无可核对合并记录时，写“见设计台账登记证据”并链向台账条目，不伪造 SHA。
- 新功能先在 [设计台账](superpowers/DESIGN-REGISTRY.md)登记设计状态；合入后再在本目录新增或扩展功能条目。
- 删除或替换功能时，保留最近变更记录，并明确迁移入口；不要让旧路径静默失效。
- 每次修改功能前，先阅读本目录对应条目和其列出的关键不变量；若文档与代码不一致，先以代码/测试修正文档，再继续迭代。
