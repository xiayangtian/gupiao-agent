# 方案设计台账

本台账是已完成方案设计的唯一执行状态源。优先查看“未完成项目”；每条均可跳转到完整主记录，再查看其历史设计和实施计划。

状态依据为人工核对的方案文档、进度记录和已合入 `main` 的验证证据；不以文件名相似、目录命名或推测性提交信息判断完成状态。判定口径：

- **待实现**：设计已完成，未找到已验证并合入 `main` 的实现证据。
- **实施中**：存在实施分支或进度记录，尚未完成验证并合入 `main`。
- **已实现**：已合入 `main`，且已记录合并提交短 SHA 与实际验证结果。
- **待核实**：历史记录不足（多数旧方案由 `main` 直接提交落地，缺少合并提交记录），无法据此断言为待实现或已实现；此时证据栏写明已核对到的交付物，供人工裁决。

可执行性取值：`可直接执行`（待实现且实施计划已存在）、`先补计划`（只有设计文档，没有实施计划）、`—`（无需从本台账直接启动）。

## 未完成项目

“待核实”表示缺少可验证的合并记录，并不等于未实现；请打开其主记录查看设计、实施计划和证据。这里仅保留主记录链接，完整字段只在“全部设计”维护。

方案 | 状态 | 可执行性 | 方案入口
--- | --- | --- | ---
| 智能问答可信 Agent 升级 | 实施中 | — | [主记录](#trusted-chat-agent) |

## 全部设计

主记录保存每个设计的完整字段；新增设计在此追加一行，并同步更新上方未完成索引。核对日期统一为 2026-09-14。

方案 | 设计文档 | 实施计划 | 实现状态 | 可执行性 | 实现证据 | 更新日期
--- | --- | --- | --- | --- | --- | ---
| <a id="financial-report-ui"></a>财报分析前端界面 | [设计](specs/2026-08-03-财报分析前端界面-design.md) | [计划](plans/2026-08-03-财报分析前端界面.md) | 已实现 | — | 用户确认 2026-09-14：历史需求已完成；原合并记录缺失；交付物已在 main：webapp/server.py、autocomplete.py、tasks.py 与静态前端；2026-09-14 回归 tests/unit/test_server_api.py、test_autocomplete.py 共 108 通过；未找到合并提交记录 | 2026-09-14 |
| <a id="analysis-history-charts"></a>分析历史与图表 | [设计](specs/2026-08-03-分析历史与图表-design.md) | [计划](plans/2026-08-03-分析历史与图表.md) | 已实现 | — | 用户确认 2026-09-14：历史需求已完成；原合并记录缺失；交付物已在 main：webapp/history.py、指标抽取与 Chart.js 折线图；2026-09-14 回归 tests/unit/test_history.py、test_insights.py 共 19 通过；未找到合并提交记录 | 2026-09-14 |
| <a id="financial-agent-architecture"></a>财报分析智能体架构优化 | [设计](specs/2026-08-05-智能体架构优化设计.md) | 未创建 | 已实现 | — | 用户确认 2026-09-14：历史需求已完成；原合并记录缺失；交付物已在 main：evidence/models.py、facts.py、report_identity.py、webapp/task_store.py；2026-09-14 回归 test_evidence_models.py、test_facts.py、test_tasks.py 共 32 通过；无同主题实施计划，未找到合并提交记录 | 2026-09-14 |
| <a id="rag-knowledge-qa"></a>RAG 知识库与通用问答 | [设计](specs/2026-08-17-RAG知识库与通用问答设计.md) | [计划](plans/2026-08-17-RAG知识库与通用问答-Phase1.md) | 已实现 | — | 用户确认 2026-09-14：历史需求已完成；原合并记录缺失；交付物已在 main：financial_report_fetcher/rag/ 子包与 rag CLI；.superpowers/sdd/2026-08-17-RAG知识库与通用问答-Phase1/ 存有任务报告；2026-09-14 回归 rag 五个单测文件共 59 通过；未找到合并提交记录 | 2026-09-14 |
| <a id="rag-enhanced-analysis"></a>RAG 增强多维度分析 | [设计](specs/2026-08-18-RAG增强多维度分析设计.md) | [计划](plans/2026-08-18-RAG增强多维度分析.md) | 已实现 | — | 用户确认 2026-09-14：历史需求已完成；原合并记录缺失；交付物已在 main：rag/analysis.py 与维度配置；2026-09-14 回归 test_rag_analysis.py、test_analysis_config.py 共 12 通过；未找到合并提交记录 | 2026-09-14 |
| <a id="rag-reranker"></a>RAG 查询重排序（rerank） | [设计](specs/2026-08-19-RAG查询重排序rerank设计.md) | 未创建 | 已实现 | — | 用户确认 2026-09-14：历史需求已完成；原合并记录缺失；交付物已在 main：rag/reranker.py；2026-09-14 回归 test_rag_reranker.py 31 通过；设计文档自述尚未实施，但代码与测试均已存在，且未找到合并提交记录 | 2026-09-14 |
| <a id="chat-mcp-tools"></a>问答接入 MCP 工具 | [设计](specs/2026-08-19-问答接入MCP工具设计.md) | 未创建 | 已实现 | — | 用户确认 2026-09-14：历史需求已完成；原合并记录缺失；交付物已在 main：rag/mcp_tools.py、webapp/mcp_guard.py；2026-09-14 回归 test_mcp_tools.py、test_mcp_client.py、test_mcp_guard.py、test_market_mcp_client.py 共 38 通过；设计文档自述尚未实施，但代码与测试均已存在，且未找到合并提交记录 | 2026-09-14 |
| <a id="git-privacy-cleanup"></a>功能修复与 Git 隐私清理 | [设计](specs/2026-08-28-功能修复与Git隐私清理设计.md) | [计划](plans/2026-08-28-功能修复与Git隐私清理.md) | 已实现 | — | 用户确认 2026-09-14：历史需求已完成；原合并记录缺失；交付物已在 main：会话隔离、AI 配置优先级、敏感扫描与 PDF 原子落盘；2026-09-14 回归 test_config.py、test_downloader.py、test_chat_store.py 共 66 通过；未找到合并提交记录 | 2026-09-14 |
| <a id="p0-reliability"></a>P0 可靠性修复 | [设计](specs/2026-08-30-P0可靠性修复设计.md) | [计划](plans/2026-08-30-P0可靠性修复.md) | 已实现 | — | 用户确认 2026-09-14：历史需求已完成；原合并记录缺失；交付物已在 main：report_identity.py、webapp/task_store.py、facts.py 校验器；2026-09-14 回归 test_models.py、test_evidence_resolver.py、test_facts.py、test_tasks.py 共 77 通过；未找到合并提交记录 | 2026-09-14 |
| <a id="dynamic-evidence-analysis"></a>动态证据化财报分析 | [设计](specs/2026-09-01-动态证据化财报分析-design.md) | [计划](plans/2026-09-01-动态证据化财报分析.md) | 已实现 | — | 用户确认 2026-09-14：历史需求已完成；原合并记录缺失；交付物已在 main：analysis_pipeline.py、evidence/ 子包与渐进式 SSE；2026-09-14 回归 test_analysis_pipeline.py、test_evidence_cache.py、test_ocr_enrichment.py、test_structured_data.py 共 42 通过；未找到合并提交记录 | 2026-09-14 |
| <a id="multi-turn-web-search"></a>多轮问答与网页搜索 | [设计](specs/2026-09-02-多轮问答与网页搜索设计.md) | [计划](plans/2026-09-02-多轮问答与网页搜索.md) | 已实现 | — | 用户确认 2026-09-14：历史需求已完成；原合并记录缺失；交付物已在 main：rag/web_search.py、SSE 阶段事件与网页来源渲染；2026-09-14 回归 test_web_search.py、test_ai_client_stream.py、test_rag_qa.py 共 35 通过；未找到合并提交记录 | 2026-09-14 |
| <a id="progressive-report-reading"></a>渐进式财报报告阅读体验 | [设计](specs/2026-09-08-渐进式财报报告阅读体验-design.md) | [计划](plans/2026-09-08-渐进式财报报告阅读体验.md) | 已实现 | — | 用户确认 2026-09-14：历史需求已完成；原合并记录缺失；交付物已在 main：analysis_workflow.js 报告式渲染与 PDF 页码定位；2026-09-14 回归 test_progressive_analysis_ui.py、test_analysis_workflow_js.py 共 62 通过；未找到合并提交记录 | 2026-09-14 |
| <a id="analysis-tab-priority"></a>分析主题Tab优先级 | [设计](specs/2026-09-10-分析主题Tab优先级-design.md) | [计划](plans/2026-09-10-分析主题Tab优先级.md) | 已实现 | — | 合并 17e4dab；`python3 -m pytest tests/unit/test_analysis_result.py -k tab_label -q`：2 passed, 2 deselected in 0.12s | 2026-09-14 |
| <a id="trusted-chat-agent"></a>智能问答可信 Agent 升级 | [设计](specs/2026-09-10-智能问答可信Agent升级-design.md) | [M1](plans/2026-09-10-智能问答可信Agent-M1.md)、[M2](plans/2026-09-10-智能问答可信Agent-M2.md)、[M3](plans/2026-09-10-智能问答可信Agent-M3.md)、[M4](plans/2026-09-10-智能问答可信Agent-M4.md) | 实施中 | — | M1 合并 6276701；设计文档第 14 节保留 `python3 -m pytest tests/browser/test_chat_trust_flow.py -q`：6 passed；M2–M4 尚未完成 | 2026-09-14 |
| <a id="financial-structure-visuals"></a>财务结构可视化 | [设计](specs/2026-09-10-财务结构可视化-design.md) | [计划](plans/2026-09-10-财务结构可视化.md) | 已实现 | — | 合并 1107509；`python3 -m pytest tests/unit/test_visualizations.py -q`：14 passed in 0.12s | 2026-09-14 |
| <a id="design-registry"></a>方案设计台账 | [设计](specs/2026-09-14-方案设计台账-design.md) | [计划](plans/2026-09-14-方案设计台账.md) | 已实现 | — | 合并 f27056c；`python3 -m pytest tests/unit/test_design_registry.py -q` → 4 passed in 0.11s | 2026-09-14 |
