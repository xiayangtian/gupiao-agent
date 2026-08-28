# AI 中转站通用客户端使用规则 (`ai_client.py`)

## 文件位置
`financial_report_fetcher/ai_client.py`

## 设计目标
提供一个**可迁移、零框架依赖**的 OpenAI-compatible API 封装层，统一中转站访问方式。
复制该文件到任意 Python 项目即可直接使用。

## 配置方式

```bash
# 方式 1：环境变量（推荐）
export AI_API_KEY="sk-xxxxxxxxxxxx"
export AI_BASE_URL="https://xxx.com/v1"     # 可选，有默认值
export AI_MODEL="DeepSeek-V4-Flash"         # 可选，有默认值

# 方式 2：代码传入
client = AIClient(base_url="...", api_key="...", default_model="...")
```

## 支持的能力

| 方法 | 用途 | 适用场景 |
|---|---|---|
| `chat()` | 基础 Chat Completion | 对话、内容生成 |
| `ask()` | 单轮快捷对话 | 一句话提问 |
| `analyze_pdf()` | PDF 内容 + 分析需求 → AI 分析 | 文档分析 |
| `chat_with_context()` | PDF 上下文 + 多轮问答 | 文档交互 QA |
| `estimate_tokens()` | Token 预估 | 控制上下文长度 |

## 结构化输出
支持 OpenAI `response_format` 参数，可以约束模型输出 JSON：

```python
result = client.ask(
    "提取数据：营收、净利润",
    response_format={"type": "json_object"},
    system="请输出 JSON，格式：{'revenue': 数字, 'net_profit': 数字}"
)
```

## 模型选择参考

| 目标 | 推荐模型 |
|---|---|
| 快速响应 / 低消耗 | `DeepSeek-V4-Flash`（默认） |
| 深度分析 / 复杂推理 | `DeepSeek-V4-Pro` / `Opus 5` |
| 长上下文处理 | 根据 API 提供商的上下文长度选择 |

## 迁移到新项目
1. 复制 `ai_client.py` 到目标项目的任意目录
2. 设置 `AI_API_KEY` 环境变量
3. 无其他依赖——`requests` 已为绝大多数项目自带

## 注意事项
- Token 估算（`estimate_tokens`）是粗略值，API 返回的 `usage` 才是准确值
- PDF 超长时会自动截断首尾关键部分，确保核心数据不丢失
- 使用 `analyze_pdf` 时，建议将 `max_chars` 控制在模型上下文窗口的 1/3 以内