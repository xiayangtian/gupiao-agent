# Implementation Plan: Financial Report Fetcher

## Overview

按照三层职责分离架构（Config → Fetcher → Downloader）逐步实现财报拉取工具。优先建立数据模型与异常体系，再依次实现各模块，最后完成 CLI 入口和集成测试。所有代码使用 Python 3，属性化测试使用 Hypothesis，单元测试使用 pytest。

## Tasks

- [x] 1. 初始化项目结构与依赖配置
  - 创建 `financial_report_fetcher/` 包目录及所有模块骨架文件（`__main__.py`、`config.py`、`fetcher.py`、`downloader.py`、`models.py`、`exceptions.py`）
  - 创建 `tests/unit/`、`tests/property/`、`tests/integration/` 目录及空 `__init__.py`
  - 创建 `requirements.txt`，写入 `pyyaml>=6.0`、`requests>=2.28`、`pydantic>=2.0`、`tenacity>=8.0`、`pytest`、`hypothesis`
  - 创建 `pytest.ini` 或 `pyproject.toml`，配置测试根目录为 `tests/`
  - _Requirements: 全局基础_

- [x] 2. 实现共享数据模型与自定义异常
  - [x] 2.1 在 `models.py` 中实现所有数据模型
    - 实现 `ReportType`、`DownloadStatus` 枚举
    - 实现 Pydantic 模型 `CompanyConfig`（含 `at_least_one_identifier` 校验器）、`AppConfig`（含 `validate_companies_count`、`validate_max_count`、`validate_date_range` 校验器）
    - 实现 dataclass `DateRange`（含 `contains` 方法）、`ReportMeta`、`DownloadSummary`（含 `total` 属性）
    - _Requirements: 1.1, 3.1, 3.3, 3.5, 3.6, 3.7, 5.1_

  - [x] 2.2 在 `exceptions.py` 中实现自定义异常层次
    - 实现 `FetcherBaseError` 及其子类：`ConfigFileNotFoundError`、`ConfigParseError`、`ConfigValidationError`、`DownloadTimeoutError`、`DownloadError`
    - _Requirements: 1.3, 1.4_

  - [x]* 2.3 为数据模型编写单元测试（`tests/unit/test_config.py` 中 model 部分）
    - 测试 `CompanyConfig` 缺少 ticker 和 name 时抛出验证错误
    - 测试 `AppConfig` companies 超出 1-50 范围时的错误
    - 测试 `AppConfig` max_count 越界时的错误
    - 测试 `AppConfig` start_date 晚于 end_date 时的错误
    - 测试 `AppConfig` 仅指定一个日期字段时的错误（含缺失字段名）
    - 测试 `DateRange.contains` 的边界值（闭区间两端）
    - _Requirements: 1.1, 3.5, 3.6, 3.7_

- [x] 3. 实现 Config 模块
  - [x] 3.1 在 `config.py` 中实现 `ConfigLoader.load()`
    - 检查文件是否存在，不存在时抛出 `ConfigFileNotFoundError`（含绝对路径）
    - 解析 YAML/JSON 文件，格式错误时抛出 `ConfigParseError`（含字段名或行号）
    - 将原始字典传入 `AppConfig` 进行 Pydantic 验证，失败时抛出 `ConfigValidationError`（含字段名）
    - 实现 `YYYY` 格式日期字符串到 `date` 的解析（start→01-01，end→12-31）
    - _Requirements: 1.1, 1.3, 1.4, 3.1, 3.5, 3.6, 3.7, 5.4_

  - [x]* 3.2 为 Config 模块编写单元测试（`tests/unit/test_config.py`）
    - 测试配置文件不存在时错误信息含路径
    - 测试 YAML 格式非法时错误信息含字段名
    - 测试 `YYYY` 格式解析（`"2023"` → start=2023-01-01，end=2023-12-31）
    - 测试 `YYYY-MM-DD` 格式正常加载
    - 测试合法配置文件正确返回 `AppConfig` 实例
    - _Requirements: 1.3, 1.4, 3.1_

  - [x]* 3.3 为 Config 模块编写属性化测试（`tests/property/test_properties.py`）
    - **Property 3: 配置错误信息包含相关字段名或路径**
    - 使用 `st.fixed_dictionaries` 生成缺字段或含非法值的配置字典，断言异常消息中含对应字段名或路径
    - `@settings(max_examples=100)`
    - **Validates: Requirements 1.3, 1.4, 3.5, 3.6, 3.7**

    - **Property 14: 非法 report_types 返回包含非法值的错误信息**
    - 使用 `st.lists(st.text())` 生成含非法字符串的 report_types，断言异常消息含该非法值
    - `@settings(max_examples=100)`
    - **Validates: Requirements 5.4**

- [x] 4. Checkpoint — 确保 Config 模块及数据模型测试全部通过
  - 运行 `python3 -m pytest tests/unit/test_config.py tests/property/test_properties.py -v`，确保所有测试通过；如有疑问请向用户确认。

