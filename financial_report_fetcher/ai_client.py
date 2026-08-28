"""
AIClient — AI 中转站统一客户端
==============================

本文件是整个 AI 分析能力的基础设施层，
职责是封装中转站（OpenAI-compatible API）的所有通信细节。

----------
设计目标：
----------
1. 对外暴露简洁的 Python 接口，隐藏 HTTP 请求、鉴权、重试等细节
2. 支持 Chat 对话、结构化输出（JSON Schema）、PDF 文本分析
3. 零框架依赖——仅需要 requests，复制到任何 Python 项目即可直接使用
4. 全方位的中文注释，方便他人理解和使用

----------
使用方式（快速开始）：
----------
    from ai_client import AIClient

    # 初始化（可自动读取 AI_API_KEY 环境变量）
    client = AIClient()

    # 基础对话
    resp = client.chat([{"role": "user", "content": "你是谁？"}])
    print(resp["content"])

    # PDF 内容分析
    result = client.analyze_pdf(open("report.pdf", "rb").read(),
                                "请提取关键财务数据")
    print(result)

----------
环境变量配置 (推荐)：
----------
    AI_API_KEY=sk-xxxxxxxxxxxxx      # API 密钥（必填）
    AI_BASE_URL=https://xxx/v1       # 端点地址（可选，有默认值）
    AI_MODEL=DeepSeek-V4-Flash       # 默认模型（可选）

----------
模型选择建议：
----------
    需要更多分析能力：DeepSeek-V4-Pro / Opus 5 等模型
    快速响应：DeepSeek-V4-Flash
    本地配置在AIClient的config属性，也可以在请求中为model指定
"""

import json
import os
import re
import time
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any

import requests

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# 默认常量
# ─────────────────────────────────────────────────────────────────────
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL   = "deepseek-v4-flash"  # 默认使用最快模型
_CONFIG_PATHS  = ["config.yaml", "config.yml"]  # 项目根目录配置文件


def _ignore_unwritable_sslkeylog() -> None:
    """忽略不可写的 SSLKEYLOGFILE。

    urllib3 在建立 HTTPS 连接时会尝试把 SSL keylog 写入 SSLKEYLOGFILE 指向的
    文件；若该路径不可写（如沙箱/权限限制），所有出站 HTTPS 请求都会失败
    （Connection aborted / Operation not permitted）。这里在构造 AIClient 时
    探测可写性，不可写则从环境变量移除并告警，保证 AI 请求正常建立连接。
    """
    path = os.environ.get("SSLKEYLOGFILE")
    if not path:
        return
    try:
        # append 模式打开仅探测可写性，不写入内容
        with open(os.path.expanduser(path), "a", encoding="utf-8"):
            pass
        return  # 可写：保留（合法调试场景）
    except OSError:
        logger.warning("SSLKEYLOGFILE 不可写（%s），已忽略以免 HTTPS 连接失败", path)
        os.environ.pop("SSLKEYLOGFILE", None)


