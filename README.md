# gp-agent — 中国上市公司财报获取与 AI 分析工具

输入股票代码或名称，自动从巨潮资讯网（CNINFO）抓取年报 / 半年报 / 季报 PDF，
再用 AI 生成财务摘要、风险识别、盈利质量、现金流等多维度分析，并支持针对财报内容自由追问。

## 功能列表

### CLI（`python3 -m financial_report_fetcher`）

- **download**：按股票代码 / 名称抓取年报、半年报、季报 PDF 到本地 `reports/`
  （存储目录可在配置文件中自定义）
- **analyze**：AI 分析财报 PDF，默认输出财务摘要 / 风险识别 / 经营亮点 / 盈利质量 /
  现金流分析五个维度（另有成长性、偿债、营运、治理、股东回报、研发、行业竞争等可选维度），
  结果落盘为 Markdown 与 JSON 双份文件；支持 `--all` 批量分析整个目录
- **chat**：与指定财报交互式问答，可多轮追问

### 股票行情 / 基本面（新增）

- **腾讯免费行情**（无需 key）：实时行情、指数行情、日/周/月 K 线
- **china-stock-mcp 财务/基本面**（30 个 MCP 工具）：公司基本信息、三大报表、
  财务指标、资金流、股东/高管/解禁/分红、筹码分布、研报、估值、技术指标、宏观数据

### Web UI（FastAPI，浏览器访问）

- 股票名称 / 代码**自动补全**（输入 ≥1 字符即触发）
- **财报列表**：按报告期展示，本地已下载的财报带 ✓ 标记
- **PDF 预览**：浏览器内嵌 iframe 直接查看；首次预览自动下载（下载期间 iframe 暂时空白）
- **AI 多维分析**：12 个分析维度可勾选（含「全选 / 清空」快捷按钮），默认勾选
  财务摘要 / 风险识别 / 经营亮点 / 盈利质量 / 现金流五个；启用 RAG 后分析上下文由
  「截断全文」升级为「按维度定向检索片段」（覆盖年报任意章节），检索为空时自动回退截断全文；
  后台任务并发执行（默认 3 个并行，可用环境变量 `TASK_MAX_WORKERS` 调整），可同时
  分析多组财报；分析中可随时点击「⏹ 停止分析」（当前维度完成后生效）；结果分维度
  卡片展示；分析结果自动双份落盘服务器 `reports/analysis/`（.md + .json），页面展示保存路径
- **自由问答**：针对所选财报提问，保留最近 4 轮会话上下文
- **智能问答**（RAG 通用问答）：答案流式输出（SSE），模型响应前显示「思考中」状态，
  可随时点击「⏹ 停止」中断生成（已生成部分自动保存）；多个会话可同时发起请求
  （各自独立流式、互不阻塞）；历史会话自动落盘保存，支持开启新会话与跳转历史会话继续追问
- **股票行情 API**：`/api/quote`（实时）、`/api/quote/kline`（K线）、`/api/quote/index`（指数）
- **财务/基本面 API**：`/api/stock/info`、`/api/stock/financials`、
  通用 MCP 调用 `POST /api/stock/mcp/call`（body: `{"tool": "...", "arguments": {...}}`）
- 未配置 `AI_API_KEY` 时界面顶部显示提醒，分析 / 问答按钮禁用并给出引导

## 安装

```bash
pip install -r requirements.txt
```

## 敏感信息防护（pre-commit 钩子）

仓库内置 `.githooks/pre-commit` 钩子：提交前自动扫描暂存区中的
API Key / Token、硬编码凭证、大陆手机号、身份证号、个人邮箱等敏感信息，命中即阻止提交。

clone 后启用一次：

```bash
git config core.hooksPath .githooks
```

说明：
- 只扫描 `git diff --cached` 中本次新增的行，不影响删除行与上下文
- 占位符（如 `sk-xxxxxxxxxxxx`）、f-string 模板（如 `Bearer {self.api_key}`）、示例域名（`example.com`）不会误报
- 真实密钥请放入 `config.yaml`（已在 `.gitignore` 中）或环境变量，勿写入代码

## 配置环境变量

> 配置文件模板见 `config.example.yaml`（完整结构，密钥为占位符）：
> `cp config.example.yaml config.yaml` 后填入真实 `ai_api_key` 即可使用；
> `config.yaml` 已在 `.gitignore` 中，不会提交到仓库。

