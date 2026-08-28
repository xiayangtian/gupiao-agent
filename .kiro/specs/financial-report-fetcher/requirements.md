# Requirements Document

## Introduction

财报拉取工具（Financial Report Fetcher）是一个自动化工具，允许用户根据配置文件定向拉取指定公司的财务报告，并将其下载保存到本地存储。工具支持自定义财报的时间范围与数量，默认拉取上一年度的财报。

## Glossary

- **Fetcher**：财报拉取工具的核心模块，负责从数据源获取财报数据
- **Config**：配置模块，负责读取和解析工具的配置信息
- **Downloader**：下载模块，负责将财报文件保存到本地存储
- **Report**：财务报告，包含公司定期发布的年报、半年报、季报等文件
- **Company**：目标公司，通过股票代码或公司名称标识
- **DateRange**：时间范围，表示财报的起止日期区间
- **Storage**：本地存储目录，用于保存下载的财报文件

## Requirements

### 需求 1：基于配置拉取指定公司财报

**用户故事：** 作为一名投资分析师，我希望通过配置文件指定目标公司，以便工具能够自动定向拉取对应公司的财报。

#### 验收标准

1. THE Config SHALL 支持配置一个或多个（最多50家）目标公司，每个公司通过股票代码（1至9位数字或字母，如 "600519"）或公司名称进行标识；当两者同时存在时，以股票代码为匹配依据
2. WHEN Config 加载成功，THE Fetcher SHALL 仅拉取配置中指定公司的财报，不拉取其他公司的财报
3. IF 配置文件不存在，THEN THE Config SHALL 返回包含具体路径信息的错误提示，且不启动拉取流程
4. IF 配置文件格式无效，THEN THE Config SHALL 返回包含具体字段名称的解析错误信息，且不启动拉取流程
5. IF 配置的公司标识无法匹配到已知公司，THEN THE Fetcher SHALL 记录包含原始标识内容的警告日志并跳过该公司，继续处理其余公司

---

### 需求 2：将财报下载到本地存储

**用户故事：** 作为一名投资分析师，我希望财报文件能够自动保存到本地目录，以便离线查阅和分析。

#### 验收标准

1. WHEN Fetcher 成功获取到财报数据，THE Downloader SHALL 将财报文件保存到配置指定的本地存储目录
2. THE Downloader SHALL 以"公司标识_财报类型_报告期.pdf"的格式命名保存的文件（例如：`600519_年报_2023.pdf`）
3. IF 目标存储目录不存在，THEN THE Downloader SHALL 自动创建该目录后继续下载
4. IF 目标存储路径中存在同名文件，THEN THE Downloader SHALL 跳过下载并记录包含文件路径的 INFO 级日志，不覆盖已有文件
5. WHEN 所有文件下载完成，THE Downloader SHALL 输出下载成功数量、跳过数量及失败数量的汇总报告
6. IF 单个文件下载失败，THEN THE Downloader SHALL 记录包含文件名称和错误原因的 ERROR 级日志，并继续下载其余文件，不中断整体流程
7. THE Downloader SHALL 对每个文件设置 60 秒下载超时，超时后视为下载失败并按标准 6 处理；网络请求失败时最多自动重试 3 次，重试间隔为 5 秒
8. WHEN 下载完成后，THE Downloader SHALL 验证文件大小不为 0 字节；IF 文件大小为 0 字节，THEN THE Downloader SHALL 删除该文件并将其计入失败数量

---

### 需求 3：支持选定财报的时间范围及数量

**用户故事：** 作为一名投资分析师，我希望能够指定财报的时间范围和最大数量，以便灵活控制拉取的财报范围。

#### 验收标准

1. THE Config SHALL 支持配置可选的 `start_date` 和 `end_date` 两个字段以定义时间范围，格式为 `YYYY`（`start_date` 解析为当年1月1日，`end_date` 解析为当年12月31日）或 `YYYY-MM-DD`；时间范围为闭区间 [start_date, end_date]
2. WHEN DateRange 被配置，THE Fetcher SHALL 仅拉取报告期在该闭区间范围内的财报
3. THE Config SHALL 支持配置可选的 `max_count` 字段，用于限制每家公司最多拉取的财报数量，取值为 1 至 10000 之间的正整数
4. WHEN `max_count` 被配置，THE Fetcher SHALL 先按时间范围过滤，再按照报告期由新到旧的顺序截取，最多拉取 `max_count` 条财报
5. IF `start_date` 晚于 `end_date`，THEN THE Config SHALL 返回包含字段名称及错误原因的日期范围错误信息，拒绝启动拉取流程
6. IF `max_count` 配置为小于 1 或大于 10000 的值，THEN THE Config SHALL 返回包含字段名称及合法范围的错误信息，拒绝启动拉取流程
7. IF `start_date` 和 `end_date` 仅指定其中一个，THEN THE Config SHALL 返回包含缺失字段名称的错误信息，拒绝启动拉取流程
8. WHEN `start_date`、`end_date` 和 `max_count` 均未配置，THE Fetcher SHALL 按需求 4 的默认规则处理

---

### 需求 4：默认拉取上一年度财报

**用户故事：** 作为一名投资分析师，当我未指定时间范围时，我希望工具默认拉取上一年度的年报，以便快速获取最近完整财年的数据。

#### 验收标准

1. WHEN 配置中未指定 `start_date` 和 `end_date`，THE Fetcher SHALL 自动将时间范围设定为运行时当前年份的上一自然年度（即 `{运行时当前年份-1}-01-01` 至 `{运行时当前年份-1}-12-31`）
2. IF 配置中仅指定了 `start_date` 或仅指定了 `end_date` 其中之一，THEN THE Config SHALL 返回包含缺失字段名称的错误信息，拒绝启动拉取流程
3. WHEN 配置中未指定 `max_count`，THE Fetcher SHALL 默认最多拉取每家公司 1 份年报
4. WHEN 默认时间范围被应用，THE Fetcher SHALL 在 INFO 级日志中记录实际使用的 `start_date` 和 `end_date` 日期值，便于用户确认

---

### 需求 5：财报类型筛选

**用户故事：** 作为一名投资分析师，我希望能够指定需要拉取的财报类型（年报、半年报、季报），以便按需获取特定类型的财务数据。

#### 验收标准

1. THE Config SHALL 支持配置 `report_types` 字段，允许值为 `annual`（年报）、`semi_annual`（半年报）、`quarterly`（季报）的一个或多个组合
2. WHEN `report_types` 被配置，THE Fetcher SHALL 仅拉取财报类型标识与配置值完全一致的财报
3. WHEN 配置中未指定 `report_types`，THE Fetcher SHALL 默认仅拉取 `annual`（年报）类型的财报
4. IF `report_types` 中包含不支持的类型值或 `report_types` 为空列表，THEN THE Config SHALL 返回包含非法值或"列表不能为空"提示的错误信息，拒绝启动拉取流程
