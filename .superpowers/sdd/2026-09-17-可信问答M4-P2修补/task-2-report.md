# Task 2 实施报告：使用 Fact ID 保存研究记忆并迁移后端消费者

## 根因

- 研究记忆 API 以共享 `evidence_ids` 查找事实，无法区分同页的多个已核验事实。
- 旧序列化 Fact 没有稳定 ID 时仍可写入研究记忆。
- 记忆、导出与离线评测各自保留 PDF 身份/URL 判断，存在规则漂移风险。

## 变更

- `ResearchMemoryStore.save_fact` 先拒绝空 `Fact.id`，并统一调用 `fact_is_backed_by_pdf`。
- `POST /api/research/memory/facts` 仅以非空 `Fact.id` 精确匹配；未知、旧 Fact 或不唯一结果均返回 422，原会话归属检查未变。
- 记忆、导出和评测改为复用 `webapp.evidence_identity`；集中 PDF URL 校验以严格本地 history-PDF、正页码和安全文件名规则拒绝不安全链接。
- 回归覆盖同页多 Fact、legacy Fact 拒绝、完整提供的 `fact_` ID round-trip、不安全 PDF URL，以及导出/评测的共享校验导入。

## 实际命令与输出

| 阶段 | 命令 | 输出 |
| --- | --- | --- |
| RED | `python3 -m pytest tests/unit/test_research_memory.py tests/unit/test_server_api.py -q -k 'fact_id or shared_page or legacy_fact'` | 退出码 1；legacy Fact 未被拒绝，Fact ID API 查找返回 422。 |
| GREEN（聚焦） | `python3 -m pytest tests/unit/test_research_memory.py tests/unit/test_research_export.py tests/unit/test_chat_evaluation.py tests/unit/test_server_api.py -q -k 'fact_id or shared_page or legacy_fact or evidence'` | `10 passed, 200 deselected`。 |
| GREEN（Task 2 回归） | `python3 -m pytest tests/unit/test_research_memory.py tests/unit/test_research_export.py tests/unit/test_chat_evaluation.py tests/unit/test_server_api.py -q` | `210 passed`。 |
| 相关契约回归 | `python3 -m pytest tests/unit/test_chat_models.py tests/unit/test_evidence_identity.py tests/unit/test_research_memory.py tests/unit/test_research_export.py tests/unit/test_chat_evaluation.py tests/unit/test_server_api.py -q` | `246 passed`。 |
| 差异检查 | `git diff --check` | 通过，无输出。 |

## 提交

`HEAD fix: 以稳定事实身份保存研究记忆`

## 风险

- Task 3--5 仍未实施；质量摘要生产、工作台刷新/UI 和全链路验收不在本 Task 范围内。
- 未推送或合并；设计台账保持“实施中”。
