# 智能问答可靠性第三阶段：异步规划、即时进度、取消与研究路由

日期：2026-10-08。基线：本地 `main` 的 P2 交付（`d197b1d`、文档 `8d5a1b7`）。

## 状态与执行边界

用户已确认本设计方案及默认值。实施对应全局任务 TASK-20260924-005；先在独立分支完成并验证，未获另行授权不推送远端，合入本地 `main` 前汇报证据并取得确认。

本阶段目标：将规划与整轮问答移出 ASGI 事件循环；在昂贵工作之前建立可关联的 SSE 运行身份；统一阶段进度、背压、超时和协作式取消；使明确研究请求稳定进入既有 ResearchAgent，且保留逐步持久化与安全恢复。

非目标：P4 真实链路评测/检索压缩；强杀 Python 线程或引入子进程执行；更换 Agent 框架；改变 P2 公司/时间/来源授权、调用预算、Fact 合约；普通问题自动升级成研究任务；跨进程/多实例的分布式任务调度。

## 确认的产品语义

- 只有用户明确表达研究意图（沿用 `IntentRouter` 的“研究计划/研究任务/分步骤/调研/研究一下”等词组）才进入 ResearchAgent；规划成功与否不能改变这个路由。模型计划只细化步骤，不能扩大 Scope、来源权限或 P2 调用预算。
- 普通问答不按模型计划复杂度自动升级研究。
- 用户取消、HTTP 客户端断开与服务端期限共用一个运行取消信号。同步调用正在途时不能安全强杀：最多运行到该调用现有超时；返回后检查取消状态并禁止启动下一个来源调用。已完成步骤和证据保留。
- 普通问答整轮时限默认 120 秒；研究任务默认 300 秒；允许服务端配置覆盖。超时有结果时保存 `partial`，无可用结果保存 `failed`；用户主动停止保存 `stopped`。单来源超时继续由既有工具策略控制。
- worker 并发默认上限 4；SSE 事件缓冲每运行上限 64。超过并发容量时快速返回受控繁忙响应，不无界排队。
- 原有 SSE 事件和 payload 保持兼容；新增事件只增加字段/事件，不泄漏原始 provider 输出、工具参数或模型私有推理。

## 架构与事件流

1. 请求校验和轻量会话获取后，为运行分配随机 run ID，并在首个可写 SSE 帧立即发送 `session`、`run_started` 与 `reasoning_stage(planning)`；任何模型规划调用都在此之后。
2. 一个每运行隔离的 `ChatRunControl` 保存 session/run 身份、deadline、cancel event、完成状态；线程安全注册表仅保留活动运行，终态/断开/异常后移除。
3. 专用有界 worker 执行范围解析、意图与时间冻结、普通计划或 ResearchAgent 计划、来源执行、回答/核验和持久化；普通计划失败回退规则仍遵守 P2 fail-closed。
4. worker 向每运行有界 `ChatEventChannel` 发布安全事件；队列满时生产线程等待容量，同时轮询取消和 deadline；消费者断开时停止等待并设置取消信号。事件 channel 有容量上限、可唤醒 ASGI 消费者，且关闭时能可靠唤醒等待者。
5. 标准阶段事件：`reasoning_stage`、`scope_resolved`、`execution_plan`/`plan_fallback`、`execution_step_started`、`execution_step_completed`、`execution_step_failed`、既有工具/证据事件、终态 `done`/`stopped`/`error`。计划和 step 的稳定 ID 用于前端合并状态。
6. `/api/chat/runs/{run_id}/cancel` 接收原 session ID；只允许取消同一 session 的活动运行，未知、终态或不匹配返回安全的 404/409。页面停止同时请求该端点并取消读取器；客户端断开由 SSE generator 的 `finally` 走相同控制信号。
7. 所有来源入口在调用前检查 `cancel_event` 和 deadline。若取消发生于在途来源中，记录其真实返回/失败状态但不再启动后续来源，最终 AnswerRun 与 ResearchRun 状态一致；运行结束后释放 semaphore、worker slot、channel 和注册表项。

## 验收要求

- 慢 planner 期间事件循环仍响应 health/其他 chat；两个会话可并行，不受单个阻塞 provider 影响。
- SSE 首帧身份早于 planner 完成；规划、步骤和终态事件有序且可消费，旧事件兼容。
- 队列容量保持有界；慢消费者施加真实反压；断开/取消解除 producer 等待。
- 普通问答和研究均受整轮 deadline；超时后状态与已取得证据一致，worker 并发额度最终归还。
- 明确研究请求在 ResearchPlanner 正常成功时进入 ResearchAgent；普通请求不会自动升级；研究停止后可恢复且已完成步骤不重复执行。
- 在途来源结束后没有下一次外部来源调用；P2 frozen scope/tool policy/runtime budget 不变。
- 用本地固定 fixture 替换模型、MCP、网页和行情，不调用真实 provider；相关前端行为通过真实隔离浏览器验证。

## 变更边界

预期责任文件：`webapp/chat_runs.py`（运行控制、容量与事件 channel）、`webapp/server.py`（启动、SSE/cancel API、worker 生命周期与阶段编排）、`webapp/chat_policy.py`（明确研究意图稳定路由，优先复用现有分类器）、`webapp/source_runtime.py`（来源调用前取消/期限门控，如接口最小扩展）、`webapp/static/app.js`（事件消费与停止请求）、必要时 `webapp/static/chat_rendering.js`（安全计划步骤状态）。

测试：`tests/unit/test_chat_runs.py`、`tests/unit/test_server_api.py`、`tests/unit/test_chat_policy.py`、`tests/unit/test_source_runtime.py`、`tests/unit/test_chat_rendering_js.py`；新建隔离 fixture/browser test 覆盖慢规划、普通并行、研究、取消和恢复。必要时扩展 `tests/browser/chat_reliability_p3_app.py` 与 `tests/browser/test_chat_reliability_p3.py`。