- [x] 5. 实现 Fetcher 模块
  - [x] 5.1 在 `fetcher.py` 中实现 `ReportFetcher._resolve_company()`
    - 优先使用 ticker 匹配公司，其次使用 name；无法匹配时返回 `None`
    - 无法匹配时记录包含原始标识的 WARN 日志
    - _Requirements: 1.1, 1.5_

  - [x] 5.2 在 `fetcher.py` 中实现 `ReportFetcher._apply_filters()`
    - 按 `DateRange` 闭区间过滤 `period`
    - 按 `report_types` 精确匹配过滤
    - 按报告期降序排序
    - 按 `max_count` 截取（未配置时默认 1）
    - _Requirements: 3.2, 3.4, 5.2_

  - [x] 5.3 在 `fetcher.py` 中实现 `ReportFetcher.fetch()`
    - 若 start/end 未配置，自动设为 `{today.year-1}-01-01` 至 `{today.year-1}-12-31`，并记录 INFO 日志含实际日期值
    - 遍历 companies，调用 `_resolve_company` 和 `_apply_filters`，返回 `Iterator[ReportMeta]`
    - _Requirements: 1.2, 1.5, 4.1, 4.4_

  - [x]* 5.4 为 Fetcher 模块编写单元测试（`tests/unit/test_fetcher.py`）
    - 测试未配置 max_count 时默认值为 1（Requirements 4.3）
    - 测试未配置 report_types 时默认为 `["annual"]`（Requirements 5.3）
    - 测试未配置时间范围时 INFO 日志包含实际日期值（Requirements 4.4）
    - 测试无法匹配公司时跳过且 WARN 日志含原始标识（Requirements 1.5）
    - _Requirements: 1.2, 1.5, 4.3, 4.4, 5.3_

  - [x]* 5.5 为 Fetcher 模块编写属性化测试（`tests/property/test_properties.py`）
    - **Property 1: Fetcher 仅返回目标公司的财报**
    - 使用 `st.lists(st.text())` 公司集合 + 随机财报集合，断言结果中每条 `company_id` ∈ 目标集合
    - `@settings(max_examples=100)`
    - **Validates: Requirements 1.2**

    - **Property 2: 未知公司标识被跳过且日志包含原始标识**
    - 使用 `st.text()` 生成无法匹配的标识，断言 WARN 日志含该标识且结果中无该公司财报
    - `@settings(max_examples=100)`
    - **Validates: Requirements 1.5**

    - **Property 10: 日期区间过滤闭区间性**
    - 使用 `st.dates()` 生成 [start, end] 区间与随机财报集合，断言结果每条 `start <= period <= end`
    - `@settings(max_examples=100)`
    - **Validates: Requirements 3.2**

    - **Property 11: max_count 截取顺序与数量约束**
    - 使用 `st.integers(1, 10000)` + `st.lists(st.dates())` 生成财报集合，断言结果数量 `<= k`、降序排列、≥k 时恰好等于 k
    - `@settings(max_examples=100)`
    - **Validates: Requirements 3.4**

    - **Property 12: 默认时间范围为运行时上一自然年度**
    - 使用 `st.dates()` mock 当前时间，断言实际使用的 start/end 为 `{today.year-1}-01-01` / `{today.year-1}-12-31`
    - `@settings(max_examples=100)`
    - **Validates: Requirements 4.1**

    - **Property 13: report_types 过滤精确性**
    - 使用 `st.frozensets(st.sampled_from(ReportType))` 生成类型子集，断言结果中每条 `report_type ∈ T`
    - `@settings(max_examples=100)`
    - **Validates: Requirements 5.2**

- [x] 6. Checkpoint — 确保 Fetcher 模块测试全部通过
  - 运行 `python3 -m pytest tests/unit/test_fetcher.py tests/property/test_properties.py -v`，确保所有测试通过；如有疑问请向用户确认。

