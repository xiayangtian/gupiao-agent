"""
china-stock-mcp 的 MCP stdio 客户端封装

- 通过 Model Context Protocol (MCP) 连接外部 china-stock-mcp 服务器
- 暴露 30 个工具：历史/实时行情、三大报表、财务指标、资金流、
  股东/高管/解禁/分红、筹码分布、研报、估值、技术指标、宏观数据等
- 实现 worker 线程 + 事件循环，提供同步 call_tool() 接口，
  CLI 与 FastAPI（线程池）都能安全复用同一个长会话；
  会话断开/调用失败时自动重建并重试一次。

服务器启动方式（按优先级）：
1. 环境变量 CHINA_STOCK_MCP_CMD —— 完整命令行，如
   "uvx china-stock-mcp" 或 "/tmp/stockmcp-venv/bin/python -m china_stock_mcp"
2. 默认 "uvx china-stock-mcp"（需要 uv，PyPI 可加镜像 UV_INDEX_URL）
"""

import asyncio
import logging
import os
import shlex
import tempfile
import threading
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

_DEFAULT_CMD = ["uvx", "china-stock-mcp"]


def _stockmcp_venv_root() -> str:
    """本地持久 venv 根目录（绕开 uvx 缓存问题的首选启动方式）"""
    return os.path.join(os.path.expanduser("~/.local/share"), "stockmcp-venv")


def _find_local_venv_command() -> Optional[List[str]]:
    """本地已安装 china-stock-mcp 的 venv 存在时返回其启动命令；否则 None"""
    py = os.path.join(_stockmcp_venv_root(), "bin", "python")
    if os.path.exists(py):
        return [py, "-m", "china_stock_mcp"]
    return None


def _resolve_server_command() -> List[str]:
    """解析服务器启动命令：环境变量 > 本地持久 venv > 默认 uvx。

    uvx 依赖 uv 缓存/工具目录，受限环境（HOME 只读等）下无法启动；
    本地 venv 方式完全绕开 uv，更稳定。
    """
    raw = os.environ.get("CHINA_STOCK_MCP_CMD", "").strip()
    if raw:
        parts = shlex.split(raw)
        if parts:
            return parts
    local = _find_local_venv_command()
    if local:
        return local
    return list(_DEFAULT_CMD)


def _tool_to_dict(tool: Any) -> Dict[str, Any]:
    """把 mcp.types.Tool 转 dict：名称/描述 + 原生 inputSchema（供 function calling 用）"""
    out: Dict[str, Any] = {
        "name": tool.name,
        "description": tool.description or "",
    }
    schema = getattr(tool, "inputSchema", None)
    if schema:
        out["input_schema"] = schema
    return out


