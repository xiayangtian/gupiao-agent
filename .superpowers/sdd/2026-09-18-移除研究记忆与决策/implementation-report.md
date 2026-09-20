# 移除研究记忆与研究决策实施完成报告

已合入 `main`：`f9472e6 merge: 移除研究记忆与决策`。

## 交付

- 删除研究记忆/研究决策的存储、API、前端入口和浏览器 fixture。
- 应用启动时只删除遗留 `data/research_memory.json`；无法删除即阻断启动。
- 保留研究工作台筛选、收藏、导出、编辑重问、分支追问、质量观测、Scope/owner 边界及稳定 Fact ID。

## 验证

- `python3 -m pytest -q`：1147 passed，2 skipped，3 warnings。
- `python3 -m pytest tests/browser/test_research_workspace_flow.py -q`：12 passed。
- `python3 -m pytest tests/unit/test_browser_test_safety.py -q`：13 passed。
- `git diff --check` 与 `python3 scripts/check_css.py`：通过。

README、功能目录和设计台账已用实际合并 SHA 与验证证据同步更新。