- [x] 7. 实现 Downloader 模块
  - [x] 7.1 在 `downloader.py` 中实现 `ReportDownloader.build_filename()`
    - 生成格式 `{company_id}_{report_type}_{period}.pdf` 的文件名
    - _Requirements: 2.2_

  - [x] 7.2 在 `downloader.py` 中实现 `ReportDownloader.download_one()`
    - 目录不存在时自动创建（`os.makedirs`）
    - 文件已存在时记录 INFO 日志（含路径）并返回 `SKIPPED`
    - 使用 `requests` + `tenacity` 实现下载，超时 60s，失败重试最多 3 次，间隔 5s
    - 下载后验证文件大小，为 0 字节时删除文件并返回 `FAILED`
    - 成功返回 `SUCCESS`；失败记录含文件名和错误原因的 ERROR 日志，返回 `FAILED`
    - _Requirements: 2.1, 2.3, 2.4, 2.6, 2.7, 2.8_

  - [x] 7.3 在 `downloader.py` 中实现 `ReportDownloader.download_all()`
    - 遍历 reports，调用 `download_one`，累计 `DownloadSummary`
    - 单个失败不中断循环
    - 最终输出汇总报告（成功/跳过/失败数量）
    - _Requirements: 2.5, 2.6_

  - [x]* 7.4 为 Downloader 模块编写单元测试（`tests/unit/test_downloader.py`）
    - 使用 `unittest.mock` mock `requests.get`，测试正常下载、超时、空文件各场景
    - 测试目标目录自动创建（Requirements 2.3）
    - 测试 `build_filename` 输出格式（Requirements 2.2）
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.7, 2.8_

  - [x]* 7.5 为 Downloader 模块编写属性化测试（`tests/property/test_properties.py`）
    - **Property 4: 文件命名格式不变式**
    - 使用 `st.text()` 公司标识 + `st.sampled_from(ReportType)` + `st.dates()`，断言结果匹配 `^.+_.+_.+\.pdf$`
    - `@settings(max_examples=100)`
    - **Validates: Requirements 2.2**

    - **Property 5: 已存在文件不被覆盖**
    - 使用 `st.binary()` 生成文件内容，写入后调用 `download_one`，断言返回 `SKIPPED` 且文件内容不变
    - `@settings(max_examples=100)`
    - **Validates: Requirements 2.4**

    - **Property 6: 下载汇总计数完整性**
    - 使用 `st.lists(st.sampled_from(DownloadStatus))` 生成 n 条结果，断言 `success + skipped + failed == n` 且三字段均非负
    - `@settings(max_examples=100)`
    - **Validates: Requirements 2.5**

    - **Property 7: 单个失败不中断整体流程**
    - 使用 `st.lists` 随机注入失败位置，断言 `download_all` 总处理数量 == n
    - `@settings(max_examples=100)`
    - **Validates: Requirements 2.6**

    - **Property 8: 重试次数不超过上限**
    - 使用 `st.integers` mock 网络始终失败，断言实际 HTTP 请求次数 `<= 4`（1次初始 + 最多3次重试）
    - `@settings(max_examples=100)`
    - **Validates: Requirements 2.7**

    - **Property 9: 零字节文件被删除并计入失败**
    - 使用 `st.just(b"")` 模拟空响应，断言文件被删除且计入 `failed`
    - `@settings(max_examples=100)`
    - **Validates: Requirements 2.8**

- [x] 8. Checkpoint — 确保 Downloader 模块测试全部通过
  - 运行 `python3 -m pytest tests/unit/test_downloader.py tests/property/test_properties.py -v`，确保所有测试通过；如有疑问请向用户确认。

- [x] 9. 实现 CLI 入口并串联所有模块
  - [x] 9.1 在 `__main__.py` 中实现 CLI 入口
    - 使用 `argparse` 解析 `--config`（必填，配置文件路径）参数
    - 调用 `ConfigLoader.load()` → `ReportFetcher.fetch()` → `ReportDownloader.download_all()`
    - 捕获 `FetcherBaseError` 子类，输出 ERROR 信息后以非零状态码退出
    - _Requirements: 1.3, 1.4, 2.5_

  - [x]* 9.2 编写集成测试（`tests/integration/test_download_flow.py`）
    - 使用 `pytest` 的 `tmp_path` 夹具与 mock HTTP 层
    - 测试目标目录不存在时自动创建（Requirements 2.3）
    - 测试文件成功保存到指定目录（Requirements 2.1）
    - 测试完整汇总报告输出（Requirements 2.5）
    - _Requirements: 2.1, 2.3, 2.5_

- [x] 10. 最终 Checkpoint — 确保所有测试通过
  - 运行 `python3 -m pytest tests/ -v`，确保全部单元、属性化、集成测试通过；如有疑问请向用户确认。

## Notes

- 标注 `*` 的子任务为可选任务，可跳过以加速 MVP 交付
- 每个任务均引用对应需求以保证可追溯性
- Checkpoint 任务确保每个模块完成后即时验证，降低集成风险
- 属性化测试（P1-P14）均使用 `@settings(max_examples=100)` 配置最少 100 次随机迭代
- 所有命令使用 `python3`，不使用 `python`
- 文件命名与模块划分严格遵循设计文档中的目录结构

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["2.1", "2.2"] },
    { "id": 1, "tasks": ["2.3", "3.1"] },
    { "id": 2, "tasks": ["3.2", "3.3", "5.1"] },
    { "id": 3, "tasks": ["5.2", "5.3"] },
    { "id": 4, "tasks": ["5.4", "5.5", "7.1"] },
    { "id": 5, "tasks": ["7.2", "7.3"] },
    { "id": 6, "tasks": ["7.4", "7.5", "9.1"] },
    { "id": 7, "tasks": ["9.2"] }
  ]
}
```