| 变量 | 必填 | 说明 |
|---|---|---|
| `AI_API_KEY` | 是 | AI 分析 / 问答必填，不配置则仅可下载与预览财报 |
| `AI_BASE_URL` | 否 | OpenAI-compatible 中转站地址；缺省为内置（内网）中转地址，外部环境请显式配置 |
| `AI_MODEL` | 否 | 默认 `DeepSeek-V4-Flash`；深度分析可换 `DeepSeek-V4-Pro` |
| `CHINA_STOCK_MCP_CMD` | 否 | china-stock-mcp 启动命令，默认 `uvx china-stock-mcp`；可用 `CHINA_STOCK_MCP_CMD="/path/to/python -m china_stock_mcp"` 指定本地安装路径 |

AI 配置优先级为：显式构造参数 > 环境变量 > `config.yaml` > 代码默认值。
`start.sh` 不会读取、改写或回显 API Key，配置由 Python 应用层统一加载。

```bash
export AI_API_KEY="sk-xxxxxxxxxxxx"
export AI_BASE_URL="https://xxx.com/v1"   # 可选
export AI_MODEL="DeepSeek-V4-Flash"       # 可选
```

## CLI 用法

所有命令通过模块入口执行，先查看帮助确认参数：

```bash
python3 -m financial_report_fetcher --help
```

### 1. 下载财报

下载需要一份 YAML / JSON 配置文件（参考项目中的 `config_600900.yaml`）：

```yaml
# config_600900.yaml
storage_dir: reports  # 必填，PDF 保存目录
companies:
  - ticker: "600900"
    name: "长江电力"
report_types:
  - annual        # annual=年报 / semi_annual=半年报 / quarterly=季报
start_date: "2023"      # 支持 "YYYY" 或 "YYYY-MM-DD"
end_date: "2025"
max_count: 3
```

执行下载：

```bash
python3 -m financial_report_fetcher download --config config_600900.yaml
```

PDF 保存到配置的 `storage_dir` 目录（示例中为 `reports/`），
命名形如 `长江电力_600900_年报_2025.pdf`；已存在的文件自动跳过。

### 2. AI 分析财报

```bash
# 分析单份 PDF，结果保存为 reports/analysis/ 下的分析报告（.md + .json 各一份）
python3 -m financial_report_fetcher analyze --pdf "reports/长江电力_600900_年报_2025.pdf"

# 批量分析 reports/ 下全部 PDF
python3 -m financial_report_fetcher analyze --all

# 自定义 PDF 目录与分析结果输出目录
python3 -m financial_report_fetcher analyze --all --dir reports --output reports/analysis
```

分析报告默认落在 `reports/analysis/`，每个分析结果同时输出 Markdown 与 JSON 两份文件。

### 3. 与财报交互问答

```bash
python3 -m financial_report_fetcher chat --pdf "reports/长江电力_600900_年报_2025.pdf"
```

### 3.5 股票行情（腾讯免费 API）

```bash
# 实时行情（批量）
python3 -m financial_report_fetcher quote realtime --symbols 600519,000001

# 指数行情
python3 -m financial_report_fetcher quote index --codes sh000001,sz399001

# 历史 K 线（日/周/月，支持前/后/不复权）
python3 -m financial_report_fetcher quote kline --symbol 600519 --period day --count 10
```

### 3.6 财务 / 基本面（china-stock-mcp）

```bash
# 公司基本信息
python3 -m financial_report_fetcher mcp info --symbol 600519

# 关键财务指标（归母净利润、ROE、周转率等，历史 20+ 年）
python3 -m financial_report_fetcher mcp financials --symbol 600519

# 三大报表 / 资金流 / 股东 / 业绩预测 / 新闻
python3 -m financial_report_fetcher mcp balance-sheet --symbol 600519
python3 -m financial_report_fetcher mcp income --symbol 600519
python3 -m financial_report_fetcher mcp cashflow --symbol 600519
python3 -m financial_report_fetcher mcp fund-flow --symbol 600519
python3 -m financial_report_fetcher mcp shareholders --symbol 600519
python3 -m financial_report_fetcher mcp forecast --symbol 600519
python3 -m financial_report_fetcher mcp news --symbol 600519

# 列出全部 30 个工具 / 通用调用任意工具
python3 -m financial_report_fetcher mcp tools
python3 -m financial_report_fetcher mcp call --tool get_realtime_data --args '{"symbol":"600519"}'
```

> china-stock-mcp 依赖 akshare（约 300MB），首次 `uvx china-stock-mcp` 会自动安装；
> PyPI 下载慢时可设置 `UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`。

### 3.7 RAG 知识库（可选）

将本地年报 PDF 原文与 AI 分析报告切块入库，提供跨报告通用问答，
并增强单报告对话（带引用来源）。

