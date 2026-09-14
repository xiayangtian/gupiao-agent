# 方案设计台账

本台账是已完成方案设计的唯一执行状态源。优先查看“未完成项目”；每条均可跳转到历史设计和实施计划。

状态依据为人工核对的方案文档、进度记录和已合入 `main` 的验证证据；不以文件名相似、目录命名或推测性提交信息判断完成状态。判定口径：

- **待实现**：设计已完成，未找到已验证并合入 `main` 的实现证据。
- **实施中**：存在实施分支或进度记录，尚未完成验证并合入 `main`。
- **已实现**：已合入 `main`，且已记录合并提交短 SHA 与实际验证结果。
- **待核实**：历史记录不足（多数旧方案由 `main` 直接提交落地，缺少合并提交记录），无法据此断言为待实现或已实现；此时证据栏写明已核对到的交付物，供人工裁决。

可执行性取值：`可直接执行`（待实现且实施计划已存在）、`先补计划`（只有设计文档，没有实施计划）、`—`（无需从本台账直接启动）。

表格格式约定：数据行以 `|` 开头，供 `tests/unit/test_design_registry.py` 识别登记行，因此表头行与分隔行不写首尾竖线。修改台账时不得删除数据行的起始竖线，也不要增删 `specs/*.md` 链接使登记行与设计文档失配。

## 未完成项目

“待核实”表示缺少可验证的合并记录，并不等于未实现；请结合“全部设计”中的证据栏判断。当前 14 条中，只有“智能问答可信 Agent 升级”在仓库中找不到任何实现产物；其余 12 条“待核实”的交付物均已在 `main`，但缺少可核对的合并提交与验证记录，需要人工确认后才可转为“已实现”。

方案 | 状态 | 可执行性 | 方案入口
--- | --- | --- | ---
| 智能问答可信 Agent 升级 | 待实现 | 可直接执行 | [设计](specs/2026-09-10-智能问答可信Agent升级-design.md)、[M1](plans/2026-09-10-智能问答可信Agent-M1.md)、[M2](plans/2026-09-10-智能问答可信Agent-M2.md)、[M3](plans/2026-09-10-智能问答可信Agent-M3.md)、[M4](plans/2026-09-10-智能问答可信Agent-M4.md) |
| 方案设计台账 | 实施中 | — | [设计](specs/2026-09-14-方案设计台账-design.md)、[计划](plans/2026-09-14-方案设计台账.md) |
| 渐进式财报报告阅读体验 | 待核实 | — | [设计](specs/2026-09-08-渐进式财报报告阅读体验-design.md)、[计划](plans/2026-09-08-渐进式财报报告阅读体验.md) |
| 多轮问答与网页搜索 | 待核实 | — | [设计](specs/2026-09-02-多轮问答与网页搜索设计.md)、[计划](plans/2026-09-02-多轮问答与网页搜索.md) |
| 动态证据化财报分析 | 待核实 | — | [设计](specs/2026-09-01-动态证据化财报分析-design.md)、[计划](plans/2026-09-01-动态证据化财报分析.md) |
| P0 可靠性修复 | 待核实 | — | [设计](specs/2026-08-30-P0可靠性修复设计.md)、[计划](plans/2026-08-30-P0可靠性修复.md) |
| 功能修复与 Git 隐私清理 | 待核实 | — | [设计](specs/2026-08-28-功能修复与Git隐私清理设计.md)、[计划](plans/2026-08-28-功能修复与Git隐私清理.md) |
| 问答接入 MCP 工具 | 待核实 | — | [设计](specs/2026-08-19-问答接入MCP工具设计.md) |
| RAG 查询重排序（rerank） | 待核实 | — | [设计](specs/2026-08-19-RAG查询重排序rerank设计.md) |
| RAG 增强多维度分析 | 待核实 | — | [设计](specs/2026-08-18-RAG增强多维度分析设计.md)、[计划](plans/2026-08-18-RAG增强多维度分析.md) |
| RAG 知识库与通用问答 | 待核实 | — | [设计](specs/2026-08-17-RAG知识库与通用问答设计.md)、[计划](plans/2026-08-17-RAG知识库与通用问答-Phase1.md) |
| 财报分析智能体架构优化 | 待核实 | — | [设计](specs/2026-08-05-智能体架构优化设计.md) |
| 分析历史与图表 | 待核实 | — | [设计](specs/2026-08-03-分析历史与图表-design.md)、[计划](plans/2026-08-03-分析历史与图表.md) |
| 财报分析前端界面 | 待核实 | — | [设计](specs/2026-08-03-财报分析前端界面-design.md)、[计划](plans/2026-08-03-财报分析前端界面.md) |

