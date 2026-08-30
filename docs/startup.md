# 安装、配置与启动指南

本文介绍如何在 Linux、macOS 和 Windows PowerShell 环境中安装依赖、创建
`config.yaml`、启动 Web 服务以及排查常见问题。

## 1. 环境要求

- 建议使用 Python 3.12。
- 所有命令均在项目根目录执行。
- 默认监听地址为 `127.0.0.1:8000`。
- 健康检查地址为 <http://127.0.0.1:8000/api/health>。

## 2. 创建虚拟环境并安装依赖

使用项目独立的虚拟环境可以避免污染系统 Python。

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Windows 上 `requirements.txt` 含中文注释。若 pip 报
`UnicodeDecodeError: 'gbk' codec can't decode ...`，请设置
`$env:PYTHONUTF8 = "1"` 后重试。该变量只影响当前 PowerShell 会话。

## 3. 创建并填写 config.yaml

项目不会自动生成 `config.yaml`，首次使用时需要手动从模板复制。

### Linux / macOS

```bash
cp config.example.yaml config.yaml
```

### Windows PowerShell

```powershell
Copy-Item config.example.yaml config.yaml
```

然后编辑 `config.yaml`，至少将 `ai_api_key` 替换为真实密钥；
`ai_base_url`、`ai_model` 和 `rag` 等配置按需调整。完整字段和说明见
[`config.example.yaml`](../config.example.yaml)。`config.yaml` 已加入 `.gitignore`，
不会提交到仓库。

`config.yaml` 是项目的全局 AI / RAG 配置；CLI 下载命令使用的
`config_600900.yaml` 一类文件是下载任务配置，两者用途不同。

| 变量 | 必填 | 说明 |
|---|---|---|
| `AI_API_KEY` | 是 | AI 分析 / 问答必填；不配置则仅可下载与预览财报 |
| `AI_BASE_URL` | 否 | OpenAI-compatible 中转站地址；外部环境建议显式配置 |
| `AI_MODEL` | 否 | 默认 `DeepSeek-V4-Flash`；深度分析可换 `DeepSeek-V4-Pro` |
| `CHINA_STOCK_MCP_CMD` | 否 | china-stock-mcp 启动命令，默认 `uvx china-stock-mcp` |

AI 配置优先级为：显式构造参数 > 环境变量 > `config.yaml` > 代码默认值。
配置由 Python 应用层统一加载，启动脚本不会读取、改写或回显 API Key。

也可以使用环境变量临时覆盖配置。

Linux / macOS：

```bash
export AI_API_KEY="sk-xxxxxxxxxxxx"
export AI_BASE_URL="https://xxx.com/v1"   # 可选
export AI_MODEL="DeepSeek-V4-Flash"       # 可选
```

Windows PowerShell：

```powershell
$env:AI_API_KEY = "sk-xxxxxxxxxxxx"
$env:AI_BASE_URL = "https://xxx.com/v1"   # 可选
$env:AI_MODEL = "DeepSeek-V4-Flash"       # 可选
```

PowerShell 中以上变量只在当前终端会话有效。通常直接维护本地
`config.yaml` 更方便；环境变量适合临时覆盖配置。

## 4. 启动 Web 服务

### Linux / macOS

推荐使用仓库自带的启停脚本。端口可通过 `PORT` 环境变量覆盖。

```bash
./start.sh                       # 后台启动，日志写入 logs/uvicorn.log
./start.sh --foreground          # 前台启动，也可使用 -f
./stop.sh                        # 停止服务
./restart.sh [--foreground|-f]   # 重启服务
```

两种启动模式都会将 PID 写入 `logs/app.pid` 并执行健康检查。前台模式日志
直接显示在终端，可按 `Ctrl+C` 停止，也可在另一终端执行 `./stop.sh`。

在 Codex 桌面等受限执行环境中，`stop.sh` 内对进程的 `kill` 可能需要非沙箱
权限，普通终端无此限制。

### Windows PowerShell

仓库当前提供的是 Bash 启停脚本；原生 PowerShell 环境请使用以下命令。

#### 前台启动

推荐用于本地开发。日志直接显示在终端，按 `Ctrl+C` 停止。

```powershell
.\.venv\Scripts\python.exe -m uvicorn webapp.server:app --host 127.0.0.1 --port 8000
```

#### 后台启动

日志和 PID 均保存在 `logs/`。

```powershell
New-Item -ItemType Directory -Force logs | Out-Null
$process = Start-Process `
  -FilePath ".\.venv\Scripts\python.exe" `
  -ArgumentList "-m", "uvicorn", "webapp.server:app", "--host", "127.0.0.1", "--port", "8000" `
  -RedirectStandardOutput ".\logs\uvicorn.log" `
  -RedirectStandardError ".\logs\uvicorn-error.log" `
  -WindowStyle Hidden `
  -PassThru
$process.Id | Set-Content ".\logs\app.pid" -NoNewline
```

#### 检查服务

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
Get-Content .\logs\uvicorn-error.log -Tail 50   # 启动失败时查看
```

#### 停止后台服务

```powershell
$servicePid = [int](Get-Content ".\logs\app.pid" -Raw)
Stop-Process -Id $servicePid
Remove-Item ".\logs\app.pid" -ErrorAction SilentlyContinue
```

## 5. 访问页面

浏览器打开 <http://127.0.0.1:8000>，在搜索框输入“长江电力”或“600900”
即可开始使用。

## 6. 常见问题

### 端口 8000 已被占用

Windows PowerShell：

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
```

可停止占用进程，或将启动命令中的 `8000` 改为其他端口。Linux / macOS 使用
启动脚本时也可以临时指定端口，例如 `PORT=8001 ./start.sh`。

### Windows 命令中没有 python3

README 中的 CLI 示例使用 `python3`。Windows 未激活虚拟环境时，请替换为：

```powershell
.\.venv\Scripts\python.exe -m financial_report_fetcher --help
```

### 服务启动失败

- 确认已创建 `.venv` 并成功安装 `requirements.txt`。
- 确认命令在项目根目录执行。
- 后台启动时检查 `logs/uvicorn-error.log`。
- 调用 `/api/health` 确认服务是否已就绪。