```bash
# 建立索引（首次运行自动下载 bge 中文 embedding 模型，约 100MB）
python3 -m financial_report_fetcher rag ingest --all

# 查看索引状态
python3 -m financial_report_fetcher rag status

# 通用 RAG 问答（可 --ticker / --year 限定范围）
python3 -m financial_report_fetcher rag chat --ticker 600900

# 单份 PDF（连带其分析报告）
python3 -m financial_report_fetcher rag ingest --pdf "reports/长江电力_600900_年报_2025.pdf"
```

Web 端：`POST /api/chat/stream` 流式通用对话（SSE，携带 `session_id` 自动保存历史）、
`GET/POST /api/chat/sessions` 会话列表 / 新建、`POST /api/chat` 非流式通用对话（兼容）；
问答默认启用 MCP 工具（`rag.mcp_tools: true`，超时/轮数/白名单见 `config.example.yaml`）；
MCP 带熔断保护（连续失败自动暂停使用、冷却后自动探测恢复）与状态检测
（`GET /api/mcp/status`、`POST /api/mcp/diagnose`，RAG 页可视化）、
`GET /api/rag/status` 状态、
`POST /api/rag/ingest` 后台重建索引；原"针对此报告的对话"在已索引时自动走 RAG 并返回引用。

需在 `config.yaml` 中开启 `rag.enabled: true`（模板见 `config.example.yaml`）。

**RAG 增强多维度分析**：启用 `rag.enhanced_analysis: true`（默认开启）后，`analyze` 与 Web 分析
任务会先自动摄取报告（幂等），再对每个维度按 `ANALYSIS_TEMPLATES` 中的检索策略（查询词 + 章节定向）
从向量库检索片段作为分析上下文；未启用 / 未索引 / 检索为空时回退原有截断全文，分析结果格式不变。
`rag.analysis_dimensions` 可配置分析页默认勾选的维度（缺省为内置 5 个默认维度）。

**自适应重排序（Rerank，可选）**：启用 `rag.rerank: true` 后，检索先放宽召回
（`rag.rerank_candidates: 30`），仅当首轮检索质量不佳 / 不确定时才用本地
cross-encoder 模型（`BAAI/bge-reranker-base`，首次运行自动下载约 1GB）精排取前
`top_k`；质量好时跳过精排以节省延迟。判定阈值 `rag.rerank_score_threshold`
（默认 0.5）与 `rag.rerank_margin_threshold`（默认 0.05）可配置。
需安装可选依赖（`requirements.txt` 含 `sentence-transformers`）；
未安装 / 模型加载失败时自动回退纯向量检索（零回归）。

### 4. 指定 AI 模型

```bash
python3 -m financial_report_fetcher --model DeepSeek-V4-Pro analyze \
  --pdf "reports/长江电力_600900_年报_2025.pdf"
```

## 启动 Web 界面

推荐使用仓库自带的启停脚本（默认监听 `127.0.0.1:8000`，端口可用 `PORT` 环境变量覆盖）：

```bash
./start.sh                 # 后台启动（nohup，适合普通终端）
./start.sh --foreground    # 前台启动（-f；适合 Codex 桌面会话等
                           #   命令结束后会回收后台子进程的环境）
./stop.sh                  # 停止服务（优雅终止，含 PID 安全校验）
./restart.sh [--foreground|-f]   # 重启（透传前台模式参数）
```

- 两种模式都会把 PID 写入 `logs/app.pid`、对服务做健康检查，`stop.sh` / `restart.sh` 均通用；
- 前台模式日志直接输出到当前终端/会话，按 `Ctrl+C` 或另开终端执行 `./stop.sh` 均可停止；
- 注意：在 Codex 桌面等受限执行环境中，`stop.sh` 内对进程的 `kill` 需要非沙箱权限
  （普通终端无此限制）。

浏览器打开 <http://127.0.0.1:8000>，在搜索框输入「长江电力」或「600900」即可开始使用。

## 运行测试

```bash
python3 -m pytest -q
```

## 目录结构

```
financial_report_fetcher/   # 核心库（数据源 / 下载 / 分析 / AI 客户端）
webapp/                     # Web 层（FastAPI 路由、任务管理、自动补全、静态前端）
tests/                      # 测试（unit / integration / property）
reports/                    # 下载的财报 PDF
reports/analysis/           # AI 分析结果（.md + .json）
```

## 免责说明

本项目数据来源为巨潮资讯网（http://www.cninfo.com.cn），仅供学习研究使用，
不构成任何投资建议。