## 全部设计

主记录保存每个设计的完整字段；新增设计在此追加一行，并同步更新上方未完成索引。核对日期统一为 2026-09-14。

方案 | 设计文档 | 实施计划 | 实现状态 | 可执行性 | 实现证据 | 更新日期
--- | --- | --- | --- | --- | --- | ---
| 财报分析前端界面 | [设计](specs/2026-08-03-财报分析前端界面-design.md) | [计划](plans/2026-08-03-财报分析前端界面.md) | 待核实 | — | 交付物已在 main：webapp/server.py、autocomplete.py、tasks.py 与静态前端；2026-09-14 回归 tests/unit/test_server_api.py、test_autocomplete.py 共 108 通过；未找到合并提交记录 | 2026-09-14 |
| 分析历史与图表 | [设计](specs/2026-08-03-分析历史与图表-design.md) | [计划](plans/2026-08-03-分析历史与图表.md) | 待核实 | — | 交付物已在 main：webapp/history.py、指标抽取与 Chart.js 折线图；2026-09-14 回归 tests/unit/test_history.py、test_insights.py 共 19 通过；未找到合并提交记录 | 2026-09-14 |
| 财报分析智能体架构优化 | [设计](specs/2026-08-05-智能体架构优化设计.md) | 未创建 | 待核实 | — | 交付物已在 main：evidence/models.py、facts.py、report_identity.py、webapp/task_store.py；2026-09-14 回归 test_evidence_models.py、test_facts.py、test_tasks.py 共 32 通过；无同主题实施计划，未找到合并提交记录 | 2026-09-14 |
| RAG 知识库与通用问答 | [设计](specs/2026-08-17-RAG知识库与通用问答设计.md) | [计划](plans/2026-08-17-RAG知识库与通用问答-Phase1.md) | 待核实 | — | 交付物已在 main：financial_report_fetcher/rag/ 子包与 rag CLI；.superpowers/sdd/2026-08-17-RAG知识库与通用问答-Phase1/ 存有任务报告；2026-09-14 回归 rag 五个单测文件共 59 通过；未找到合并提交记录 | 2026-09-14 |
| RAG 增强多维度分析 | [设计](specs/2026-08-18-RAG增强多维度分析设计.md) | [计划](plans/2026-08-18-RAG增强多维度分析.md) | 待核实 | — | 交付物已在 main：rag/analysis.py 与维度配置；2026-09-14 回归 test_rag_analysis.py、test_analysis_config.py 共 12 通过；未找到合并提交记录 | 2026-09-14 |
| RAG 查询重排序（rerank） | [设计](specs/2026-08-19-RAG查询重排序rerank设计.md) | 未创建 | 待核实 | — | 交付物已在 main：rag/reranker.py；2026-09-14 回归 test_rag_reranker.py 31 通过；设计文档自述尚未实施，但代码与测试均已存在，且未找到合并提交记录 | 2026-09-14 |
| 问答接入 MCP 工具 | [设计](specs/2026-08-19-问答接入MCP工具设计.md) | 未创建 | 待核实 | — | 交付物已在 main：rag/mcp_tools.py、webapp/mcp_guard.py；2026-09-14 回归 test_mcp_tools.py、test_mcp_client.py、test_mcp_guard.py、test_market_mcp_client.py 共 38 通过；设计文档自述尚未实施，但代码与测试均已存在，且未找到合并提交记录 | 2026-09-14 |
| 功能修复与 Git 隐私清理 | [设计](specs/2026-08-28-功能修复与Git隐私清理设计.md) | [计划](plans/2026-08-28-功能修复与Git隐私清理.md) | 待核实 | — | 交付物已在 main：会话隔离、AI 配置优先级、敏感扫描与 PDF 原子落盘；2026-09-14 回归 test_config.py、test_downloader.py、test_chat_store.py 共 66 通过；未找到合并提交记录 | 2026-09-14 |
| P0 可靠性修复 | [设计](specs/2026-08-30-P0可靠性修复设计.md) | [计划](plans/2026-08-30-P0可靠性修复.md) | 待核实 | — | 交付物已在 main：report_identity.py、webapp/task_store.py、facts.py 校验器；2026-09-14 回归 test_models.py、test_evidence_resolver.py、test_facts.py、test_tasks.py 共 77 通过；未找到合并提交记录 | 2026-09-14 |
| 动态证据化财报分析 | [设计](specs/2026-09-01-动态证据化财报分析-design.md) | [计划](plans/2026-09-01-动态证据化财报分析.md) | 待核实 | — | 交付物已在 main：analysis_pipeline.py、evidence/ 子包与渐进式 SSE；2026-09-14 回归 test_analysis_pipeline.py、test_evidence_cache.py、test_ocr_enrichment.py、test_structured_data.py 共 42 通过；未找到合并提交记录 | 2026-09-14 |
| 多轮问答与网页搜索 | [设计](specs/2026-09-02-多轮问答与网页搜索设计.md) | [计划](plans/2026-09-02-多轮问答与网页搜索.md) | 待核实 | — | 交付物已在 main：rag/web_search.py、SSE 阶段事件与网页来源渲染；2026-09-14 回归 test_web_search.py、test_ai_client_stream.py、test_rag_qa.py 共 35 通过；未找到合并提交记录 | 2026-09-14 |
| 渐进式财报报告阅读体验 | [设计](specs/2026-09-08-渐进式财报报告阅读体验-design.md) | [计划](plans/2026-09-08-渐进式财报报告阅读体验.md) | 待核实 | — | 交付物已在 main：analysis_workflow.js 报告式渲染与 PDF 页码定位；2026-09-14 回归 test_progressive_analysis_ui.py、test_analysis_workflow_js.py 共 62 通过；未找到合并提交记录 | 2026-09-14 |
| 分析主题Tab优先级 | [设计](specs/2026-09-10-分析主题Tab优先级-design.md) | [计划](plans/2026-09-10-分析主题Tab优先级.md) | 已实现 | — | 合并 17e4dab（相关修复合并 554b967）；2026-09-14 回归 tests/unit/test_analysis_result.py、test_analysis_workflow_js.py 共 44 通过 | 2026-09-14 |
| 智能问答可信 Agent 升级 | [设计](specs/2026-09-10-智能问答可信Agent升级-design.md) | [M1](plans/2026-09-10-智能问答可信Agent-M1.md)、[M2](plans/2026-09-10-智能问答可信Agent-M2.md)、[M3](plans/2026-09-10-智能问答可信Agent-M3.md)、[M4](plans/2026-09-10-智能问答可信Agent-M4.md) | 待实现 | 可直接执行 | 无；webapp/chat_models.py、webapp/chat_scope.py、webapp/chat_evidence.py 均不存在，会话仍为 schema v1，M1 尚未开工 | 2026-09-14 |
| 财务结构可视化 | [设计](specs/2026-09-10-财务结构可视化-design.md) | [计划](plans/2026-09-10-财务结构可视化.md) | 已实现 | — | 合并 1107509（收尾修复合并 f56d3b9）；2026-09-14 回归 test_visualizations.py、test_analysis_visualizations_js.py、test_progressive_analysis_ui.py 共 40 通过 | 2026-09-14 |
| 方案设计台账 | [设计](specs/2026-09-14-方案设计台账-design.md) | [计划](plans/2026-09-14-方案设计台账.md) | 实施中 | — | 实施分支 feat/design-registry；进度记录 .superpowers/sdd/2026-09-14-方案设计台账/progress.md | 2026-09-14 |
