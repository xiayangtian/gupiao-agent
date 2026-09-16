# M3 实施进度

- 状态：实施中（`feat/trusted-chat-m3`）
- 范围：严格执行 M3 Tasks 1–7；不含 M4 收藏、导出、跨会话筛选或长期记忆。

| Task | 状态 | RED | GREEN | 提交 | 偏离 |
|---|---|---|---|---|---|
| 1 | 已完成 | 失败（模块不存在） | 35 passed | `1f0c638` | 无 |
| 2 | 已完成 | 失败（模块不存在） | 9 passed | `adb5d2c` | 无 |
| 3 | 已完成 | 失败（模块不存在） | 3 passed | `a264ecb` | 无 |
| 4 | 已完成 | 失败（模块不存在） | 11 passed | `a13f809` | 无 |
| 5 | 已完成 | 契约缺失 | 5 passed | `4220e57` | 普通流未改用 TaskManager |
| 6 | 已完成 | 渲染函数缺失 | 17 passed | `d0014d9` | 无 |
| 7 | 已完成 | 浏览器用例缺失 | 1029 passed, 2 skipped | 待提交 | 本地 fixture，无外网 |

## 命令日志

- RED `python3 -m pytest tests/unit/test_research_models.py -q`：预期收集失败（`ModuleNotFoundError`）。
- GREEN `python3 -m pytest tests/unit/test_research_models.py tests/unit/test_chat_models.py -q`：35 passed。
- RED `python3 -m pytest tests/unit/test_research_planner.py -q`：预期收集失败（`ModuleNotFoundError`）。
- GREEN `python3 -m pytest tests/unit/test_research_planner.py tests/unit/test_research_models.py -q`：9 passed。
- RED `python3 -m pytest tests/unit/test_research_executor.py -q`：预期收集失败（`ModuleNotFoundError`）。
- GREEN `python3 -m pytest tests/unit/test_research_executor.py -q`：3 passed。
- RED `python3 -m pytest tests/unit/test_research_agent.py -q`：预期收集失败（`ModuleNotFoundError`）。
- GREEN `python3 -m pytest tests/unit/test_research_agent.py tests/unit/test_research_executor.py tests/unit/test_research_models.py -q`：11 passed。

- 浏览器 QA：1280x900、768x1000、390x844 均无横向溢出；控制台/页面错误为空。
- 完整验证：`python3 -m pytest -q`：1029 passed, 2 skipped, 3 warnings；`git diff --check` 通过；`python3 scripts/check_css.py` 通过。
