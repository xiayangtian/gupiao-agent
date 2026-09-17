# Task 3 实施报告：健康/负向评测分组与显式质量摘要命令

## 变更

- 固定离线 fixture schema 升级为 v2；每个用例必须声明 `health` 或 `probe`，health 不允许预期失败，probe 必须列出枚举化预期失败码。
- `ChatEvaluator.run_suite()` 独立回放 health/probe；health 仅在没有失败码时通过，probe 仅在每个用例检测到其全部声明失败码时通过。
- `QualitySummary.to_dict()` 和 v2 sidecar payload 只输出安全聚合指标；probe 使用 `detected_failure_codes`，不输出逐用例、问题或失败原文。
- 新增 `scripts/run_chat_evaluation.py`，仅导入离线 evaluator，写入 `<output>.tmp` 后以 `os.replace` 原子替换；fixture 解析或评测失败时返回非零且不覆盖旧摘要。
- API 只接受 v2 summary 并白名单读取 `generated_at`、health/probe 安全聚合；未知嵌套失败码或无效结构返回 `available: false`。

## 验证

| 阶段 | 命令 | 输出 |
| --- | --- | --- |
| RED | `python3 -m pytest tests/unit/test_chat_evaluation.py tests/unit/test_server_api.py -q -k 'suite or quality_command or quality_summary'` | 退出码 2；`ModuleNotFoundError: scripts.run_chat_evaluation`。 |
| GREEN（聚焦） | `python3 -m pytest tests/unit/test_chat_evaluation.py tests/unit/test_server_api.py -q -k 'suite or quality_command or quality_summary'` | `5 passed, 184 deselected`。 |
| Task 3 回归 | `python3 -m pytest tests/unit/test_chat_evaluation.py tests/unit/test_server_api.py -q` | `189 passed, 3 warnings`。 |
| 质量命令 | `python3 scripts/run_chat_evaluation.py --fixture tests/fixtures/chat_eval_cases.json --output /tmp/research_quality_summary.json` | 成功；health/probe 均通过，probe 检测到 `scope_leak: 1`。 |
| 差异检查 | `git diff --check` | 通过，无输出。 |

## Task 3 Fix round 1（审查 P1/P2）

- 固定错误的 fixture 均归入 `probe`：`scope-leak`、`pdf-without-page`、`news-attribution` 和 `stop-recovery` 分别声明 `scope_leak`、`invalid_pdf_page_url`、`external_fact_missing_as_of` 和 `stopped_run_rendered_complete`。health 仅保留没有契约失败的固定输出；probe summary 对四个 failure code 各计数一次。
- 质量 API 只接受带时区的扩展 ISO-8601 `generated_at`，并在返回前归一为 UTC。prompt/case-like 字符串与 naive 时间戳都 fail-closed 为 `available: false`。
- `EvaluationFixture.from_dict()` 不再把 `schema_version` 强制转换为整数；字符串版本会被 schema 校验拒绝。

| 阶段 | 命令 | 输出 |
| --- | --- | --- |
| RED | `python3 -m pytest tests/unit/test_chat_evaluation.py tests/unit/test_server_api.py -q -k 'health_summary_can_pass or fixture_requires_a_supported_schema_version or quality_rejects_non_timezone_generated_at or quality_normalizes_timezone_generated_at_to_utc'` | `6 failed`：fixture 未生成四个 probe 检测、字符串版本被 coercion 接受，API 接受任意/naive `generated_at` 且未归一 UTC。 |
| GREEN（聚焦） | 同上 | `6 passed, 187 deselected, 3 warnings`。 |
| Task 3 回归 | `python3 -m pytest tests/unit/test_chat_evaluation.py tests/unit/test_server_api.py -q` | `193 passed, 3 warnings`。 |
| 质量命令 | `python3 scripts/run_chat_evaluation.py --fixture tests/fixtures/chat_eval_cases.json --output /tmp/research_quality_summary.json` | 成功；health 6 个健康输出通过，probe 4 个固定错误通过，四个检测码均为 `1`。 |
| 差异检查 | `git diff --check` | 通过，无输出。 |

## 风险

- 本任务没有实现 Task 4--5 的质量状态 UI、浏览器验收或文档目录更新。
- 质量摘要是显式本地观测，不在服务启动或 API 请求中运行；未推送或合并。