def _is_writable_dir(path: str) -> bool:
    """探测目录是否可写（可创建并删除探测文件）"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".mcp_wtest")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(probe)
        return True
    except OSError:
        return False


def _pick_uv_cache_dir(cache_dir: str, alt_dir: str) -> Optional[str]:
    """~/.cache/uv 不可写时返回可写替代目录（uvx 启动需要），否则 None。"""
    if _is_writable_dir(cache_dir):
        return None
    try:
        os.makedirs(alt_dir, exist_ok=True)
    except OSError:
        return None
    return alt_dir if _is_writable_dir(alt_dir) else None


def _build_server_env() -> Dict[str, str]:
    """构造 MCP 子进程环境：继承当前环境；uv 缓存/工具目录不可写时把
    UV_CACHE_DIR / UV_TOOL_DIR 指到可写临时目录（否则 uvx 无法启动服务器进程）。"""
    env = dict(os.environ)
    if not env.get("UV_CACHE_DIR"):
        alt = _pick_uv_cache_dir(
            os.path.expanduser("~/.cache/uv"),
            os.path.join(tempfile.gettempdir(), f"uv-cache-{os.getuid()}"),
        )
        if alt:
            env["UV_CACHE_DIR"] = alt
            logger.warning("uv 缓存不可写，MCP 子进程 UV_CACHE_DIR 指向：%s", alt)
    if not env.get("UV_TOOL_DIR"):
        alt = _pick_uv_cache_dir(
            os.path.expanduser("~/.local/share/uv/tools"),
            os.path.join(tempfile.gettempdir(), f"uv-tools-{os.getuid()}"),
        )
        if alt:
            env["UV_TOOL_DIR"] = alt
            logger.warning("uv 工具目录不可写，MCP 子进程 UV_TOOL_DIR 指向：%s", alt)
    return env


def _extract_text(result: Any) -> str:
    """从 MCP call_tool 结果中提取纯文本内容。"""
    chunks = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            chunks.append(text)
    return "\n".join(chunks)


class StockMCPClient:
    """
    china-stock-mcp 客户端（同步接口 + 长会话复用）。

    用法::

        from financial_report_fetcher.market.mcp_client import StockMCPClient
        client = StockMCPClient()
        tools = client.list_tools()
        info = client.call_tool("get_stock_basic_info", {"symbol": "600519"})
    """

    def __init__(self, command: Optional[List[str]] = None,
                 connect_timeout: float = 120.0,
                 call_timeout: float = 90.0) -> None:
        self._command = command if command is not None else _resolve_server_command()
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        # worker 线程与事件循环
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # 会话状态（仅在工作线程内访问）
        self._session: Optional[ClientSession] = None
        self._ctx_stack: List[Any] = []
        self._closed = False

    # ─────────────────────────────────────────────────────────────
    # 生命周期：worker 线程 + 事件循环
    # ─────────────────────────────────────────────────────────────

    def _ensure_running(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._loop.is_running():
            return self._loop
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._run_loop, args=(loop,), daemon=True,
                name="stock-mcp-client",
            )
            thread.start()
            self._loop = loop
            self._thread = thread
        return loop

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _submit(self, coro, timeout: float):
        loop = self._ensure_running()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except Exception:
            future.cancel()
            raise

    # ─────────────────────────────────────────────────────────────
    # 会话管理（仅在工作线程内执行）
    # ─────────────────────────────────────────────────────────────

    async def _open_session(self) -> ClientSession:
        """启动服务器进程并完成 MCP 握手，返回可用会话。"""
        params = StdioServerParameters(
            command=self._command[0],
            args=self._command[1:],
            env=_build_server_env(),
        )
        read_ctx = stdio_client(params)
        read_stream, write_stream = await asyncio.wait_for(
            read_ctx.__aenter__(), timeout=self._connect_timeout
        )
        session_ctx = ClientSession(read_stream, write_stream)
        session = await asyncio.wait_for(
            session_ctx.__aenter__(), timeout=self._connect_timeout
        )
        await asyncio.wait_for(session.initialize(), timeout=self._connect_timeout)
        # 记录上下文栈，便于按顺序关闭
        self._ctx_stack = [read_ctx, session_ctx]
        self._session = session
        logger.info("china-stock-mcp 会话已建立：%s", " ".join(self._command))
        return session

    async def _close_session(self) -> None:
        """关闭当前会话与子进程（按倒序退出上下文）。"""
        ctx_stack, self._ctx_stack = self._ctx_stack, []
        self._session = None
        for ctx in reversed(ctx_stack):
            try:
                await asyncio.wait_for(ctx.__aexit__(None, None, None), timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 MCP 上下文异常：%s", exc)

    async def _get_session(self) -> ClientSession:
        if self._session is not None:
            return self._session
        return await self._open_session()

    # ─────────────────────────────────────────────────────────────
    # 公开接口（同步，可在任意线程调用）
    # ─────────────────────────────────────────────────────────────

    def list_tools(self, timeout: Optional[float] = None) -> List[Dict[str, str]]:
        """列出服务器全部工具（名称 + 描述）。"""
        timeout = timeout or self._call_timeout

        async def _impl():
            for attempt in (0, 1):
                session = await self._get_session()
                try:
                    tools = await asyncio.wait_for(session.list_tools(), timeout=timeout)
                    return [_tool_to_dict(t) for t in tools.tools]
                except Exception as exc:
                    logger.warning("list_tools 第 %d 次失败：%s", attempt + 1, exc)
                    await self._close_session()
                    if attempt == 1:
                        raise
            raise RuntimeError("list_tools 失败")

        return self._submit(_impl(), timeout + 30)

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None,
                  timeout: Optional[float] = None) -> str:
        """
        调用 MCP 工具，返回文本结果。

        参数
        ----
        name : str
            工具名，如 "get_stock_basic_info" / "get_financial_metrics"。
        arguments : dict
            工具参数，如 {"symbol": "600519", "output_format": "json"}。
        timeout : float
            单次调用超时（秒），默认 90s。

        返回
        ----
        str
            工具返回的文本内容（JSON 或 markdown 表格）。

        异常
        ----
        RuntimeError / mcp 异常：会话重建后仍失败时抛出。
        """
        timeout = timeout or self._call_timeout
        args = dict(arguments or {})

        async def _impl():
            for attempt in (0, 1):
                session = await self._get_session()
                try:
                    result = await asyncio.wait_for(
                        session.call_tool(name, args), timeout=timeout
                    )
                    return _extract_text(result)
                except Exception as exc:
                    logger.warning("call_tool(%s) 第 %d 次失败：%s", name, attempt + 1, exc)
                    await self._close_session()
                    if attempt == 1:
                        raise
            raise RuntimeError(f"call_tool({name}) 失败")

        return self._submit(_impl(), timeout + 30)

    def close(self) -> None:
        """关闭会话并停止 worker 线程。"""
        self._closed = True
        if self._loop is not None:
            async def _shutdown():
                await self._close_session()

            try:
                asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=15)
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 MCP 客户端异常：%s", exc)
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                if self._thread is not None:
                    self._thread.join(timeout=5)
                self._loop = None
                self._thread = None
