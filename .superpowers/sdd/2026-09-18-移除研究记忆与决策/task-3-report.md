# Task 3 实施报告：更新浏览器 fixture 与验收

## 状态

已完成实现与适用验证；本分支尚未推送或合并，因此没有合并 SHA。

## 实施

- 增加 fixture-safety 契约：浏览器启动器不含 `ResearchMemoryStore` 或 `save_decision`，浏览器流程源码不再保留已删除入口标识。
- 删除浏览器流程中保存、撤销、刷新和查询已删除持久化能力的断言。
- 在真实工作台中断言已删除面板、事实保存动作和决策保存动作均不存在；断言使用运行时组装的选择器，使源码 safety 契约同时防止已删除标识重新进入流程。
- 保留并验收公司/状态筛选、收藏、导出下载拦截、质量状态、会话删除后的 PDF 可用性，以及 1280px、768px、390px 三视口无横向溢出。
- 浏览器验收继续检查导出点击被拦截、未触达原生下载，以及 console/page errors 和失败的同源请求。

## TDD 与验证

- RED：`python3 -m pytest tests/unit/test_browser_test_safety.py -k 'workspace_browser_fixture' -q`（1 failed；浏览器流程仍含已删除入口标识）。
- GREEN：同一命令（1 passed，12 deselected）。
- `python3 -m pytest tests/unit/test_browser_test_safety.py -q`（13 passed）。
- `python3 -m pytest tests/browser/test_research_workspace_flow.py -q`（12 passed）。
- `python3 -m pytest tests/unit/test_server_api.py tests/unit/test_chat_rendering_js.py -q`（199 passed，3 warnings）。
- `python3 -m pytest -q`（1145 passed，2 skipped，3 warnings）。
- `git diff --check` 与 `python3 scripts/check_css.py`（通过）。

## 数据语义与合入后文档动作

研究记忆和决策的持久化能力已由 Task 1/2 移除；启动时仅删除默认 `data/research_memory.json`，删除失败会阻止启动。此次浏览器回归不再创建、保存、读取或撤销该数据，导出点击被测试拦截，未触达原生下载。

按未合入分支 ruling，本任务未修改 `README.md`、`docs/FEATURE-CATALOG.md` 或 `docs/superpowers/DESIGN-REGISTRY.md`。合入 `main` 后必须以实际合并短 SHA 和本报告中的实际验证结果更新设计台账；README 移除研究记忆/决策说明；FEATURE-CATALOG 移除记忆 API/侧车/不变量并记录破坏性移除。

## 偏离与风险

Task 3 计划中的 fixture 移除部分已在 Task 1 按“直接消费者最小解耦”裁定完成；本 Task 完成其余浏览器流程迁移，报告按实际命名保存为 `task-3-report.md`。风险为破坏性移除使旧研究记忆和决策不可恢复；这是已确认的产品语义。浏览器 suite 依赖本机可用的 `agent-browser`，本次环境已实际执行并通过。
