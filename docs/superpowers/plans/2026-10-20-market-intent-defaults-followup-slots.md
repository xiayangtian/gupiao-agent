# 实施计划：市场请求默认值与多轮澄清槽位修复

## 目标与范围

按已确认设计修复行情请求的默认市场/期限，以及同一会话内短回复丢失待澄清槽位的问题。范围限定在聊天意图、请求解析、pending 持久化/SSE 流程与对应测试；不新增数据源，不改财报/常识/Web 的一般行为。

## 步骤（TDD）

1. **请求解析回归**：在 `tests/unit/test_chat_request_resolution.py` 添加缺市场/缺期限默认 A 股今日、孤立 `7天`、明确港股、pending unclear 槽位回复、pending 后新市场请求和同会话窗口/市场修正用例；先运行并确认失败。
2. **意图契约**：在 `tests/unit/test_chat_intent.py` 覆盖意图 prompt 对缺省槽位与 pending 短回复的指引，不更改 JSON schema。
3. **最小实现**：在 `webapp/chat_request_resolution.py` 实现默认值、基于可信上一轮/待澄清槽位的窄范围补全，以及 pending 匹配后的继续解析/保留；在 `webapp/chat_intent_analyzer.py` 明确默认与补槽规则。必要时仅调整 `webapp/chat_store.py` / `webapp/server.py`，保证未消费/未解析的 pending 不丢失且合并后的原问题与槽位送入范围解析。
4. **端到端回归**：扩展 `tests/unit/test_server_api.py`，用离线分类器、股票解析器及行情来源替身复现“再分析下海尔的今日行情”→“海尔智家”，并验证后续“今日行情”不会再次丢失公司；同时验证仅调用允许来源。
5. **聚焦验证**：运行上述测试及直接相关的 `test_chat_time.py`、`test_chat_followups.py`、`test_chat_store.py`、`test_execution_planner.py`；运行 `test_design_registry.py`、`compileall` 和 `git diff --check`。只记录实测结果；不做未授权远端推送或危险服务重启。
6. 验证通过后更新功能目录和设计台账；按项目流程本地合入 `main`，不推送远端。若有验证失败、服务无法安全重启或其他阻塞，保留未完成状态并准确记录。

## 风险与约束

- 短文本分类不可靠；pending 的槽位与已冻结意图优先，但公司身份只能由服务端索引/代码校验。
- 默认值只对市场分析槽位生效，不把任意金融问题重写成 A 股请求。
- 前一轮 broad market 或个股任务只有在当前会话、最近已完成 run 可验证时才能继承；显式的港股/新对象覆盖旧市场/公司。
- 市场来源不可用时继续披露 unavailable/partial，不能把默认值包装成已取得数据。