def _load_config_file() -> Dict[str, Any]:
    """从项目根目录配置文件读取 AI 配置段。"""
    project_root = Path.cwd()
    for name in _CONFIG_PATHS:
        cfg_path = project_root / name
        if cfg_path.is_file():
            try:
                data = __import__("yaml").safe_load(cfg_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


class AIClient:
    """
    AI 中转站客户端。

    封装了与 OpenAI-compatible API 的所有通信逻辑，支持：
    - 基础对话 (chat)
    - PDF 内容分析 (analyze_pdf)
    - 结构化输出 (response_format)
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        timeout: int = 300,
    ):
        """
        初始化客户端。

        参数优先级：构造参数 > 环境变量 > config.yaml 配置文件 > 编译默认值。
        这样可以做到配置灵活，同时避免在代码里硬编码密钥。

        Args:
            base_url:     API 地址，默认读取 AI_BASE_URL 环境变量或内置默认值
            api_key:      API 密钥，默认读取 AI_API_KEY 环境变量
            default_model: 默认模型名，默认读取 AI_MODEL 环境变量或内置默认值
            timeout:       请求超时时间（秒），默认 300
        """
        # 忽略不可写的 SSLKEYLOGFILE（防止 urllib3 HTTPS 连接被权限拒绝）
        _ignore_unwritable_sslkeylog()

        # 尝试读取配置文件（最低优先级）
        _cfg = _load_config_file()

        # API 基础地址
        self.base_url = (
            base_url
            or os.environ.get("AI_BASE_URL")
            or _cfg.get("ai_base_url")
            or DEFAULT_BASE_URL
        ).rstrip("/")

        # API 密钥
        api_key_val = api_key or os.environ.get("AI_API_KEY") or _cfg.get("ai_api_key") or ""
        if not api_key_val:
            logger.warning(
                "AIClient 未配置 API 密钥，请设置 AI_API_KEY 环境变量或在 config.yaml 中配置 ai_api_key"
            )
        self.api_key = api_key_val

        # 默认模型
        self.default_model = (
            default_model
            or os.environ.get("AI_MODEL")
            or _cfg.get("ai_model")
            or DEFAULT_MODEL
        )

        self.timeout = timeout

        # 创建请求会话
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

        logger.info(
            "AIClient 已初始化 [base_url=%s, model=%s, timeout=%s]",
            self.base_url, self.default_model, self.timeout,
        )

    # ─────────────────────────────────────────────────────────────
    # 核心方法：改造成对话
    # ─────────────────────────────────────────────────────────────

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = 4096,
        response_format: Optional[Dict[str, Any]] = None,
        stream: bool = False,
    ) -> Dict[str, Any]:
        """
        发起 Chat Completion 调用。

        Args:
            messages:   对话消息列表 [{"role": "user", "content": "..."}]
            model:      模型名；默认使用 self.default_model, 如果 preset 不为 None，会被该字段的模型覆盖
            system:     系统提示词（将作为 system role 插入）
            temperature: 生成温度 (0~2)，财务分析建议 0.3 以下
            max_tokens:  最大输出 Tokens 数
            response_format: 结构化输出格式，如 {"type": "json_object"}
            stream:     是否流式输出（暂未实现了）

        Returns:
            API 响应字典，格式：
            {
                "content": "...",         # 模型回复文本
                "finish_reason": "stop",  # 结束原因
                "model": "...",           # 实际使用的模型
                "usage": {                # Token 用量
                    "prompt_tokens": ...,
                    "completion_tokens": ...,
                    "total_tokens": ...,
                },
                "raw": {...}              # API 原始响应（日志调试用）
            }
        """
        # 构建请求体
        request_body = {
            "model": model or self.default_model,
            "messages": list(messages),
            "temperature": temperature,
            "stream": stream,
        }

        # 插入系统提示词（放在消息最前面）
        if system:
            request_body["messages"].insert(0, {
                "role": "system",
                "content": system,
            })

        # 设置 max_tokens
        if max_tokens is not None:
            request_body["max_tokens"] = max_tokens

        # 如果支持结构化输出，按 OpenAI API 标准设置 response_format
        if response_format is not None:
            request_body["response_format"] = response_format

        # 发起请求
        url = f"{self.base_url}/chat/completions"
        start_time = time.time()

        try:
            resp = self._session.post(
                url,
                json=request_body,
                timeout=self.timeout,
            )
            elapsed = time.time() - start_time
            logger.info(
                "chat 请求完成 [model=%s, 耗时=%.1fs, 状态=%s]",
                request_body["model"], elapsed, resp.status_code,
            )

            # 如果 HTTP 状态码不是 2xx，自动触发异常
            resp.raise_for_status()
            data = resp.json()

        except requests.exceptions.Timeout:
            logger.error("chat 请求超时 [model=%s, timeout=%s]", model or self.default_model, self.timeout)
            raise TimeoutError(
                f"AI 请求超时（{self.timeout}秒），请稍后重试"
            )

        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "N/A"
            try:
                detail = exc.response.json() if exc.response is not None else {}
            except Exception:
                detail = {"error": str(exc)}
            logger.error("chat HTTP 错误 [status=%s, detail=%s]", status, detail)
            raise RuntimeError(
                f"AI 服务返回 HTTP {status}：{detail.get('error', {}).get('message', str(exc))}"
            ) from exc

        # 提取响应内容
        try:
            choice = data["choices"][0]
            message = choice.get("message", {})
            content = message.get("content", "")
            # DeepSeek 推理模型可能只产出 reasoning_content、content 为空；
            # 单独保留 reasoning 字段供调用方判断"模型未输出答案"而非"数据不存在"
            reasoning = message.get("reasoning_content", "") or ""
            finish_reason = choice.get("finish_reason", "stop")
            usage = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            logger.error("API 返回格式异常: %s", exc)
            raise ValueError(f"AI 响应解析失败：{exc}") from exc

        logger.info(
            "chat 完成 [tokens: %d 输入 + %d 输出%s]",
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            "，仅含思考过程" if not content.strip() and reasoning.strip() else "",
        )

        return {
            "content": content.strip(),
            "reasoning": reasoning.strip(),
            "finish_reason": finish_reason,
            "model": data.get("model", request_body["model"]),
            "usage": usage,
            "raw": data,
        }

    # ── 简化版：单轮对话 ──────────────────────────────────────────

    def ask(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = 4096,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        最简单的单轮对话方法，直接传文字量产，返回文本。

        示例：
            client.ask("贵州茅台2025年的营收是多少？")

        Args:
            prompt:              用户输入
            system:              系统提示词
            model:               模型名
            temperature:         生成温度
            max_tokens:          最大输出长度
            response_format:     结构化输出格式

        Returns:
            模型回复文本
        """
        result = self.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        return result["content"]

    # ── PDF 分析 ────────────────────────────────────────────────

    def analyze_pdf(
        self,
        pdf_content: str,
        prompt: str,
        *,
        system: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = 4096,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        对 PDF 文本内容进行分析。

        核心思路：
        1. 将 PDF 提取出的文本通过 system 提示词注入上下文
        2. 在同一个 chat 调用中完成分析
        3. 支持结构化输出（通过 response_format）

        Args:
            pdf_content:   PDF 提取的文本内容
            params:        用户的分析需求描述（如 "提取关键财务数据"）
            system:        额外的系统提示词（可选）
            model:         模型名
            temperature:   生成温度
            max_tokens:    最大输出长度
            response_format: 结构化输出格式

        Returns:
            模型回复文本
        """
        # 构建多级提示词
        system_parts = [
            "你是一位专业的金融分析师，擅长阅读和分析上市公司财务报告。",
            "请根据用户提供的财报内容，基于事实回答问题。",
            "如果内容不足以回答，请明确指出哪些数据缺失。",
        ]
        if system:
            system_parts.append(system)

        # 如果内容太长，提示 AI 注意
        if len(pdf_content) > 10000:
            system_parts.append(
                "注意：财报内容较长，请聚焦在关键财务数据和重要信息上。"
            )

        full_system = "\n\n".join(system_parts)

        # 构建用户消息中将 PDF 内容放入用户侧的 context 中
        user_content = (
            f"以下是目标财务报告的内容（请基于此进行分析）：\n\n"
            f"--- 财报内容开始 ---\n"
            f"{pdf_content}\n"
            f"--- 财报内容结束 ---\n\n"
            f"以下是需要你完成的分析任务：\n{prompt}"
        )

        return self.ask(
            prompt=user_content,
            model=model,
            system=full_system,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    # ── 对话式问答（适用于指定 PDF 内容的交互式问答） ────────────

    def chat_with_context(
        self,
        pdf_content: str,
        user_prompt: str,
        history: Optional[List[Dict[str, str]]] = None,
        *,
        system: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = 4096,
    ) -> str:
        """
        基于 PDF 上下文的多轮对话交互式问答。

        和 analyze_pdf 的区别：
        - analyze_pdf 是单次分析，每次都有完整的 PDF 内容
        - chat_with_context 支持历史消息，形成可以追问的对话

        Args:
            pdf_content:   PDF 文本内容（第一次会自动注入到系统提示词）
            user_prompt:   用户当前的问题
            history:       历史对话消息列表（用于多轮对话）
            system:        额外的系统提示词
            model:         模型名
            temperature:   生成温度
            max_tokens:    最大输出长度

        Returns:
            模型回复文本
        """
        # 构建消息列表
        messages = []

        # 系统提示词（含 PDF 上下文）
        system_parts = [
            "你是一个专业的金融分析师，正在阅读一家上市公司的财务报告。",
            "回答应该基于财报内容，事实为基础，简洁、专业。",
        ]
        if system:
            system_parts.append(system)

        # 将 PDF 内容放入系统提示词（无需每次重复发送）
        system_parts.append(
            f"\n[附注] 以下是这份财务报告的完整内容，请在回答时参考：\n\n{pdf_content[:10000]}"
        )
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        # 补齐历史消息
        if history:
            messages.extend(history)

        # 补当前用户问题
        messages.append({"role": "user", "content": user_prompt})

        result = self.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return result["content"]

    # ── 流式对话 ──────────────────────────────────────────────────

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = 4096,
        response_format: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ):
        """发起流式 Chat Completion 调用（SSE），逐个产出事件 dict。

        返回生成器，每个元素为：
            {"type": "delta", "text": str, "reasoning": str}  # 内容/推理增量
            {"type": "done", "answer": str, "reasoning": str,
             "model": str, "usage": dict}                      # 全部完成
            {"type": "error", "error": str}                    # 请求/解析失败

        支持 DeepSeek 推理模型：reasoning_content 单独产出（text 为空）。
        调用方可用首个 delta 到达前的等待期呈现「思考中」状态。
        """
        request_body = {
            "model": model or self.default_model,
            "messages": list(messages),
            "temperature": temperature,
            "stream": True,
        }
        if system:
            request_body["messages"].insert(0, {"role": "system", "content": system})
        if max_tokens is not None:
            request_body["max_tokens"] = max_tokens
        if response_format is not None:
            request_body["response_format"] = response_format
        # function calling：注入工具定义并允许模型按需调用（tool_choice=auto）
        if tools:
            request_body["tools"] = tools
            request_body["tool_choice"] = "auto"

        url = f"{self.base_url}/chat/completions"
        try:
            resp = self._session.post(
                url,
                json=request_body,
                timeout=self.timeout,
                stream=True,
            )
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            yield {"type": "error", "error": f"AI 请求超时（{self.timeout}秒），请稍后重试"}
            return
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "N/A"
            yield {"type": "error", "error": f"AI 服务返回 HTTP {status}，请稍后重试"}
            return

        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        usage: Dict[str, Any] = {}
        resp_model = request_body["model"]
        # 流式 tool_calls 分片累积：delta.tool_calls[index].function.{name,arguments}
        tool_calls_acc: Dict[int, Dict[str, Any]] = {}
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                resp_model = data.get("model", resp_model)
                if data.get("usage"):
                    usage = data["usage"]
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                reasoning = delta.get("reasoning_content")
                if content:
                    text_parts.append(content)
                    yield {"type": "delta", "text": content, "reasoning": reasoning or ""}
                elif reasoning:
                    reasoning_parts.append(reasoning)
                    yield {"type": "delta", "text": "", "reasoning": reasoning}
                # 工具调用分片（index 分组累积 id/name/arguments）
                for tc in delta.get("tool_calls") or []:
                    idx = int(tc.get("index", 0))
                    slot = tool_calls_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
        except Exception as exc:
            yield {"type": "error", "error": f"流式读取失败：{exc}"}
            return

        if tool_calls_acc:
            calls = [
                {"id": v["id"], "name": v["name"], "arguments": v["arguments"]}
                for _k, v in sorted(tool_calls_acc.items())
            ]
            yield {"type": "tool_calls", "tool_calls": calls}
            return

        yield {
            "type": "done",
            "answer": "".join(text_parts).strip(),
            "reasoning": "".join(reasoning_parts),
            "model": resp_model,
            "usage": usage,
        }


    # ── 工具方法 ────────────────────────────────────────────────

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        快速估算字符串的 Tokens 数量。

        规则：中文约 1 token / 1.5 字符，英文约 1 token / 3 字符
        """
        chinese_chars = len(re.findall(r'[一-鿿]', text))
        other_chars = len(text) - chinese_chars
        return int(chinese_chars / 1.5 + other_chars / 3)

    def count_message_tokens(self, messages: List[Dict[str, str]]) -> int:
        """
        计算一组消息的总 Token 数。用于在发送前判断是否需要截断或分块。

        Args:
            messages: 消息列表

        Returns:
            估算的总 token 数
        """
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            total += self.estimate_tokens(content)
            total += 5  # 每条消息的格式开销（role 等）
        return total

    @classmethod
    def from_env(cls) -> "AIClient":
        """从环境变量快速构造客户端（最佳实践），
           等效于 AIClient()"""
        return cls()


class AIError(Exception):
    """AI 相关错误的基类"""


class AITimeoutError(AIError):
    """AI 请求超时"""