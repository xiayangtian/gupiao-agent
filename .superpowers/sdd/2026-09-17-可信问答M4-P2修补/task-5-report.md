# Task 5 实施报告：全链路验收、文档与回归

## 范围与裁定

- 新增 `test_quality_command_writes_only_requested_temp_output`：显式离线质量命令写入调用方提供的临时路径；仓库 `data/research_quality_summary.json` 不存在时不得创建，存在时字节内容不得被替换。
- Task 3 已在此任务开始前提供命令实现，故新隔离回归首次运行即为 GREEN；Task 3 的原始命令缺失 RED 证据见 `task-3-report.md`。未通过临时破坏既有实现伪造 RED。
- 不修改 `docs/FEATURE-CATALOG.md` 或 `README.md`。裁定原文：不得在未合入分支修改 `docs/FEATURE-CATALOG.md` 或 README，即使标注 pending 也不例外。Ruling：AGENTS.md 明确规定功能目录/README 仅记录已合入 main 的能力，优先于计划 Task 5 的笼统文档要求；本 Task 只在 M4 P2 completion report 与 progress ledger 记录准确的命令、验证、用户可见变化和“合入后必须同步 FEATURE-CATALOG/README”的待办。合入 main 后由协调方用实际 merge SHA 和最终验证结果更新这两个文档。代价：分支阶段的 catalog/README 暂不展示新命令，避免未合入功能被当作已发布。

合入后待办：由协调方使用实际 merge SHA 和最终验收结果，在 FEATURE-CATALOG 的“可信智能问答”条目更新稳定 Fact ID、统一证据身份、显式离线质量命令、质量 UI 与即时刷新；README 同步说明仅本地、只回放固定 fixture、观测性且不作为发布阻断的命令：

```bash
python3 scripts/run_chat_evaluation.py \
  --fixture tests/fixtures/chat_eval_cases.json \
  --output /tmp/research_quality_summary.json
```

该命令不是服务启动/API 请求的一部分，不调用真实 AI、MCP、网页或生产数据；它只生成安全聚合摘要，不能作为发布阻断器。

## 前序任务链路证据

| Task | RED / GREEN 证据 | 来源 |
| --- | --- | --- |
| 1 | RED 缺少 `webapp.evidence_identity`；GREEN `36 passed`，相关回归 `44 passed` | `task-1-report.md` |
| 2 | RED legacy Fact 未拒绝、Fact-ID 查找失败；GREEN 聚焦 `10 passed`、回归 `211 passed, 3 warnings` | `task-2-report.md` |
| 3 | RED 缺少 `scripts.run_chat_evaluation`；GREEN 聚焦 `5 passed`；修复轮 GREEN `6 passed, 187 deselected, 3 warnings`、回归 `193 passed, 3 warnings` | `task-3-report.md` |
| 4 | RED 缺少质量渲染/旧 Fact 保存条件；GREEN JS `30 passed`、浏览器 `12 passed` | `task-4-report.md` |
| 5 | 新命令隔离回归首次 GREEN：`1 passed, 10 deselected`（实现已由 Task 3 提供） | 本报告 |

## 实际验收

| 命令 | 结果 |
| --- | --- |
| `python3 -m pytest tests/unit/test_browser_test_safety.py -q -k quality_command` | `1 passed, 10 deselected in 0.36s`；仓库质量 sidecar SHA-256 未变化。 |
| `python3 -m pytest -q` | `1156 passed, 2 skipped, 3 warnings in 199.49s`。 |
| `python3 -m pytest tests/unit/test_server_api.py -q` | `169 passed, 3 warnings in 8.31s`。 |
| `node --check webapp/static/app.js && node --check webapp/static/chat_rendering.js` | 通过，无输出。 |
| `python3 scripts/check_css.py` | `✅ webapp/static/style.css 结构检查通过`。 |
| `python3 scripts/run_chat_evaluation.py --fixture tests/fixtures/chat_eval_cases.json --output /tmp/research_quality_summary.json` | 成功；`schema_version=2`、`health.passed=True`、`probe.passed=True`；四个 probe code `external_fact_missing_as_of`、`invalid_pdf_page_url`、`scope_leak`、`stopped_run_rendered_complete` 各为 1。输出随后已删除。 |
| `python3 -m pytest tests/browser/test_research_workspace_flow.py -q` | `12 passed in 39.76s`；真实本地 fixture 应用覆盖决策即时刷新、health/probe 状态、安全文本、筛选保留与三视口。 |
| `git diff --check` | 通过，无输出。 |

首次两次全量运行曾在 `agent-browser open` 阶段超时（不是应用断言失败）；终止由超时验收留下的 agent-browser daemon 后，相关浏览器模块为 `17 passed`、Task 5 工作台浏览器为 `12 passed`，最终完整命令为 `1156 passed, 2 skipped, 3 warnings`。该清理仅处理验收进程，未修改产品代码或测试。

## 三视口浏览器 QA

`tests/browser/test_research_workspace_flow.py` 实际以隔离的 `visual_test_app.py` 和 localhost 运行；它在 `1280x900`、`768x1000`、`390x844` 验证 `scrollWidth <= innerWidth`，并在桌面/移动视口验证保存决策后即时刷新、保留筛选、显示“健康评测：通过”及“负向探针：通过”、不回显用例文本。所有被测 localhost 非 favicon 请求为成功响应，且 console/page error 均为空。

额外 agent-browser 手工检查实际访问 `http://127.0.0.1:<temporary-port>/#/chat`：Tab 后焦点落在可交互 `<a>` 元素；三个尺寸的 `scrollWidth/innerWidth` 分别为 `1280/1280`、`768/768`、`390/390`；工作台均可见，console/page error 为空。网络记录中唯一 404 是测试应用未提供的 `/favicon.ico`，浏览器回归已明确排除该非功能资源；其余本地页面/API 请求均为 2xx。未保存截图、HAR、PDF 或浏览器产物。

## 风险与状态

- 无 P0/P1、无 Scope/owner 扩权、无自动交易/主动监控/推送。
- 浏览器 CLI 在长全量运行后曾残留 daemon 并造成 `open` 超时；清理后最终全量和定向浏览器验收均通过。若 CI 复现，应先清理遗留 agent-browser daemon，而非放宽超时或跳过浏览器 QA。
- 全局 `create_task_ledger_entry` / `update_task_ledger_entry` 工具不在本会话工具集中，因此全局任务台账未登记；本地 SDD ledger 已更新。
- 该分支尚未合并到 `main`；设计台账保持“实施中”，FEATURE-CATALOG/README 待合入后更新。
