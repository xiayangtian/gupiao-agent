"""webapp.server — FastAPI 应用与 REST API

对外 API（详见设计文档 3.2）：
- GET  /api/health                            AI Key 与索引就绪状态
- GET  /api/companies?q=                     自动补全
- GET  /api/companies/{code}/reports        财报列表（含本地已下载标记）
- GET  /api/reports/{code}/{period}.pdf     PDF 文件流（iframe 预览）
- POST /api/reports/{code}/{period}/analyze 启动分析任务
- GET  /api/tasks/{task_id}                 任务状态轮询
- POST /api/reports/{code}/{period}/chat    对财报自由问答

设计要点：组件为模块级单例（测试可替换）；分析为串行后台任务；
问答会话保存在内存 dict（重启即清）。
"""

import asyncio
import datetime as dt
import json
import inspect
import logging
import logging.handlers
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import requests
from pydantic import BaseModel, Field, model_validator

from financial_report_fetcher.ai_client import AIClient
from financial_report_fetcher.analysis_ai import build_progressive_pipeline
from financial_report_fetcher.analysis_pipeline import (
    AnalysisPipelineRequest,
    analysis_output_stem,
)
from financial_report_fetcher.analyzer import (
    ANALYSIS_TEMPLATES,
    ReportAnalyzer,
    clean_analysis_payload,
)
from financial_report_fetcher.datasource import CNINFODatasource
from financial_report_fetcher.downloader import ReportDownloader
from financial_report_fetcher.models import DownloadStatus, ReportMeta, ReportType
from financial_report_fetcher.report_identity import build_report_id
from financial_report_fetcher.market import market_data_mcp, stock_mcp, tencent_quote
from financial_report_fetcher.rag.analysis import RagAnalysis
from financial_report_fetcher.rag.mcp_tools import (
    DISABLED_MCP_TOOL_NAMES,
    WEB_SEARCH_TOOL,
    build_tool_defs,
    to_openai_tools,
)
from financial_report_fetcher.rag.config import RagConfig
from financial_report_fetcher.rag.embedding import LocalEmbedder
from financial_report_fetcher.rag.ingest import IngestionService
from financial_report_fetcher.rag.qa import (
    SUPPLEMENT_REASON_MAX_CHARS,
    SUPPLEMENT_REQUEST_TOOL,
    SUPPLEMENT_REQUEST_TOOL_NAME,
    RagQA,
)
from financial_report_fetcher.rag.store import RagStore
from financial_report_fetcher.rag.web_search import TavilyWebSearch

from .autocomplete import StockIndex
from .chat_evidence import EvidenceNormalizer
from .chat_facts import FactNormalizer, detect_conflicts
from .chat_models import SUPPLEMENT_MAX_CANDIDATES, AnswerRun, Fact, IndustryRef, IntentDecision, Scope, ToolPolicy
from .chat_policy import IntentRouter, ToolAvailability, ToolPolicyResolver
from .chat_verifier import ClaimVerifier
from .chat_scope import ScopeRequest, ScopeResolver
from .chat_store import ChatStore
from .chat_evaluation import FAILURE_CODES
from .research_export import ExportValidationError, ResearchExporter
from .research_memory import ResearchMemoryStore, artifact_evidence_ids, run_evidence_ids
from .research_workspace import ResearchWorkspaceQuery, ResearchWorkspaceStore
from .research_agent import ResearchAgent
from .research_executor import ResearchExecutor
from .research_models import ResearchRun
from .chat_supplement import (
    SupplementCandidate,
    SupplementCandidateResolver,
    SupplementExecutor,
    SupplementOutcome,
    SupplementRegistry,
    SupplementRequest,
)
from .mcp_guard import McpCircuitBreaker
from .history import (
    analyzed_periods_for_code,
    build_flat_history,
    get_analysis_detail,
    retire_superseded_analysis_files,
)
from .tasks import TaskManager

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
ANALYSIS_DIR = os.path.join(REPORTS_DIR, "analysis")

app = FastAPI(title="财报分析工具")

# 服务进程启动时间（用于判断服务是否加载了最新代码）
SERVER_STARTED_AT = dt.datetime.now()

# ── 共享组件（模块级单例；测试用 monkeypatch 替换同名模块变量）──
datasource = CNINFODatasource()
stock_index = StockIndex(datasource)
ai_client = AIClient()
analyzer = ReportAnalyzer(ai_client)
progressive_pipeline = None
downloader = ReportDownloader()
task_manager: Optional[TaskManager] = None
_task_manager_lifecycle_lock = threading.Lock()
chat_sessions: Dict[str, List[Dict[str, str]]] = {}
# 单报告会话内存表并发读改写锁
_chat_lock = threading.Lock()
# 智能问答历史会话（JSON 文件持久化，data/chat_sessions.json）
chat_store = ChatStore()
# M4 sidecars contain only derived workspace metadata and explicit memory entries;
# immutable AnswerRun/ResearchRun records remain in ChatStore.  The workspace reads
# active decision memories from the same sidecar to expose them to keyword search.
research_memory = ResearchMemoryStore()
research_workspace = ResearchWorkspaceStore(chat_store, memory_path=research_memory.path)
# Explicit-memory writes and the session-deletion count share one coordination
# lock.  Lock order is `_research_memory_lock` -> ChatStore lock; no code path may
# hold the ChatStore lock (or any store lock) and then wait for this lock.
_research_memory_lock = threading.RLock()
QUALITY_SUMMARY_PATH = os.path.join(BASE_DIR, "data", "research_quality_summary.json")


def _startup_task_manager() -> None:
    """Create the production task store when the application starts."""
    global task_manager
    with _task_manager_lifecycle_lock:
        if task_manager is None:
            task_manager = TaskManager()


def _shutdown_task_manager() -> None:
    """Wait for task workers before closing their SQLite connection."""
    global task_manager
    with _task_manager_lifecycle_lock:
        manager = task_manager
        shutdown = getattr(manager, "shutdown", None)
        if shutdown is not None:
            shutdown()
        if task_manager is manager:
            task_manager = None


app.router.add_event_handler("startup", _startup_task_manager)
app.router.add_event_handler("shutdown", _shutdown_task_manager)


# ── RAG 知识库（惰性初始化；未配置 rag.enabled 时保持 None）──
rag_store = None
rag_service = None
rag_qa = None


# MCP 工具定义缓存（进程内只尝试一次；不可用时保持 None，问答不注入假工具）
_mcp_tool_defs_cache: Optional[List[Dict[str, Any]]] = None
_mcp_tool_defs_ready: bool = False
_mcp_tool_input_schemas: Dict[str, Dict[str, Any]] = {}
# MCP 调用熔断器：连续失败暂停使用，冷却后自动探测恢复
mcp_breaker = McpCircuitBreaker()
_mcp_diagnose_cache: Dict[str, Any] = {}
_mcp_tool_health: Dict[str, Dict[str, Any]] = {}
_mcp_tool_health_lock = threading.Lock()


def _record_mcp_tool_health(name: str, ok: bool, message: str) -> None:
    """保存每个问答 MCP 工具最近一次执行结果，供状态页排障。"""
    with _mcp_tool_health_lock:
        _mcp_tool_health[name] = {
            "ok": ok,
            "message": str(message)[:200],
            "checked_at": int(time.time()),
        }


def _mcp_tool_defs() -> Optional[List[Dict[str, Any]]]:
    """返回问答 MCP 的实时工具定义；清单不可用时不注入旧工具。"""
    global _mcp_tool_defs_cache, _mcp_tool_defs_ready, _mcp_tool_input_schemas
    if not mcp_breaker.allow():
        return None  # 熔断冷却中：不注入 MCP 工具（纯 RAG，避免持续失败）
    if _mcp_tool_defs_ready:
        return _mcp_tool_defs_cache
    _mcp_tool_defs_ready = True  # 进程生命周期内只探测一次
    try:
        cfg = RagConfig.load()
        whitelist = cfg.mcp_tool_whitelist
        timeout = cfg.mcp_tool_timeout
    except Exception:
        whitelist, timeout = [], 30
    try:
        listed = market_data_mcp.list_tools(timeout=timeout)
    except Exception:
        logger.warning("问答 MCP 工具清单获取失败，不注入工具")
        listed = []
    _mcp_tool_input_schemas = {
        str(tool.get("name")): tool.get("input_schema")
        for tool in listed
        if tool.get("name") and isinstance(tool.get("input_schema"), dict)
    }
    _mcp_tool_defs_cache = (
        build_tool_defs(lambda: listed, whitelist=whitelist, max_tools=12)
        if listed else None
    )
    return _mcp_tool_defs_cache


def _build_chat_tool_defs(cfg: Any) -> Optional[List[Dict[str, Any]]]:
    """组合 MCP、内部网页搜索与受控补报工具；网页搜索不受 MCP 熔断影响。"""
    definitions = list(_mcp_tool_defs() or []) if getattr(cfg, "mcp_tools", False) else []
    # 防御性过滤：即使 MCP 定义来自旧进程缓存，也不能再暴露已下线工具。
    definitions = [
        item for item in definitions
        if item.get("function", {}).get("name") not in DISABLED_MCP_TOOL_NAMES
    ]
    web = TavilyWebSearch(timeout=getattr(cfg, "web_search_timeout", 15))
    if getattr(cfg, "web_search", True) and web.available:
        definitions.extend(to_openai_tools([WEB_SEARCH_TOOL]))
    # 补报工具只申请授权，不执行下载；由服务端解析候选并等待用户确认。
    definitions.append(SUPPLEMENT_REQUEST_TOOL)
    return definitions or None


def _unavailable_tool_executor() -> Callable[[str, Dict[str, Any]], str]:
    """MCP 与网页搜索都不可用时的占位执行器。

    RagQA 的工具编排路径只在 ``tool_executor is not None`` 时进入；补报工具必须
    经由该路径才能到达模型，因此这里保留一个只返回受控失败说明的执行器。
    任何真实工具都不会被调用，``use_mcp=false`` 的纯 RAG 路径也不受影响。
    """
    def _executor(name: str, arguments: Dict[str, Any]) -> str:
        return f"工具调用失败：{name} 当前不可用"

    return _executor


# 股票名称 → 代码 词典缓存（来源：MCP get_stock_a_code_name，加载一次复用）
_stock_name_cache: Optional[Dict[str, str]] = None


def _load_stock_name_map() -> Dict[str, str]:
    """加载股票名称→代码词典（MCP/akshare 全市场清单）；失败返回空 dict"""
    global _stock_name_cache
    if _stock_name_cache is not None:
        return _stock_name_cache
    _stock_name_cache = {}
    try:
        raw = stock_mcp.call_tool("get_stock_a_code_name", {"output_format": "json"}, timeout=60)
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, list):
            for item in data:
                code = item.get("code")
                name = (item.get("name") or "").replace(" ", "").strip()
                if code and name:
                    _stock_name_cache[name] = code
    except Exception:
        logger.warning("股票名称词典加载失败，名称解析将依赖自动补全索引")
    return _stock_name_cache


def _resolve_symbol_code(symbol: str) -> Optional[str]:
    """把股票名称/代码解析为 6 位代码。

    6 位纯数字代码直接可用（不依赖自动补全索引）；名称优先走索引搜索，
    索引未就绪时返回 None（由调用方提示改用代码）。
    """
    symbol = str(symbol).strip()
    if re.fullmatch(r"\d{6}", symbol):
        return symbol
    try:
        if stock_index.is_valid_code(symbol):
            return symbol
    except Exception:
        pass
    try:
        results = stock_index.search(symbol, limit=10) or []
    except Exception:
        results = []
    for item in results:
        code = item.get("code", "")
        try:
            if stock_index.is_valid_code(code):
                return code
        except Exception:
            if re.fullmatch(r"\d{6}", str(code)):
                return code
    # 索引未就绪时用 MCP 全市场名称词典兜底
    return _load_stock_name_map().get(symbol)


def _realtime_via_tencent(symbol: str) -> str:
    """用腾讯行情提供实时数据（akshare 东财数据源当前不可用时的替代路由）"""
    try:
        rows = tencent_quote.realtime([symbol])
    except Exception as exc:
        return f"工具调用失败：{exc}"
    if not rows:
        return f"未获取到 {symbol} 的实时行情"
    row = rows[0]
    return json.dumps({
        k: row.get(k) for k in (
            "code", "name", "price", "prev_close", "open", "high", "low",
            "change", "change_pct", "volume", "amount_wan", "turnover_rate",
            "pe", "total_mv_yi", "time",
        )
    }, ensure_ascii=False)


def _build_mcp_tool_executor(cfg: Any) -> Optional[Callable[[str, Dict[str, Any]], str]]:
    """构建问答工具执行器：(name, arguments) -> 文本；未启用/关闭时返回 None。

    执行前把股票名称解析为 6 位代码，默认 output_format=json；
    无法解析返回提示文本（模型可改用代码重试）。
    """
    if not getattr(cfg, "mcp_tools", False):
        return None
    timeout = getattr(cfg, "mcp_tool_timeout", 30)

    def _tool_result_failed(result: str) -> bool:
        lowered = str(result).lower()
        return (
            "validation error" in lowered
            or "unexpected keyword argument" in lowered
            or lowered.startswith("error:")
        )

    def _executor(name: str, arguments: Dict[str, Any]) -> str:
        # 熔断检查：连续失败达阈值且未到冷却期 → 暂停使用
        if not mcp_breaker.allow():
            return (f"MCP 服务暂不可用（连续失败 {mcp_breaker.consecutive_failures} 次，"
                    f"熔断中，约 {int(mcp_breaker.cooldown_seconds)} 秒后自动探测恢复）")
        args = dict(arguments or {})
        schema = _mcp_tool_input_schemas.get(name)
        if schema is not None:
            allowed = set((schema.get("properties") or {}).keys())
            args = {key: value for key, value in args.items() if key in allowed}
        symbol = args.get("symbol")
        if symbol:
            code = _resolve_symbol_code(str(symbol))
            if code:
                args["symbol"] = code
            else:
                return f"无法解析股票「{symbol}」，请使用 6 位股票代码"
        # 实时行情路由到腾讯（akshare 东财数据源不可用，腾讯直连已验证可用）
        if name == "get_realtime_data":
            result = _realtime_via_tencent(args["symbol"])
            if result.startswith("工具调用失败") or result.startswith("未获取到"):
                mcp_breaker.record_failure(result)
                _record_mcp_tool_health(name, False, result)
            else:
                mcp_breaker.record_success()
                _record_mcp_tool_health(name, True, result)
            return result
        # 只有工具 schema 声明了 output_format 才补默认值；get_time_info 等
        # 无参数工具不能接收通用股票参数。
        if schema is None or "output_format" in (schema.get("properties") or {}):
            args.setdefault("output_format", "json")
        try:
            result = market_data_mcp.call_tool(name, args, timeout=timeout)
        except Exception as exc:
            mcp_breaker.record_failure(exc)
            _record_mcp_tool_health(name, False, str(exc))
            return f"工具调用失败：{exc}"
        if _tool_result_failed(result):
            mcp_breaker.record_failure(result)
            _record_mcp_tool_health(name, False, result)
            return f"工具调用失败：{result}"
        mcp_breaker.record_success()
        _record_mcp_tool_health(name, True, result)
        return result

    return _executor


def _build_chat_tool_executor(cfg: Any) -> Optional[Callable[[str, Dict[str, Any]], str]]:
    """统一调度 MCP 与网页搜索，任一可用即启用工具编排。"""
    mcp_executor = _build_mcp_tool_executor(cfg)
    web = TavilyWebSearch(timeout=getattr(cfg, "web_search_timeout", 15))
    web_enabled = getattr(cfg, "web_search", True) and web.available
    if mcp_executor is None and not web_enabled:
        return None

    def _executor(name: str, arguments: Dict[str, Any]) -> str:
        if name == "web_search":
            if not web_enabled:
                return "工具调用失败：网页搜索未配置 TAVILY_API_KEY"
            return web.search(**dict(arguments or {}))
        if name in DISABLED_MCP_TOOL_NAMES:
            return "工具已下线：请使用 web_search 查询近期新闻、公告与事件。"
        if mcp_executor is None:
            return "工具调用失败：MCP 工具未启用"
        return mcp_executor(name, arguments)

    return _executor


def _build_reranker(cfg) -> Optional[Any]:
    """cfg.rerank 开启时构造 CrossEncoderReranker；失败仅警告并回退 None（零回归）。

    构造本身不加载模型（惰性加载），这里主要捕获未安装 sentence-transformers
    等构造期异常；加载失败在首次调用时由 _maybe_rerank 回退纯向量检索。
    """
    if not getattr(cfg, "rerank", False):
        return None
    try:
        from financial_report_fetcher.rag.reranker import CrossEncoderReranker

        return CrossEncoderReranker(
            getattr(cfg, "rerank_model", "BAAI/bge-reranker-base"),
            top_k=getattr(cfg, "top_k", 8),
        )
    except Exception:
        logger.warning("CrossEncoderReranker 构造失败，回退纯向量检索（rag.rerank=%s）",
                       getattr(cfg, "rerank", False), exc_info=True)
        return None


def _init_rag() -> None:
    """按 config.yaml 的 rag: 段初始化 RAG 组件；未启用或初始化失败则保持 None"""
    global rag_store, rag_service, rag_qa
    try:
        cfg = RagConfig.load()
        if not cfg.enabled:
            return
        embedder = LocalEmbedder(
            cfg.embedding_model,
            hf_endpoint=getattr(cfg, "hf_endpoint", ""),
        )
        rag_store = RagStore(cfg.store_path, embedder)
        rag_service = IngestionService(
            rag_store,
            chunk_size=cfg.chunk_size,
            chunk_overlap=cfg.chunk_overlap,
            manifest_path=os.path.join(cfg.store_path, "manifest.json"),
            auto_ingest=cfg.auto_ingest,
        )
        reranker = _build_reranker(cfg)
        rag_qa = RagQA(
            rag_store,
            ai_client,
            top_k=cfg.top_k,
            # 补报工具需要工具编排路径，工具全不可用时用占位执行器保底。
            tool_executor=_build_chat_tool_executor(cfg) or _unavailable_tool_executor(),
            supplement_request_handler=_handle_supplement_request,
            # Scope 校验与执行器必须使用同一名称→代码解析路径，否则模型可用公司名称绕过范围。
            company_code_resolver=_resolve_symbol_code,
            max_tool_rounds=getattr(cfg, "mcp_max_tool_rounds", 3),
            max_tool_calls=getattr(cfg, "mcp_max_tool_calls", 6),
            reranker=reranker,
            rerank_candidates=getattr(cfg, "rerank_candidates", 30),
            rerank_score_threshold=getattr(cfg, "rerank_score_threshold", 0.5),
            rerank_margin_threshold=getattr(cfg, "rerank_margin_threshold", 0.05),
        )
        # RAG 启用且增强分析开启时，重建 analyzer 注入按维度检索上下文；
        # enhanced_analysis=false 或 RAG 未启用时保持原有截断全文行为。
        global analyzer
        if cfg.enhanced_analysis:
            analyzer = ReportAnalyzer(ai_client, rag_analysis=RagAnalysis(
                rag_store, top_k=cfg.top_k, reranker=reranker,
                rerank_candidates=getattr(cfg, "rerank_candidates", 30),
                rerank_score_threshold=getattr(cfg, "rerank_score_threshold", 0.5),
                rerank_margin_threshold=getattr(cfg, "rerank_margin_threshold", 0.05),
            ))
        logger.info("RAG 知识库已初始化：%s", cfg.store_path)
    except Exception as exc:
        logger.warning("rag_init_failed error_type=%s", type(exc).__name__)
        rag_store = rag_service = rag_qa = None


# 串行化「检查 + 下载」：防止 serve_pdf / chat 请求线程与后台分析线程
# 并发下载同一份报告时互踩同一文件路径
_download_lock = threading.Lock()

# ── 请求日志（落盘到 logs/ 目录）────────────────────────────────

LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
APP_LOG = os.path.join(LOG_DIR, "app.log")
ACCESS_LOG = os.path.join(LOG_DIR, "access.log")

# 应用日志（按天轮转，保留 30 天）
_app_handler = logging.handlers.TimedRotatingFileHandler(
    APP_LOG, when="midnight", interval=1, backupCount=30, encoding="utf-8",
)
_app_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
))
_app_logger = logging.getLogger("webapp")
_app_logger.addHandler(_app_handler)
_app_logger.setLevel(logging.INFO)
_app_logger.propagate = False  # 不重复输出到控制台

logger.addHandler(_app_handler)  # server 本模块日志也落盘
logger.setLevel(logging.INFO)

# 访问日志（纯文本追加）
_access_fmt = "{time} [{method}] {path} {status} {elapsed:.0f}ms{extra}\n"
_access_lock = threading.Lock()


def _write_access(method: str, path: str, status: int, elapsed: float,
                  extra: str = "") -> None:
    """写一条访问日志到文件（线程安全）。"""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = _access_fmt.format(
        time=now, method=method, path=path, status=status,
        elapsed=elapsed * 1000, extra=extra,
    )
    with _access_lock:
        with open(ACCESS_LOG, "a", encoding="utf-8") as f:
            f.write(line)


@app.middleware("http")
async def _access_log_middleware(request: Request, call_next):
    """请求级访问日志中间件：记录每个请求的方法、路径、耗时、状态码。"""
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed = time.monotonic() - start
        _write_access(request.method, request.url.path, 500, elapsed,
                      extra=f" | ERROR: {exc}")
        raise
    elapsed = time.monotonic() - start
    _write_access(request.method, request.url.path, response.status_code, elapsed)
    return response


# ── 请求体模型 ─────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    interests: List[str] = Field(default_factory=list)
    dimensions: List[str] = Field(default_factory=list)

    def resolved_interests(self) -> List[str]:
        if self.interests:
            requested = self.interests
        else:
            requested = [LEGACY_INTEREST_ALIASES.get(item, item) for item in self.dimensions]
        selected = [item for item in requested if item in SUPPORTED_INTEREST_IDS]
        return list(dict.fromkeys(selected)) or list(DEFAULT_INTERESTS)


class ChatRequest(BaseModel):
    question: str


class GlobalChatRequest(BaseModel):
    question: str
    filters: Optional[Dict[str, Any]] = None


class RenameSessionRequest(BaseModel):
    title: str


class StreamChatRequest(BaseModel):
    question: str
    filters: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None  # 缺省/无效时自动新建会话
    use_mcp: bool = True             # 允许模型调用 MCP 工具获取更多信息
    focus_report: Optional[Dict[str, str]] = None  # {code, period}：提升该报告检索权重
    scope_mode: Literal["auto", "company_only", "company_industry", "whole_corpus"] = "auto"


class ResumeResearchRequest(BaseModel):
    session_id: str


class RagIngestOneRequest(BaseModel):
    report_id: str
    source: str


class FavoriteRunRequest(BaseModel):
    favorite: bool


class SaveFactMemoryRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)
    fact_id: str = Field(min_length=1, max_length=512)


class SaveArtifactMemoryRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)
    artifact_id: str = Field(min_length=1, max_length=1024)


class SaveDecisionMemoryRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: List[str] = Field(min_length=1, max_length=50)


# ── 工具函数 ───────────────────────────────────────────────

def _report_type_for_period(period: dt.date) -> ReportType:
    """按报告期月推断财报类型：3/9 月=季报，6 月=半年报，12 月=年报"""
    if period.month == 6:
        return ReportType.SEMI_ANNUAL
    if period.month in (3, 9):
        return ReportType.QUARTERLY
    return ReportType.ANNUAL


def _parse_period(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"报告期格式无效：{value}（应为 YYYY-MM-DD）")


def _find_report_meta(code: str, period: dt.date) -> ReportMeta:
    """查询单份财报元信息（含公司名与下载地址）；查无 → 404，上游不可用 → 503"""
    rt = _report_type_for_period(period)
    try:
        reports = datasource.fetch_reports(
            stock_code=code,
            report_types=[rt],
            start_date=period,
            end_date=period,
        )
    except requests.exceptions.RequestException as exc:
        logger.warning("查询 %s 财报元数据失败：%s", code, exc)
        raise HTTPException(
            503, "财报数据源暂时不可用，请稍后重试"
        ) from exc
    if not reports:
        raise HTTPException(
            404, f"{code} 在 {period.isoformat()} 无 {rt.value} 财报"
        )
    return min(reports, key=lambda r: abs((r.period - period).days))


def _local_pdf_path(meta: ReportMeta) -> str:
    return os.path.join(REPORTS_DIR, ReportDownloader.build_filename(meta))


def _pdf_file_exists(meta: ReportMeta) -> bool:
    """本地是否已有可用的 PDF 文件（存在且非空）"""
    return os.path.exists(_local_pdf_path(meta)) and os.path.getsize(_local_pdf_path(meta)) > 0


def _require_ai() -> None:
    if not ai_client.api_key:
        raise HTTPException(400, "未配置 AI API 密钥，请设置 AI_API_KEY 环境变量或在 config.yaml 中配置 ai_api_key")


def _ensure_pdf(meta: ReportMeta) -> str:
    """本地未下载则先下载；返回本地路径，失败抛 503"""
    # 存在性检查与 download_one 在锁内原子执行，避免并发线程
    # （analyze 工作线程 / serve_pdf / chat）对同一报告重复下载互踩
    with _download_lock:
        path = _local_pdf_path(meta)
        if _pdf_file_exists(meta):
            return path
        try:
            status = downloader.download_one(meta, REPORTS_DIR)
            if status != DownloadStatus.SUCCESS:
                raise RuntimeError(f"下载返回 {status.value}")
        except Exception as exc:
            logger.exception("PDF 下载失败：%s", meta.title)
            raise HTTPException(503, f"PDF 下载失败：{exc}")
        return path


def _auto_ingest_pdf(pdf_path: str) -> None:
    """下载场景自动建入 RAG（只摄 PDF）；未启用或失败时静默跳过"""
    if rag_service is None:
        return
    try:
        rag_service.auto_ingest_pdf(pdf_path)
    except Exception:
        logger.exception("自动摄取失败：%s", pdf_path)


def _auto_ingest_report(pdf_path: str) -> None:
    """分析场景自动建入 RAG（连带分析报告）；未启用或失败时静默跳过"""
    if rag_service is None:
        return
    try:
        rag_service.auto_ingest_report(pdf_path)
    except Exception:
        logger.exception("自动摄取失败：%s", pdf_path)


# ── 历史记录（本地资源直达，无需 AI）───────────────────────────

@app.get("/api/history")
def get_history(search: str = Query(default="")) -> Dict[str, Any]:
    """返回本地所有已下载 PDF 与已分析报告的扁平列表"""
    items = build_flat_history(ANALYSIS_DIR, REPORTS_DIR)
    if search:
        q = search.strip().lower()
        items = [
            i for i in items if q in i.get("company", "").lower()
            or q in i.get("code", "")
            or str(i.get("year", "")).startswith(q)
        ]
    return {"items": items}


@app.delete("/api/history/{filename:path}")
def delete_history_analysis(filename: str) -> Dict[str, Any]:
    """删除一份本地 AI 分析 JSON 及其 Markdown 副本，保留原始 PDF。"""
    safe_name = os.path.basename(filename)
    if safe_name != filename or not safe_name.endswith("_分析报告.json"):
        raise HTTPException(404, "分析报告不存在")
    json_path = os.path.join(ANALYSIS_DIR, safe_name)
    if not os.path.isfile(json_path):
        raise HTTPException(404, f"分析报告不存在：{safe_name}")
    try:
        os.remove(json_path)
        markdown_path = os.path.splitext(json_path)[0] + ".md"
        if os.path.isfile(markdown_path):
            os.remove(markdown_path)
    except OSError as exc:
        logger.exception("删除分析报告失败：%s", safe_name)
        raise HTTPException(500, f"删除分析报告失败：{exc}") from exc
    return {"deleted": safe_name}


@app.get("/api/history-pdf/{filename:path}")
def get_history_pdf(filename: str) -> FileResponse:
    """以内联方式返回历史记录中的本地 PDF，不触发远端查询或下载。"""
    safe_name = os.path.basename(filename)
    if safe_name != filename or not safe_name.lower().endswith(".pdf"):
        raise HTTPException(404, "PDF 文件不存在")
    path = os.path.join(REPORTS_DIR, safe_name)
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        raise HTTPException(404, f"PDF 文件不存在：{safe_name}")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=safe_name,
        content_disposition_type="inline",
    )


@app.get("/api/history/{filename:path}")
def get_history_detail(filename: str) -> Dict[str, Any]:
    """返回单份分析报告的完整内容"""
    content = get_analysis_detail(ANALYSIS_DIR, filename)
    if content is None:
        raise HTTPException(404, f"分析报告不存在：{filename}")
    return clean_analysis_payload(content)


# ── 股票行情（腾讯免费 API）───────────────────────────────────────

@app.get("/api/quote")
def market_quote(symbols: str = Query(..., description="股票代码，逗号分隔，如 600519,000001")) -> Dict[str, Any]:
    """个股实时行情（腾讯免费源）"""
    code_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not code_list:
        raise HTTPException(status_code=400, detail="symbols 不能为空")
    try:
        return {"source": "tencent", "quotes": tencent_quote.realtime(code_list)}
    except Exception as exc:  # noqa: BLE001
        logger.error("行情查询失败 symbols=%s：%s", symbols, exc)
        raise HTTPException(status_code=502, detail=f"行情查询失败：{exc}")


@app.get("/api/quote/kline")
def market_kline(
    symbol: str = Query(..., description="股票代码，如 600519"),
    period: str = Query("day", pattern="^(day|week|month)$"),
    count: int = Query(320, ge=1, le=800),
    adjust: str = Query("qfq", pattern="^(qfq|hfq|none)$"),
) -> Dict[str, Any]:
    """历史 K 线（腾讯免费源）"""
    try:
        return {"source": "tencent", "symbol": symbol,
                "period": period, "adjust": adjust,
                "bars": tencent_quote.kline(symbol, period=period, count=count, adjust=adjust)}
    except Exception as exc:  # noqa: BLE001
        logger.error("K线查询失败 symbol=%s：%s", symbol, exc)
        raise HTTPException(status_code=502, detail=f"K线查询失败：{exc}")


@app.get("/api/quote/index")
def market_index(codes: str = Query(..., description="指数代码（带前缀），如 sh000001,sz399001")) -> Dict[str, Any]:
    """指数实时行情（腾讯免费源）"""
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list:
        raise HTTPException(status_code=400, detail="codes 不能为空")
    try:
        return {"source": "tencent", "quotes": tencent_quote.index(code_list)}
    except Exception as exc:  # noqa: BLE001
        logger.error("指数查询失败 codes=%s：%s", codes, exc)
        raise HTTPException(status_code=502, detail=f"指数查询失败：{exc}")


# ── 财务 / 基本面（china-stock-mcp）────────────────────────────

@app.get("/api/stock/info")
def stock_info(symbol: str = Query(..., description="股票代码，如 600519")) -> Dict[str, Any]:
    """公司基本信息（MCP: get_stock_basic_info）"""
    return _mcp_call_json("get_stock_basic_info", {"symbol": symbol, "output_format": "json"})


@app.get("/api/stock/financials")
def stock_financials(symbol: str = Query(..., description="股票代码，如 600519")) -> Dict[str, Any]:
    """关键财务指标（MCP: get_financial_metrics）"""
    return _mcp_call_json("get_financial_metrics", {"symbol": symbol, "output_format": "json"})


class McpCallRequest(BaseModel):
    """通用 MCP 工具调用请求体"""
    tool: str = Field(..., description="工具名，如 get_balance_sheet")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="工具参数")
    timeout: float = Field(default=90, ge=1, le=600)


@app.post("/api/stock/mcp/call")
def stock_mcp_call(body: McpCallRequest) -> Dict[str, Any]:
    """通用调用 china-stock-mcp 任意工具（供 agent 使用）"""
    try:
        text = stock_mcp.call_tool(body.tool, body.arguments, timeout=body.timeout)
    except Exception as exc:  # noqa: BLE001
        logger.error("MCP 调用失败 tool=%s：%s", body.tool, exc)
        raise HTTPException(status_code=502, detail=f"MCP 调用失败：{exc}")
    return {"tool": body.tool, "result_text": text}


def _mcp_call_json(tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """调用 MCP 工具并尝试把返回文本解析为 JSON；失败则原样返回文本。"""
    try:
        text = stock_mcp.call_tool(tool, arguments)
    except Exception as exc:  # noqa: BLE001
        logger.error("MCP 调用失败 tool=%s：%s", tool, exc)
        raise HTTPException(status_code=502, detail=f"MCP 调用失败：{exc}")
    try:
        return {"tool": tool, "data": json.loads(text)}
    except json.JSONDecodeError:
        return {"tool": tool, "data": None, "text": text}


# ── 健康检查 ───────────────────────────────────────────────

@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "ai_key_configured": bool(ai_client.api_key),
        "index_ready": stock_index.is_ready,
        "started_at": SERVER_STARTED_AT.isoformat(timespec="seconds"),
        "started_ts": SERVER_STARTED_AT.timestamp(),
    }


# ── 自动补全 ───────────────────────────────────────────────

@app.get("/api/companies")
def autocomplete(q: str = Query(default="", max_length=30)) -> Dict[str, Any]:
    stock_index.start()                   # 幂等：确保后台构建已启动
    stock_index.wait_ready(timeout=5.0)   # 首次请求等待索引（实测约 1 秒）
    return {"results": stock_index.search(q, limit=10)}


# ── 财报列表 ───────────────────────────────────────────────

@app.get("/api/companies/{code}/reports")
def list_reports(
    code: str, start: str = Query(...), end: str = Query(...)
) -> Dict[str, Any]:
    start_date, end_date = _parse_period(start), _parse_period(end)
    if start_date > end_date:
        raise HTTPException(400, "start 不得晚于 end")

    stock_index.start()
    if not stock_index.wait_ready(timeout=5.0):
        raise HTTPException(503, "股票索引尚未就绪，请稍后重试")
    if not stock_index.is_valid_code(code):
        raise HTTPException(404, f"未知股票代码：{code}")

    reports = datasource.fetch_reports(
        stock_code=code,
        report_types=[
            ReportType.ANNUAL,
            ReportType.SEMI_ANNUAL,
            ReportType.QUARTERLY,
        ],
        start_date=start_date,
        end_date=end_date,
    )
    reports.sort(key=lambda r: r.period, reverse=True)
    analyzed = analyzed_periods_for_code(ANALYSIS_DIR, code)
    items = [
        {
            "code": r.company_id,
            "period": r.period.isoformat(),
            "type": r.report_type.value,
            "title": r.title,
            "downloaded": _pdf_file_exists(r),
            "analyzed": r.period.isoformat() in analyzed,
        }
        for r in reports
    ]
    return {
        "code": code,
        "name": stock_index.company_name(code) or code,
        "reports": items,
    }


# ── PDF 预览 ───────────────────────────────────────────────

@app.get("/api/reports/{code}/{period}.pdf")
def serve_pdf(code: str, period: str) -> FileResponse:
    p = _parse_period(period)
    meta = _find_report_meta(code, p)
    path = _ensure_pdf(meta)
    if rag_service is not None:
        task_manager.submit(lambda: _auto_ingest_pdf(path))  # 幂等，繁忙被拒也无妨
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
        content_disposition_type="inline",  # iframe 预览而非下载
    )


@app.post("/api/reports/{code}/{period}/download")
def download_report(code: str, period: str) -> Dict[str, Any]:
    """显式下载财报 PDF（幂等）：前端下载按钮转圈 → 打钩 → 再加载预览"""
    p = _parse_period(period)
    meta = _find_report_meta(code, p)
    path = _ensure_pdf(meta)
    if rag_service is not None:
        task_manager.submit(lambda: _auto_ingest_pdf(path))  # 幂等，繁忙被拒也无妨
    return {"downloaded": True, "file": os.path.basename(path)}


def _run_mcp_diagnose() -> Dict[str, Any]:
    """检查问答实际使用的 MCP、工具清单与数据源可用性。"""
    try:
        listed = market_data_mcp.list_tools(timeout=25)
    except Exception as exc:
        return {
            "ok": False,
            "provider": "stock-data-mcp",
            "tools": [],
            "data_source_status": {"ok": False, "message": "未执行：工具清单获取失败"},
            "message": f"问答 MCP 连接异常：{exc}",
        }

    names = [str(tool.get("name")) for tool in listed if tool.get("name")]
    if "data_source_status" not in names:
        return {
            "ok": False,
            "provider": "stock-data-mcp",
            "tools": names,
            "data_source_status": {"ok": False, "message": "当前 MCP 版本未提供 data_source_status"},
            "message": f"问答 MCP 已连接，共 {len(names)} 个工具；但无法检查数据源",
        }
    try:
        result = market_data_mcp.call_tool("data_source_status", {}, timeout=25)
        failed = str(result).lower().startswith("error:")
        check = {"ok": not failed, "message": str(result)[:500]}
    except Exception as exc:
        check = {"ok": False, "message": str(exc)}
    return {
        "ok": check["ok"],
        "provider": "stock-data-mcp",
        "tools": names,
        "data_source_status": check,
        "message": (
            f"问答 MCP 与数据源正常，共 {len(names)} 个工具"
            if check["ok"] else f"问答 MCP 已连接，但数据源异常：{check['message']}"
        ),
    }


@app.get("/api/mcp/status")
def mcp_status() -> Dict[str, Any]:
    """MCP 状态检查：熔断器状态 + 工具注入情况 + 最近诊断"""
    st = mcp_breaker.status()
    st["tools_injected"] = bool(
        mcp_breaker.allow() and _mcp_tool_defs_ready and _mcp_tool_defs_cache
    )
    st["provider"] = "stock-data-mcp"
    with _mcp_tool_health_lock:
        st["tool_health"] = dict(_mcp_tool_health)
    st["diagnose"] = _mcp_diagnose_cache or {"ok": None, "message": "尚未执行检测"}
    return st


@app.post("/api/mcp/diagnose")
def mcp_diagnose() -> Dict[str, Any]:
    """手动触发 MCP 服务检测（会尝试连接，可能耗时数秒）"""
    global _mcp_diagnose_cache
    _mcp_diagnose_cache = _run_mcp_diagnose()
    return _mcp_diagnose_cache


def _default_analysis_dimensions() -> List[str]:
    """默认分析维度：config.yaml 的 rag.analysis_dimensions 优先，否则内置 5 个"""
    try:
        cfg = RagConfig.load()
        configured = cfg.analysis_dimensions
    except Exception:
        configured = []
    return configured or list(ReportAnalyzer.DEFAULT_DIMENSIONS)


SUPPORTED_INTERESTS = (
    {"id": "financial_overview", "name": "财务概览", "description": "关键规模、增速与结构变化"},
    {"id": "cash_flow", "name": "现金流", "description": "现金创造、回款与偿债质量"},
    {"id": "risks", "name": "风险", "description": "异常波动、承诺事项与经营风险"},
    {"id": "earnings_quality", "name": "盈利质量", "description": "利润含金量与可持续性"},
    {"id": "business", "name": "经营变化", "description": "业务结构、客户与增长动因"},
    {"id": "governance", "name": "治理", "description": "治理、关联交易与股东事项"},
    {"id": "industry_position", "name": "行业位置", "description": "竞争地位与行业变化"},
)
SUPPORTED_INTEREST_IDS = frozenset(item["id"] for item in SUPPORTED_INTERESTS)
DEFAULT_INTERESTS = ("financial_overview", "cash_flow", "risks")
LEGACY_INTEREST_ALIASES = {
    "financial_summary": "financial_overview",
    "cashflow": "cash_flow",
    "risk_warning": "risks",
    "profit_quality": "earnings_quality",
    "business_highlights": "business",
    "governance": "governance",
    "industry_position": "industry_position",
}


@app.get("/api/analysis/interests")
def analysis_interests() -> Dict[str, Any]:
    defaults = set(DEFAULT_INTERESTS)
    return {
        "interests": [
            {**item, "default": item["id"] in defaults} for item in SUPPORTED_INTERESTS
        ],
        "defaults": list(DEFAULT_INTERESTS),
    }


@app.get("/api/analysis/dimensions")
def analysis_dimensions() -> Dict[str, Any]:
    """返回全部可选分析维度元数据（前端勾选面板渲染 + 全选/默认勾选用）"""
    defaults = set(_default_analysis_dimensions())
    items = []
    for dim_id, cfg in ANALYSIS_TEMPLATES.items():
        if not cfg.get("prompt"):
            continue
        items.append({
            "id": dim_id,
            "name": cfg["name"],
            "description": cfg.get("description", ""),
            "default": dim_id in defaults,
        })
    return {"dimensions": items, "defaults": sorted(defaults)}


# ── 分析任务 ───────────────────────────────────────────────


def _get_progressive_pipeline():
    global progressive_pipeline
    if progressive_pipeline is None:
        progressive_pipeline = build_progressive_pipeline(ai_client, ANALYSIS_DIR)
    return progressive_pipeline

def _analysis_progress_value(event: Dict[str, Any]) -> float:
    """把分析器阶段事件映射到供前端轮询的 0~1 总进度。"""
    stage = event.get("stage")
    if stage == "extracting_pdf":
        return 0.20
    if stage in ("dimension_started", "dimension_completed"):
        total = max(1, int(event.get("total") or 1))
        completed = max(0, min(total, int(event.get("completed") or 0)))
        return 0.25 + 0.55 * completed / total
    if stage == "extracting_metrics":
        return 0.82
    if stage == "analysis_completed":
        return 0.92
    return 0.18


def _retire_superseded_analysis(analysis_id: str, code: str, period: dt.date) -> None:
    """清理同报告期被取代的旧命名分析产物（一期一份），尽力而为且不阻断分析。"""
    try:
        keep_filename = f"{analysis_output_stem(analysis_id)}.json"
        removed = retire_superseded_analysis_files(
            ANALYSIS_DIR,
            code=code,
            period=period.isoformat(),
            keep_filename=keep_filename,
        )
    except (OSError, ValueError):
        logger.exception("清理同报告期旧分析产物失败：%s", analysis_id)
        return
    if removed:
        logger.info("已清理同报告期旧分析产物：%s", "、".join(removed))


@app.post("/api/reports/{code}/{period}/analyze")
def analyze_report(code: str, period: str, body: AnalyzeRequest) -> Dict[str, Any]:
    _require_ai()
    p = _parse_period(period)
    meta = _find_report_meta(code, p)

    interests = body.resolved_interests()
    stop_event = threading.Event()
    analysis_id = f"{meta.company_name}_{code}_{p.isoformat()}_分析报告"
    report_id = build_report_id(code, p, meta.report_type)

    def _run(emit: Callable[[str, Dict[str, Any]], None]):
        path = _ensure_pdf(meta)
        request = AnalysisPipelineRequest(
            analysis_id=analysis_id,
            report_id=report_id,
            company_code=code,
            company_name=meta.company_name,
            period=p.isoformat(),
            pdf_path=path,
            interests=tuple(interests),
        )
        document = _get_progressive_pipeline().run(request, emit, stop_event)
        if getattr(document, "stage", None) in ("completed", "partial"):
            # 只有真正产出可读结果时才清理旧产物；取消/中断/状态不明一律保留旧报告
            _retire_superseded_analysis(analysis_id, code, p)
        _auto_ingest_report(path)
        return document

    task_id = task_manager.submit(_run, stop_event=stop_event, eventful=True)
    if task_id is None:
        raise HTTPException(409, "已有分析任务进行中，请稍候")
    return {
        "task_id": task_id,
        "analysis_id": analysis_id,
        "interests": interests,
        "dimensions": list(body.dimensions),
        "status_url": f"/api/tasks/{task_id}",
        "event_url": f"/api/analysis/tasks/{task_id}/events",
    }


_TERMINAL_TASK_STATUSES = {"done", "failed", "cancelled", "partial"}


async def _analysis_event_stream(task_id: str, after_id: int):
    cursor = after_id
    while True:
        events = await asyncio.to_thread(
            task_manager.wait_for_events, task_id, cursor, 15.0
        )
        if not events:
            yield ": keep-alive\n\n"
        for event in events:
            cursor = event.id
            yield (
                f"id: {event.id}\n"
                f"event: {event.event_type}\n"
                f"data: {json.dumps(event.payload, ensure_ascii=False)}\n\n"
            )
        task = task_manager.get(task_id)
        if task is None or task["status"] in _TERMINAL_TASK_STATUSES:
            break


@app.get("/api/analysis/tasks/{task_id}/events")
async def analysis_task_events(
    task_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    if task_manager.get(task_id) is None:
        raise HTTPException(404, f"未知任务：{task_id}")
    header_cursor = request.headers.get("Last-Event-ID")
    try:
        cursor = int(header_cursor) if header_cursor is not None else after
    except ValueError as exc:
        raise HTTPException(400, "Last-Event-ID 必须是整数") from exc
    if cursor < 0:
        raise HTTPException(400, "事件游标不能为负数")
    return StreamingResponse(
        _analysis_event_stream(task_id, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> Dict[str, Any]:
    """停止分析任务（仅 pending/running 有效）"""
    if task_manager.cancel(task_id):
        return {"cancelled": True, "status": "cancelled"}
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(404, f"未知任务：{task_id}")
    return {"cancelled": False, "status": task["status"]}


# ── 任务轮询 ───────────────────────────────────────────────

@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> Dict[str, Any]:
    t = task_manager.get(task_id)
    if t is None:
        raise HTTPException(404, f"未知任务：{task_id}")
    return t


# ── 自由问答 ───────────────────────────────────────────────

@app.post("/api/reports/{code}/{period}/chat")
def chat(code: str, period: str, body: ChatRequest) -> Dict[str, Any]:
    started_at = time.perf_counter()
    _require_ai()
    if not body.question.strip():
        raise HTTPException(400, "问题不能为空")
    p = _parse_period(period)
    meta = _find_report_meta(code, p)

    session_key = f"{code}:{p.isoformat()}"
    with _chat_lock:
        history = list(chat_sessions.get(session_key, []))

    # 先尝试 RAG（无需下载 PDF）；未命中/失败则回退传统 PDF 问答
    citations = []
    rag_result = None
    if rag_qa is not None:
        try:
            rag_result = rag_qa.try_answer_report(
                code, p.isoformat(), body.question, history=history
            )
        except Exception as exc:
            # 异常文本可能包含用户问题，只记录受控诊断信息。
            logger.warning(
                "chat_rag_fallback report_code=%s report_period=%s error_type=%s",
                code, p.isoformat(), type(exc).__name__,
            )
            rag_result = None
    if rag_result is not None:
        answer = rag_result["answer"]
        citations = rag_result.get("citations", [])
    else:
        path = _ensure_pdf(meta)
        answer = analyzer.qa(path, body.question, history=history)

    history = history + [
        {"role": "user", "content": body.question},
        {"role": "assistant", "content": answer},
    ]
    with _chat_lock:
        chat_sessions[session_key] = history[-8:]  # 保留最近 4 轮
    resp: Dict[str, Any] = {
        "answer": answer,
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
    }
    if citations:
        resp["citations"] = citations
    return resp


# ── RAG：通用对话 / 状态 / 摄取 ─────────────────────────────

@app.post("/api/chat")
def global_chat(body: GlobalChatRequest) -> Dict[str, Any]:
    started_at = time.perf_counter()
    _require_ai()
    if not body.question.strip():
        raise HTTPException(400, "问题不能为空")
    if rag_qa is None:
        raise HTTPException(503, "RAG 知识库未初始化：请配置 rag.enabled 并执行索引")
    result = rag_qa.answer(body.question, filters=body.filters)
    if result is None:
        return {
            "answer": "知识库中未检索到相关内容，请补充更多报告或更换问法。",
            "citations": [],
            "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        }
    result["elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
    return result


# ── 智能问答：流式 + 历史会话 ─────────────────────────────

def _resolve_company_industry(code: str) -> Optional[IndustryRef]:
    """通过公司基本信息工具解析行业；不可用/非 JSON/字段为空时返回 None。

    仅读取结构化响应中的显式 ``industry`` / ``industry_name`` 字段，绝不从
    模型自然语言回答推断行业；provider 与解析时间随 IndustryRef 一并记录。
    """
    try:
        raw = stock_mcp.call_tool(
            "get_stock_basic_info",
            {"symbol": code, "output_format": "json"},
            timeout=30,
        )
    except Exception:
        logger.warning("公司基本信息工具不可用，无法解析行业：%s", code)
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    industry = ""
    for key in ("industry", "industry_name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            industry = value.strip()
            break
    if not industry:
        return None
    return IndustryRef(
        name=industry,
        provider="china-stock-mcp",
        resolved_at=dt.datetime.now().isoformat(timespec="seconds"),
    )


def _build_scope_resolver() -> ScopeResolver:
    """构造 ScopeResolver：报告身份来自本地 RAG，行业来自公司基本信息工具。"""
    def _local_report_ids() -> List[str]:
        if rag_store is None:
            return []
        try:
            return rag_store.list_report_ids()
        except Exception:
            return []

    return ScopeResolver(
        report_ids_provider=_local_report_ids,
        company_name_provider=lambda code: stock_index.company_name(code),
        industry_provider=_resolve_company_industry,
        cache_path=os.path.join(BASE_DIR, "data", "company_industries.json"),
    )


def _resolve_scope(body: StreamChatRequest, run_id: str = "") -> Scope:
    """解析并冻结 Scope；任何解析失败回退全库，绝不向上抛出异常。"""
    resolver = _build_scope_resolver()
    request = ScopeRequest(body.scope_mode, body.focus_report)
    try:
        return resolver.resolve(body.question, request)
    except Exception as exc:
        # 异常文本可能包含用户问题；日志与回退原因都只保留受控摘要。
        logger.warning(
            "chat_scope_resolution_failed run_id=%s error_type=%s",
            run_id or "-", type(exc).__name__,
        )
        return Scope("whole_corpus", (), (), fallback_reason="范围解析失败，已回退全库范围")


def _sse(event: str, data: Dict[str, Any]) -> str:
    """SSE 帧：event: xxx\ndata: {...}\n\n"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── 智能问答财报补充下载：候选解析、一次性授权与恢复回答 ──────────

# 一次性授权请求表（进程内），与持久化记录共同构成授权边界。
supplement_registry = SupplementRegistry()
# 候选 → 受控报告元数据；与授权表同生命周期，重启后失效（fail-closed）。
_supplement_reports: Dict[str, Dict[str, ReportMeta]] = {}
_supplement_reports_lock = threading.Lock()
# 请求线程私有的补报上下文：模型只能提交需求，不能决定候选或触发下载。
_supplement_context = threading.local()

_SUPPLEMENT_TYPE_LABELS = {"annual": "年报", "semi_annual": "半年报", "quarterly": "季报"}
# 等待授权与无可用候选时的受控回答文案（不含模型猜测）。
_SUPPLEMENT_WAITING_TEXT = "已申请补充财报原文，等待你选择要下载的报告。"
_SUPPLEMENT_UNAVAILABLE_TEXT = (
    "本地现有财报证据不足，且未找到可补充下载的报告；"
    "以下回答仅基于当前可核验的信息，请谨慎参考。"
)
# 模型申请补库但问题本身不在单公司财报范围内：不得声称“不存在可下载报告”。
_SUPPLEMENT_OUT_OF_SCOPE_TEXT = (
    "当前问题不属于单公司财报范围，无法自动补充财报原文；"
    "以下回答仅基于当前可核验的信息，请谨慎参考。"
)


def _supplement_unavailable_text(scope: Optional[Scope]) -> str:
    """按真实原因给出补报不可用的诚实说明。"""
    if scope is not None and scope.mode == "company_only":
        return _SUPPLEMENT_UNAVAILABLE_TEXT
    return _SUPPLEMENT_OUT_OF_SCOPE_TEXT


class _SseEventPump:
    """把同步事件源（阻塞式模型调用）放到生产者线程，经 asyncio.Queue 转发。

    多个会话的流式请求因此真正并行；一个会话的模型调用不会阻塞其他会话的
    响应。事件循环关闭（客户端断开后清理）时丢弃剩余事件而不报错。
    """

    def __init__(self, loop: "asyncio.AbstractEventLoop", label: str, run_id: str = "") -> None:
        self.queue: "asyncio.Queue" = asyncio.Queue(maxsize=64)
        self.sentinel = object()
        self._loop = loop
        self._label = label
        self._run_id = run_id
        self._stop = threading.Event()

    def put(self, item: Any) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self.queue.put(item), self._loop)
        except RuntimeError:
            pass

    def start(self, source: Callable[[], Any]) -> None:
        def _run() -> None:
            try:
                for item in source():
                    if self._stop.is_set():
                        return
                    self.put(item)
            except Exception as exc:
                logger.warning(
                    "chat_run_producer_failed run_id=%s error_type=%s",
                    self._run_id or "-", type(exc).__name__,
                )
                if not self._stop.is_set():
                    self.put({"type": "error", "error_type": type(exc).__name__})
            finally:
                if not self._stop.is_set():
                    self.put(self.sentinel)

        threading.Thread(target=_run, daemon=True, name=f"{self._label}-producer").start()

    def stop(self) -> None:
        self._stop.set()


class ResolveSupplementRequest(BaseModel):
    """用户对一次补充授权请求的决定。"""

    session_id: str
    action: Literal["approve", "decline"]
    candidate_ids: Optional[List[str]] = None

    @model_validator(mode="after")
    def _decline_must_omit_candidate_ids(self) -> "ResolveSupplementRequest":
        """拒绝操作不接受候选字段；显式 null 与省略字段的语义不同。"""
        if self.action == "decline" and "candidate_ids" in self.model_fields_set:
            raise ValueError("拒绝补充请求不接受 candidate_ids")
        return self


def _report_id_of(meta: ReportMeta) -> str:
    return build_report_id(meta.company_id, meta.period, meta.report_type)


def _supplement_label(period: str, report_type: str) -> str:
    return f"{period[:4]} {_SUPPLEMENT_TYPE_LABELS.get(report_type, report_type)}"


def _indexed_report_ids() -> List[str]:
    if rag_store is None:
        return []
    try:
        return list(rag_store.list_report_ids())
    except Exception:
        logger.warning("读取本地索引报告失败，补充候选可能重复", exc_info=True)
        return []


def _fetch_supplement_reports(scope: Scope, needs: List[Dict[str, Any]]) -> List[ReportMeta]:
    """按受控披露需求查询目标公司的报告元数据。

    只查询当前 Scope 的公司；查询失败或需求非法时返回空列表（按现有证据回答，
    不下载、不向调用方抛异常）。
    """
    if scope.mode != "company_only" or not scope.companies:
        return []
    periods: List[dt.date] = []
    report_types: List[ReportType] = []
    for need in needs or []:
        if not isinstance(need, dict):
            continue
        try:
            periods.append(dt.date.fromisoformat(str(need.get("period"))))
            report_types.append(ReportType(str(need.get("report_type"))))
        except (TypeError, ValueError):
            continue
    if not periods or not report_types:
        return []
    try:
        return list(datasource.fetch_reports(
            stock_code=scope.companies[0].code,
            report_types=sorted(set(report_types), key=lambda item: item.value),
            # 需求期次通常要求“最新”，向前放宽若干年以便解析同公司其他可补披露。
            start_date=dt.date(min(period.year for period in periods) - 2, 1, 1),
            end_date=max(periods),
        ))
    except Exception:
        logger.warning("补充财报候选查询失败，按现有证据回答", exc_info=True)
        return []


def _handle_supplement_request(payload: Dict[str, Any]) -> bool:
    """RagQA 补报处理器：仅在请求线程上下文内登记受控需求，绝不下载。"""
    context = getattr(_supplement_context, "current", None)
    if not context:
        return False
    context["payload"] = payload
    return True


# 补报处理器定义完成后才初始化 RAG；否则 enabled=true 时构造 RagQA 会引用未定义名称。
_init_rag()


def _remember_supplement_reports(
    request_id: str, candidates: List[SupplementCandidate], metas: List[ReportMeta],
) -> None:
    """记住候选 → 报告元数据，供已授权后的下载执行器使用。"""
    by_report_id = {_report_id_of(meta): meta for meta in metas}
    by_candidate = {
        candidate.id: by_report_id[candidate.report_id]
        for candidate in candidates
        if candidate.report_id in by_report_id
    }
    with _supplement_reports_lock:
        _supplement_reports[request_id] = by_candidate


def _supplement_reports_for(request_id: str) -> Dict[str, ReportMeta]:
    with _supplement_reports_lock:
        return dict(_supplement_reports.get(request_id) or {})


def _supplement_candidate_payload(candidate: SupplementCandidate) -> Dict[str, str]:
    """候选对外载荷：只有可读报告身份，不含下载地址、本地路径或模型参数。"""
    return {
        "id": candidate.id,
        "company": candidate.company,
        "code": candidate.code,
        "period": candidate.period,
        "report_type": candidate.report_type,
        "label": _supplement_label(candidate.period, candidate.report_type),
        "source": candidate.source,
    }


def _persist_supplement_status(request_id: str, status: str) -> None:
    """同步授权状态到会话存储；以登记表为准，存储不可用时只记日志。"""
    record = chat_store.get_supplement(request_id)
    if record is None:
        return
    record["status"] = status
    live = supplement_registry.get(request_id)
    if live is not None:
        record["status"] = live.status
        record["selected_ids"] = list(live.selected_ids)
        record["consumed_at"] = live.consumed_at
    try:
        chat_store.save_supplement(record)
    except Exception:
        logger.warning("补充授权状态无法落盘：%s", status, exc_info=True)


def _propose_supplement(
    sid: str, question: str, scope: Scope, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """把模型需求解析为受控候选并保存待授权请求；不可用时返回 None。

    返回 None 表示不得向用户发出授权卡（无候选、范围不符或会话不可绑定），调用
    方继续按现有证据给出回答。
    """
    if scope.mode != "company_only":
        return None
    needs = payload.get("needs") or []
    metas = _fetch_supplement_reports(scope, needs)
    if not metas:
        return None
    try:
        candidates = SupplementCandidateResolver().resolve(
            scope, needs, metas, _pdf_file_exists, _indexed_report_ids,
        )
    except Exception:
        logger.warning("补充候选解析失败，按现有证据回答", exc_info=True)
        return None
    if not candidates:
        return None

    request = supplement_registry.create(sid, question, scope, candidates)
    stored = chat_store.save_supplement({
        "id": request.id,
        "session_id": sid,
        "question_digest": request.question_digest,
        "scope": scope.to_dict(),
        "candidates": [candidate.to_dict() for candidate in candidates],
        "reason": str(payload.get("reason") or "")[:SUPPLEMENT_REASON_MAX_CHARS],
        "status": request.status,
        "selected_ids": list(request.selected_ids),
        "created_at": request.created_at,
        "expires_at": request.expires_at,
        "consumed_at": request.consumed_at,
    })
    if stored is None:
        # 会话不存在：授权无法绑定，视为不可用（不下载）。
        logger.warning("补充请求无法绑定会话，按现有证据回答：%s", sid)
        return None
    _remember_supplement_reports(request.id, candidates, metas)
    return {
        # 发给浏览器的授权事件载荷：只有可读报告身份，不含下载地址与本地路径。
        "event": {
            "supplement_id": request.id,
            "reason": str(payload.get("reason") or "")[:SUPPLEMENT_REASON_MAX_CHARS],
            "limit": SUPPLEMENT_MAX_CANDIDATES,
            "candidates": [_supplement_candidate_payload(c) for c in candidates],
        },
        # 随 waiting_consent 运行落盘的摘要
        "summary": _supplement_summary(
            status="proposed",
            reason=str(payload.get("reason") or ""),
            candidates=candidates,
        ),
    }


def _supplement_summary(
    *,
    status: str,
    reason: str,
    candidates: List[SupplementCandidate],
    ingested: Optional[List[str]] = None,
    skipped: Optional[List[str]] = None,
    failed: Optional[List[Dict[str, str]]] = None,
    resumed_at: str = "",
) -> Dict[str, Any]:
    """构造 AnswerRun 的补充摘要；字段集受 chat_models 白名单约束。"""
    return {
        "status": status,
        "reason": reason[:SUPPLEMENT_REASON_MAX_CHARS],
        "limit": SUPPLEMENT_MAX_CANDIDATES,
        "candidates": [_supplement_candidate_payload(candidate) for candidate in candidates],
        "ingested_report_ids": list(ingested or []),
        "skipped_report_ids": list(skipped or []),
        "resumed_at": resumed_at,
        "failed": list(failed or []),
    }


def _last_user_question(session: Dict[str, Any]) -> str:
    """从会话中取回原问题：恢复回答必须重放同一问题，从历史消息里读。"""
    for message in reversed(session.get("messages", [])):
        if message.get("role") == "user" and message.get("content"):
            return str(message["content"])
    return ""


def _supplement_question_matches(record: Mapping[str, Any], question: str) -> bool:
    """授权只能恢复到提出问题的那次问答。

    复用登记表创建授权时的同一摘要算法（同一归一化）；摘要缺失或比对失败一律
    视为不匹配，由调用方 fail-closed 拒绝。
    """
    stored = str(record.get("question_digest") or "")
    if not stored:
        return False
    try:
        return SupplementRegistry._digest(question) == stored
    except ValueError:
        return False


def _run_supplement_executor(
    supplement_id: str, pending: SupplementRequest,
) -> SupplementOutcome:
    """在受控路径上顺序下载并摄取已授权候选。

    不向上抛异常：协作方不可用或执行失败都转为只含候选 ID 与受控原因类别的最小
    安全摘要，调用方据此给出诚实说明。
    """
    selected = tuple(pending.selected_ids)
    if downloader is None or rag_service is None:
        reason = "download_unavailable" if downloader is None else "ingest_unavailable"
        return SupplementOutcome(
            failed=selected,
            failure_reasons=tuple((candidate_id, reason) for candidate_id in selected),
        )
    executor = SupplementExecutor(
        downloader, rag_service, REPORTS_DIR, _supplement_reports_for(supplement_id),
    )
    try:
        return executor.run(pending)
    except PermissionError:
        reason = "not_approved"
    except Exception:
        logger.warning("补充财报下载失败", exc_info=True)
        reason = "download_failed"
    return SupplementOutcome(
        failed=selected,
        failure_reasons=tuple((candidate_id, reason) for candidate_id in selected),
    )


def _resume_history(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    """恢复回答的上下文：剔除等待授权的占位轮，也不重复传入原问题。"""
    messages = [
        message for message in session.get("messages", [])
        if not (message.get("role") == "assistant"
                and (message.get("run") or {}).get("status") == "waiting_consent")
    ]
    if messages and messages[-1].get("role") == "user":
        messages = messages[:-1]
    return messages[-8:]


def _resume_chat_tools() -> Optional[List[Dict[str, Any]]]:
    """恢复回答的工具集：去掉补报工具，一个问题的生命周期内只申请一次授权。"""
    try:
        definitions = _build_chat_tool_defs(RagConfig.load()) or []
    except Exception:
        logger.warning("恢复回答工具加载失败，按纯 RAG 回答", exc_info=True)
        return None
    remaining = [
        item for item in definitions
        if item.get("function", {}).get("name") != SUPPLEMENT_REQUEST_TOOL_NAME
    ]
    return remaining or None


@dataclass
class _RagRunState:
    """一次流式问答运行的可变累积状态（SSE 转发与运行持久化共用）。"""

    scope: Optional[Scope] = None
    answer_parts: List[str] = field(default_factory=list)
    pending_tool_args: List[Dict[str, Any]] = field(default_factory=list)
    tool_artifacts: List[Any] = field(default_factory=list)
    facts: List[Any] = field(default_factory=list)
    conflicts: List[Any] = field(default_factory=list)
    intent_decision: Any = None
    tool_policy: Any = None
    verification_report: Any = None
    structured_tool_payloads: List[Dict[str, Any]] = field(default_factory=list)
    evidence_artifacts: List[Any] = field(default_factory=list)
    retrieval_report_ids: List[str] = field(default_factory=list)
    model_name: str = ""
    had_external_failure: bool = False
    retrieval_degraded: bool = False
    done_payload: Optional[Dict[str, Any]] = None
    error_text: str = ""
    empty: bool = False
    supplement_request: Optional[Dict[str, Any]] = None


def _relay_rag_event(
    evt: Dict[str, Any], state: _RagRunState, normalizer: EvidenceNormalizer,
) -> List[str]:
    """把 RagQA 事件翻译为 SSE 帧，并把可持久化证据累积到 state。

    ``/api/chat/stream`` 与补充授权恢复流共用本函数，避免事件语义分叉。
    """
    etype = evt.get("type")
    frames: List[str] = []

    if etype == "delta":
        text = evt.get("text", "")
        if text:
            state.answer_parts.append(text)
            frames.append(_sse("delta", {"text": text}))
    elif etype == "tool_call":
        state.pending_tool_args.append(evt.get("arguments", {}))
        frames.append(_sse("tool_call", {
            "name": evt.get("name", ""), "arguments": evt.get("arguments", {}),
        }))
    elif etype == "reasoning_stage":
        frames.append(_sse("reasoning_stage", {
            "stage": evt.get("stage", ""), "round": evt.get("round", 0),
            "message": evt.get("message", ""),
        }))
    elif etype == "policy_resolved":
        frames.append(_sse("policy_resolved", {key: evt.get(key) for key in ("intent", "allowed_tools", "max_calls", "max_rounds")}))
    elif etype == "structured_tool_result":
        payload = evt.get("payload")
        if isinstance(payload, dict):
            state.structured_tool_payloads.append({"name": evt.get("name", ""), "payload": payload})
    elif etype == "tool_result":
        name = evt.get("name", "")
        summary = evt.get("summary", "")
        ok = bool(evt.get("ok", True))
        args = state.pending_tool_args.pop(0) if state.pending_tool_args else {}
        provider = "web_search" if name == "web_search" else "stock-data-mcp"
        as_of = dt.datetime.now().isoformat(timespec="seconds")
        try:
            artifact = normalizer.normalize_tool_event(
                name, args, summary, provider=provider, as_of=as_of, ok=ok,
            )
        except Exception:
            state.had_external_failure = True
        else:
            state.tool_artifacts.append(artifact)
            if not ok:
                state.had_external_failure = True
            # Raw M1 tool text is retained only as a reference artifact.  M2 facts
            # must originate from the policy-gated structured event below so every
            # persisted value passes FactNormalizer's complete contract.
            # Only the RAG policy-gated JSON-object event is eligible for M2 Fact
            # normalization; raw tool text remains a reference artifact.
            for structured in [item for item in state.structured_tool_payloads if item["name"] == name]:
                raw = dict(structured["payload"])
                # Provider identity and as-of belong to the execution artifact, not
                # to model/tool-controlled JSON, so structured payloads cannot
                # claim a different source or timestamp.
                raw["provider"] = artifact.provider
                raw["as_of"] = artifact.as_of
                fact = FactNormalizer().normalize(raw, artifact, state.scope)
                if fact is not None:
                    state.facts.append(fact)
                    frames.append(_sse("fact", {"fact": fact.to_dict()}))
            state.structured_tool_payloads = [item for item in state.structured_tool_payloads if item["name"] != name]
            frames.append(_sse("artifact", {"artifact": artifact.to_dict()}))
        frames.append(_sse("tool_result", {"name": name, "summary": summary, "ok": ok}))
    elif etype == "supplement_request":
        # 仅登记受控需求；候选解析、授权与下载都由服务端处理。
        state.supplement_request = {
            "reason": str(evt.get("reason") or ""),
            "needs": list(evt.get("needs") or []),
        }
    elif etype == "empty":
        state.empty = True
    elif etype == "error":
        state.error_text = str(evt.get("error") or "未知错误")
    elif etype == "done":
        state.retrieval_report_ids.extend(evt.get("retrieval_report_ids", []) or [])
        state.retrieval_degraded = bool(evt.get("retrieval_degraded", False))
        state.model_name = evt.get("model") or ""
        legacy_citations = evt.get("citations", []) or []
        legacy_web_sources = evt.get("web_sources", []) or []
        # 标准化 PDF/网页证据（analysis 引用只保留在旧 citations 字段一个版本）
        state.evidence_artifacts.extend(normalizer.normalize_rag_citations(
            legacy_citations,
            analysis_dir=ANALYSIS_DIR,
            reports_dir=REPORTS_DIR,
            jump_version=int(time.time() * 1000),
        ))
        state.evidence_artifacts.extend(normalizer.normalize_web_sources(
            legacy_web_sources,
            fetched_at=dt.datetime.now().isoformat(timespec="seconds"),
        ))
        for artifact in state.evidence_artifacts:
            frames.append(_sse("artifact", {"artifact": artifact.to_dict()}))
        state.done_payload = evt

    return frames


def _make_answer_run(
    state: _RagRunState,
    *,
    run_id: str,
    created_at: str,
    started_at: float,
    status: str,
    content: str,
    supplement: Optional[Dict[str, Any]] = None,
    research_run_id: str = "",
    research_summary: Optional[Dict[str, Any]] = None,
) -> AnswerRun:
    """按累积状态构造可持久化的 AnswerRun。"""
    return AnswerRun(
        content=content,
        status=status,
        scope=state.scope,
        facts=tuple(state.facts),
        conflicts=tuple(state.conflicts),
        intent_decision=state.intent_decision,
        tool_policy=state.tool_policy,
        verification_report=state.verification_report,
        artifacts=tuple(state.evidence_artifacts),
        tool_artifacts=tuple(state.tool_artifacts),
        retrieval_report_ids=tuple(state.retrieval_report_ids),
        id=run_id,
        research_run_id=research_run_id,
        research_summary=research_summary,
        created_at=created_at,
        completed_at=dt.datetime.now().isoformat(timespec="seconds"),
        elapsed_seconds=round(time.perf_counter() - started_at, 3),
        model=state.model_name,
        supplement=supplement,
    )


@dataclass(frozen=True)
class _ResearchResumeOrigin:
    """恢复一次研究运行所需的最小原始上下文：原问题、当时上下文与冻结策略。"""

    question: str
    history: List[Dict[str, Any]]
    intent: Optional[IntentDecision]
    policy: Optional[ToolPolicy]


def _preceding_user_index(messages: List[Dict[str, Any]], before: int) -> Optional[int]:
    """返回 ``before`` 之前最近一条有内容的用户消息下标；没有则 None。"""
    for index in range(before - 1, -1, -1):
        message = messages[index]
        if isinstance(message, Mapping) and message.get("role") == "user" and str(message.get("content") or "").strip():
            return index
    return None


def _research_resume_origin(session: Mapping[str, Any], run_id: str) -> _ResearchResumeOrigin:
    """定位该研究运行的原始提问轮次，并取回当时冻结的意图与工具策略。

    恢复必须重放「产生该运行的那条用户问题」，不能拿会话里最新的问题充数；策略也
    必须沿用当时冻结的那一份，不得在恢复时重新解析而扩大范围。两者都只能来自持有
    该 run id 的助手消息及其之前最近的用户消息。若某个候选轮次的策略字段不可读（异
    步写入失败的历史轮次），继续向更早的同 run 轮次回退，一个可读策略都没有时由
    调用方 fail-closed 拒绝；找不到提问轮次同样返回空问题交由调用方拒绝。
    """
    messages = list(session.get("messages", []))
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        saved = message.get("run")
        if not isinstance(saved, Mapping) or saved.get("research_run_id") != run_id:
            continue
        try:
            policy = ToolPolicy.from_dict(saved["tool_policy"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            intent: Optional[IntentDecision] = IntentDecision.from_dict(saved["intent_decision"])
        except (KeyError, TypeError, ValueError):
            intent = None
        origin = _preceding_user_index(messages, index)
        if origin is None:
            return _ResearchResumeOrigin("", [], intent, policy)
        history = [dict(item) for item in messages[:origin] if isinstance(item, Mapping)][-8:]
        return _ResearchResumeOrigin(str(messages[origin].get("content") or ""), history, intent, policy)
    return _ResearchResumeOrigin("", [], None, None)


def _research_step_handlers(
    state: "_RagRunState",
    *,
    question: str,
    history: List[Dict[str, Any]],
    filters: Mapping[str, Any],
    tools: Any,
    priority_report_id: Optional[str],
    scope: Scope,
    policy: ToolPolicy,
    run_id: str,
) -> Dict[str, Callable[[Any, ResearchRun], Dict[str, Any]]]:
    """首次研究与恢复共用的受控步骤 handler。

    两组流程必须走同一组 handler：检索桥把既有 RagQA 的引用、工具产物与归一事实写成
    步骤产物，normalize/compare 再从这些产物算出事实与冲突。恢复路径若另用空 handler，
    就会得到与首次运行不同的 conflicts/verification。
    """
    normalizer = EvidenceNormalizer()
    # 恢复与首次运行共用同一个检索桥：并行 retrieve 步骤只允许发生一次外部调用，
    # 且累积状态不会被并发写入破坏。
    retrieval_lock = threading.Lock()
    cached: Optional[Dict[str, Any]] = None

    def retrieve_research(_step: Any, _run: Any) -> Dict[str, Any]:
        nonlocal cached
        with retrieval_lock:
            if cached is None:
                kwargs = dict(question=question, history=history, filters=filters, tools=tools,
                              priority_report_id=priority_report_id, scope=scope, run_id=run_id)
                # M1 兼容的注入式适配器可能还没有 M2 参数；生产 RagQA 总能收到策略。
                parameters = inspect.signature(rag_qa.answer_stream).parameters.values()
                if any(parameter.name == "tool_policy" or parameter.kind is inspect.Parameter.VAR_KEYWORD
                       for parameter in parameters):
                    kwargs["tool_policy"] = policy
                for event in rag_qa.answer_stream(**kwargs):
                    _relay_rag_event(event, state, normalizer)
                if state.error_text:
                    raise ValueError(state.error_text)
                if state.empty or not (state.evidence_artifacts or state.facts):
                    raise ValueError("未取得可核验的范围内来源")
                cached = {
                    "artifacts": [item.to_dict() for item in state.evidence_artifacts],
                    "facts": [item.to_dict() for item in state.facts],
                    "result_summary": (state.done_payload or {}).get("answer") or "".join(state.answer_parts).strip(),
                }
            return cached

    def merged_facts(current_run: ResearchRun) -> List[Fact]:
        """本次流累积的事实 + 已完成步骤已落盘的事实（按值去重）。

        恢复可以从 normalize/compare 起步，此时检索步骤不会重跑，事实只存在于已完成
        步骤记录里。只读当前累积状态会让冲突检测得到空集，恢复后的 conflicts 与首次
        运行不一致，从而漏掉必须披露的冲突。
        """
        merged: List[Fact] = []
        seen: set[Fact] = set()
        for fact in state.facts:
            if fact not in seen:
                seen.add(fact)
                merged.append(fact)
        for step_run in current_run.step_runs:
            for raw in step_run.facts:
                try:
                    fact = Fact.from_dict(raw)
                except ValueError:
                    continue
                if fact not in seen:
                    seen.add(fact)
                    merged.append(fact)
        return merged

    def normalize_research(_step: Any, current_run: ResearchRun) -> Dict[str, Any]:
        return {"facts": [item.to_dict() for item in merged_facts(current_run)]}

    def compare_research(_step: Any, current_run: ResearchRun) -> Dict[str, Any]:
        state.conflicts = list(detect_conflicts(merged_facts(current_run)))
        return {"conflicts": [item.to_dict() for item in state.conflicts]}

    def verify_research(_step: Any, _run: Any) -> Dict[str, Any]:
        # 步骤级核验是空操作：ResearchAgent 在全部步骤完成后用 M2 ClaimVerifier 核验。
        return {}

    def answer_research(_step: Any, current_run: ResearchRun) -> Dict[str, Any]:
        answer = ((state.done_payload or {}).get("answer") or "".join(state.answer_parts).strip()
                  or current_run.result_of_kind("retrieve"))
        if not answer:
            raise ValueError("未形成可核验的研究结论")
        return {"answer": answer}

    return {"retrieve": retrieve_research, "normalize": normalize_research, "compare": compare_research,
            "verify": verify_research, "answer": answer_research}


def _chat_qa_or_degraded() -> RagQA:
    """返回检索问答器；索引不可用时保留受控外部工具问答。"""
    if rag_qa is not None:
        return rag_qa
    cfg = RagConfig.load()
    return RagQA(
        None,
        ai_client,
        top_k=cfg.top_k,
        tool_executor=_build_chat_tool_executor(cfg) or _unavailable_tool_executor(),
        supplement_request_handler=_handle_supplement_request,
        company_code_resolver=_resolve_symbol_code,
        max_tool_rounds=cfg.mcp_max_tool_rounds,
        max_tool_calls=cfg.mcp_max_tool_calls,
    )


def _mark_research_stopped(sid: str, run_id: str) -> Optional[ResearchRun]:
    """Persist the honest stopped state for a research run whose client disconnected.

    The executor thread can still be inside a blocking external call, so this
    writes the same cooperative outcome the executor will reach: in-flight steps
    become ``stopped`` while every completed step and its evidence stay immutable.
    """
    run = chat_store.get_research_run(sid, run_id)
    if run is None:
        return None
    if run.status in {"completed", "partial", "stopped", "failed"}:
        return run
    for step_run in run.step_runs:
        if step_run.status == "running":
            run = run.with_step_run(step_run.transition("stopped"))
    if run.status != "stopped":
        run = run.transition("stopped")
    chat_store.save_research_run(sid, run)
    return run


@app.post("/api/chat/stream")
async def chat_stream(body: StreamChatRequest, request: Request) -> StreamingResponse:
    """流式全局问答（SSE）。

    事件：session / scope_resolved / run_started / delta / artifact /
    tool_call / tool_result / reasoning_stage / supplement_needed / done / error。

    - session:        会话 id（新建或沿用 body.session_id）
    - scope_resolved: 冻结的 Scope（范围合同）
    - run_started:    本次 AnswerRun 的 id
    - delta:          模型回答内容增量 {text}
    - artifact:       标准化证据/工具 artifact
    - supplement_needed: 模型申请补充财报原文；载荷是受控候选清单
                      {supplement_id, reason, limit, candidates}，此轮以
                      waiting_consent 保存并结束，用户确认后走补充授权接口
    - done:           完整 AnswerRun（scope/facts/artifacts/status），同时保留
                      answer/citations/tools_used/web_sources/retrieval_degraded
                      旧字段一个版本
    - error:          失败 {error}

    所有 completed/partial/stopped/failed 运行都经 chat_store.append_turn()
    原子持久化；停止（断开/取消）保存 stopped，生产异常保存 failed。
    """
    started_at = time.perf_counter()
    _require_ai()
    if not body.question.strip():
        raise HTTPException(400, "问题不能为空")
    chat_qa = _chat_qa_or_degraded()

    session = chat_store.get_or_create(body.session_id)
    sid = session["id"]
    history = session.get("messages", [])[-8:]  # 传给模型的最近 4 轮

    run_id = uuid.uuid4().hex

    # 解析并冻结 Scope（在启动生产线程之前；失败回退全库，不让线程崩溃）
    # run_id 提前生成，使范围解析失败也能与本次问答关联检索。
    scope = await asyncio.to_thread(_resolve_scope, body, run_id)

    # Scope 冻结后才分类；策略是工具定义的唯一权限来源。
    decision = IntentRouter().classify(body.question, scope)
    tools = _build_chat_tool_defs(RagConfig.load()) if body.use_mcp else None
    availability = ToolAvailability.available(*[
        str(item.get("function", {}).get("name") or "") for item in (tools or [])
    ])
    policy = ToolPolicyResolver().resolve(decision, scope, availability)
    # 聚焦报告：解析为 report_id 后提升其检索权重（历史记录跳转场景）
    priority_report_id = None
    fr = body.focus_report or {}
    if fr.get("code") and fr.get("period"):
        try:
            priority_report_id = RagQA.build_report_id(fr["code"], fr["period"])
        except Exception:
            priority_report_id = None

    created_at = dt.datetime.now().isoformat(timespec="seconds")

    async def gen():
        # 同步生成器（rag_qa.answer_stream 内部为阻塞式 requests 流）放在独立
        # 生产者线程执行，经 asyncio.Queue 转发到事件循环 —— 这样多个会话的
        # 流式请求真正并行，一个会话的模型调用不会阻塞其他会话的响应。
        state = _RagRunState(scope=scope, intent_decision=decision, tool_policy=policy)
        normalizer = EvidenceNormalizer()
        pump = _SseEventPump(asyncio.get_running_loop(), "流式问答", run_id=run_id)
        saved = False
        research_run_id = ""

        def _persist(status: str, content: str = "", supplement=None, *, research_run_id: str = "",
                     research_summary=None) -> AnswerRun:
            nonlocal saved
            run = _make_answer_run(
                state, run_id=run_id, created_at=created_at, started_at=started_at,
                status=status, content=content, supplement=supplement,
                research_run_id=research_run_id, research_summary=research_summary,
            )
            chat_store.append_turn(sid, question=body.question, run=run)
            saved = True
            logger.info("chat_run_finished run_id=%s status=%s", run_id, status)
            return run

        def _produce() -> Any:
            # 补报上下文必须落在生产线程（模型调用所在线程）：RagQA 的补报处理器
            # 由该线程回调，只能提交需求，不能决定候选或触发下载。
            _supplement_context.current = {"session_id": sid, "payload": None}
            try:
                kwargs = dict(question=body.question, history=history, filters=body.filters, tools=tools,
                              priority_report_id=priority_report_id, scope=scope, run_id=run_id)
                # M1-compatible injected test adapters may not yet expose the M2
                # argument; production RagQA always receives the policy.
                parameters = inspect.signature(chat_qa.answer_stream).parameters.values()
                if any(parameter.name == "tool_policy" or parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
                    kwargs["tool_policy"] = policy
                yield from chat_qa.answer_stream(**kwargs)
            finally:
                _supplement_context.current = None

        # M3 research_task uses the same policy-gated RagQA path as ordinary
        # chat, but exposes its durable steps through ResearchAgent instead of
        # pretending an empty deterministic plan is evidence.
        is_research = policy.intent == "research_task"
        if not is_research:
            pump.start(_produce)

        try:
            logger.info("chat_run_started run_id=%s", run_id)
            yield _sse("session", {"session_id": sid})
            yield _sse("scope_resolved", {"scope": scope.to_dict()})
            yield _sse("policy_resolved", {"intent": policy.intent, "allowed_tools": list(policy.allowed_tools),
                                             "max_calls": policy.max_calls, "max_rounds": policy.max_rounds})
            # 策略降级说明进入生产事件流：无外部工具或研究任务时用户能知道本次能力的边界。
            if policy.fallback_message and (not policy.allowed_tools or is_research):
                yield _sse("policy_fallback", {
                    "intent": policy.intent,
                    "message": policy.fallback_message,
                })
            yield _sse("run_started", {"run_id": run_id})
            if is_research:
                # Research runs in their own producer thread.  The async side
                # continuously observes disconnects and cooperatively sets the
                # executor event, so a long research never blocks normal chat SSE.
                # The retrieve handler is a production bridge to existing RagQA:
                # Scope and ToolPolicy are passed unchanged, and its citations,
                # tool artifacts and normalized facts become the durable step data.
                research_state = _RagRunState(scope=scope, intent_decision=decision, tool_policy=policy)
                handlers = _research_step_handlers(
                    research_state, question=body.question, history=history, filters=body.filters, tools=tools,
                    priority_report_id=priority_report_id, scope=scope, policy=policy, run_id=run_id,
                )
                def persist_research(research_run):
                    nonlocal research_run_id
                    research_run_id = research_run.id
                    chat_store.save_research_run(sid, research_run)

                loop = asyncio.get_running_loop()
                research_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
                stop_event = threading.Event()
                agent = ResearchAgent(executor=ResearchExecutor(handlers), persist=persist_research)
                def emit_research(event: dict[str, Any]) -> None:
                    loop.call_soon_threadsafe(research_events.put_nowait, event)
                research_task: asyncio.Task[Any] | None = None
                try:
                    research_task = asyncio.create_task(asyncio.to_thread(
                        agent.run, body.question, scope, decision, policy,
                        stop_event=stop_event, emit=emit_research,
                    ))
                    while not research_task.done():
                        if await request.is_disconnected():
                            stop_event.set()
                        try:
                            event = await asyncio.wait_for(research_events.get(), timeout=0.05)
                        except asyncio.TimeoutError:
                            continue
                        yield _sse(event["type"], {key: value for key, value in event.items() if key != "type"})
                    research_answer = await research_task
                    while not research_events.empty():
                        event = research_events.get_nowait()
                        yield _sse(event["type"], {key: value for key, value in event.items() if key != "type"})
                finally:
                    # Starlette cancels this generator when the client disconnects, so
                    # the polling loop above cannot always observe it.  Setting the
                    # shared event here keeps the executor cooperative instead of
                    # letting a later completion masquerade as the user's run.
                    if research_task is not None and not research_task.done():
                        stop_event.set()
                run = replace(
                    research_answer, id=run_id, created_at=created_at,
                    completed_at=dt.datetime.now().isoformat(timespec="seconds"),
                    elapsed_seconds=round(time.perf_counter() - started_at, 3),
                )
                chat_store.append_turn(sid, question=body.question, run=run)
                saved = True
                yield _sse("done", {
                    "answer": run.content, "citations": [], "session_id": sid,
                    "tools_used": [], "web_sources": [], "retrieval_degraded": False,
                    "run": run.to_dict(), "research_run": agent.last_run.to_dict() if agent.last_run else None,
                    "elapsed_seconds": round(time.perf_counter() - started_at, 3),
                })
                return
            while True:
                evt = await pump.queue.get()
                if evt is pump.sentinel:
                    break
                # 客户端已断开（点击「停止」）：终止生成，保留已产出部分
                if await request.is_disconnected():
                    break
                for frame in _relay_rag_event(evt, state, normalizer):
                    yield frame

                if state.supplement_request is not None:
                    # 模型申请补充财报：解析受控候选并暂停等待用户授权。
                    proposal = _propose_supplement(
                        sid, body.question, scope, state.supplement_request
                    )
                    if proposal is not None:
                        run = _persist(
                            "waiting_consent", _SUPPLEMENT_WAITING_TEXT,
                            supplement=proposal["summary"],
                        )
                        yield _sse("supplement_needed", {
                            **proposal["event"],
                            "session_id": sid,
                            "run": run.to_dict(),
                            "elapsed_seconds": round(time.perf_counter() - started_at, 3),
                        })
                        return
                    # 无可用候选：按真实原因给出诚实说明，不假装可以补充。
                    unavailable = _supplement_unavailable_text(scope)
                    run = _persist("partial", unavailable)
                    yield _sse("done", {
                        "answer": unavailable,
                        "citations": [],
                        "session_id": sid,
                        "run": run.to_dict(),
                        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
                    })
                    return

                if state.empty:
                    default = "知识库中未检索到相关内容，请补充更多报告或更换问法。"
                    run = _persist("completed", default)
                    yield _sse("done", {
                        "answer": default,
                        "citations": [],
                        "session_id": sid,
                        "run": run.to_dict(),
                        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
                    })
                    return
                if state.error_text:
                    run = _persist("failed", "".join(state.answer_parts).strip())
                    yield _sse("error", {
                        "error": f"流式问答失败，请重试（诊断 ID：{run_id}）",
                        "run": run.to_dict(),
                        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
                    })
                    return
                if state.done_payload is not None:
                    done_evt = state.done_payload
                    answer = done_evt.get("answer") or ""
                    legacy_citations = done_evt.get("citations", []) or []
                    legacy_web_sources = done_evt.get("web_sources", []) or []
                    legacy_tools_used = done_evt.get("tools_used", []) or []
                    state.conflicts = list(detect_conflicts(state.facts))
                    for conflict in state.conflicts:
                        yield _sse("conflict", {"conflict": conflict.to_dict()})
                    state.verification_report = ClaimVerifier().verify(
                        answer, scope, state.facts, state.evidence_artifacts, state.conflicts,
                    )
                    yield _sse("verification", {"verification": state.verification_report.to_dict()})
                    if state.verification_report.status == "blocked":
                        # 只替换不受支持的数值论断；受支持内容与上下文保留。
                        answer = ClaimVerifier().degrade_blocked(answer, scope, state.facts)
                    elif state.verification_report.status == "partial":
                        answer = answer.rstrip() + "\n\n存在口径/时间差异或外部参考，请结合来源核对。"
                    status = (
                        "partial" if (state.had_external_failure or state.retrieval_degraded or state.verification_report.status == "partial")
                        else "completed"
                    )
                    run = _persist(status, answer)
                    yield _sse("done", {
                        "answer": answer,
                        "citations": legacy_citations,
                        "session_id": sid,
                        "tools_used": legacy_tools_used,
                        "web_sources": legacy_web_sources,
                        "retrieval_degraded": state.retrieval_degraded,
                        "run": run.to_dict(),
                        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
                    })
                    return
        except Exception as exc:
            logger.error("chat_run_failed run_id=%s error_type=%s", run_id, type(exc).__name__)
            if not saved:
                _persist("failed", "".join(state.answer_parts).strip())
            yield _sse("error", {
                "error": f"流式问答失败，请重试（诊断 ID：{run_id}）",
                "elapsed_seconds": round(time.perf_counter() - started_at, 3),
            })
        finally:
            pump.stop()
            # 未正常完成（停止/断开/没有 done）：保存为 stopped，不伪装完整；
            # 研究运行的步骤产物已由同一 persist 回调落盘，这里只补上引用与安全摘要。
            if not saved:
                stopped_summary = None
                stopped_content = "".join(state.answer_parts).strip()
                if research_run_id:
                    stopped_run = _mark_research_stopped(sid, research_run_id)
                    if stopped_run is not None:
                        stopped_summary = {"status": stopped_run.status,
                                           "resume_from_step_id": stopped_run.resume_from_step_id}
                    stopped_content = stopped_content or "研究已停止；已完成步骤已保存，可继续研究。"
                _persist("stopped", stopped_content,
                         research_run_id=research_run_id, research_summary=stopped_summary)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/chat/research/{run_id}")
def get_research_run(run_id: str, session_id: str = Query(...)) -> Dict[str, Any]:
    run = chat_store.get_research_run(session_id, run_id)
    if run is None:
        raise HTTPException(404, "研究运行不存在")
    return {"run": run.to_dict()}


@app.post("/api/chat/research/{run_id}/resume")
async def resume_research_run(run_id: str, body: ResumeResearchRequest, request: Request) -> StreamingResponse:
    research_run = chat_store.get_research_run(body.session_id, run_id)
    if research_run is None:
        raise HTTPException(404, "研究运行不存在")
    if research_run.status not in {"stopped", "partial", "failed"}:
        raise HTTPException(409, "该研究运行不可恢复")
    session = chat_store.get_session(body.session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    origin = _research_resume_origin(session, run_id)
    if origin.policy is None:
        raise HTTPException(409, "缺少原始工具策略，不能安全恢复研究")
    if not origin.question.strip():
        raise HTTPException(409, "找不到该研究运行的原研究问题，不能安全恢复")

    async def gen():
        # 恢复使用与首次研究流同一组受控 handler，且只用已完成步骤之外要重跑的步骤。
        state = _RagRunState(scope=research_run.plan.scope, intent_decision=origin.intent, tool_policy=origin.policy)
        handlers = _research_step_handlers(
            state, question=origin.question, history=origin.history, filters={}, tools=None,
            priority_report_id=None, scope=research_run.plan.scope, policy=origin.policy, run_id=run_id,
        )
        agent = ResearchAgent(executor=ResearchExecutor(handlers),
                              persist=lambda saved: chat_store.save_research_run(body.session_id, saved))
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        stop_event = threading.Event()

        def emit(event: Dict[str, Any]) -> None:
            loop.call_soon_threadsafe(events.put_nowait, event)

        resume_task: asyncio.Task[Any] | None = None
        try:
            resume_task = asyncio.create_task(asyncio.to_thread(
                agent.resume, research_run, stop_event=stop_event, emit=emit,
                intent=origin.intent, policy=origin.policy,
            ))
            while not resume_task.done():
                # 与首次研究流一致：客户端断开（停止）时置协作停止事件，让阻塞中的外部
                # 调用返回后按 stopped 落盘，而不是把它当成完整研究。
                if await request.is_disconnected():
                    stop_event.set()
                try:
                    event = await asyncio.wait_for(events.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                yield _sse(event["type"], {key: value for key, value in event.items() if key != "type"})
            answer = await resume_task
            while not events.empty():
                event = events.get_nowait()
                yield _sse(event["type"], {key: value for key, value in event.items() if key != "type"})
        except ValueError:
            yield _sse("research_blocked", {"reason": "没有可安全恢复的未完成步骤。"})
            return
        finally:
            if resume_task is not None and not resume_task.done():
                stop_event.set()
        answer = replace(answer, id=uuid.uuid4().hex, created_at=dt.datetime.now().isoformat(timespec="seconds"),
                         completed_at=dt.datetime.now().isoformat(timespec="seconds"))
        chat_store.append_turn(body.session_id, question=origin.question, run=answer)
        yield _sse("done", {"session_id": body.session_id, "run": answer.to_dict(), "answer": answer.content})
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/chat/supplements/{supplement_id}/resolve")
async def resolve_chat_supplement(
    supplement_id: str, body: ResolveSupplementRequest, request: Request,
) -> StreamingResponse:
    """用户对补充财报授权请求的决定，SSE 返回恢复后的回答。

    事件：session / supplement_download_started / supplement_downloaded /
    supplement_ingested / supplement_failed / run_started / delta / artifact /
    tool_call / tool_result / reasoning_stage / done / error。

    授权只在一次性登记表中生效且与会话绑定：跨会话、重放、过期、超限、篡改
    候选都返回 409 且不调用下载器；拒绝或全部失败仍基于现有证据给出回答。
    """
    started_at = time.perf_counter()
    _require_ai()
    if rag_qa is None:
        raise HTTPException(503, "RAG 知识库未初始化：请配置 rag.enabled 并执行索引")

    record = chat_store.get_supplement(supplement_id)
    if record is None:
        raise HTTPException(404, f"未知补充请求：{supplement_id}")
    if record.get("session_id") != body.session_id:
        raise HTTPException(409, "补充请求不属于当前会话")
    session = chat_store.get_session(body.session_id)
    if session is None:
        raise HTTPException(409, "补充请求所属会话不存在")
    question = _last_user_question(session)
    if not question.strip():
        raise HTTPException(409, "会话中找不到原问题，无法恢复回答")
    if not _supplement_question_matches(record, question):
        # 提出问题后又问了别的（会话末条用户消息已被替换），或摘要缺失：
        # fail-closed 拒绝，绝不触碰登记表、下载器与执行器。
        raise HTTPException(409, "补充请求与当前问题不匹配，请重新提问后再补充")

    try:
        if body.action == "decline":
            pending = supplement_registry.decline(supplement_id, body.session_id)
            # decline 是终态转换，必须在生成回答前写入审计存储。
            _persist_supplement_status(supplement_id, pending.status)
        else:
            pending = supplement_registry.approve(
                supplement_id, body.session_id, body.candidate_ids or [],
            )
    except KeyError:
        # 一次性登记表已失效（如进程重启）：fail-closed，不凭落盘记录放开下载。
        raise HTTPException(404, f"未知补充请求：{supplement_id}")
    except (PermissionError, ValueError) as exc:
        raise HTTPException(409, f"补充请求不可用：{exc}")

    sid = body.session_id
    candidates = list(pending.candidates)
    reason = str(record.get("reason") or "")
    history = _resume_history(session)
    run_id = uuid.uuid4().hex
    created_at = dt.datetime.now().isoformat(timespec="seconds")
    state = _RagRunState(scope=pending.scope)
    ingested: List[str] = []
    skipped: List[str] = []
    failed: List[Dict[str, str]] = []
    resumed_at = ""
    saved = False
    # 授权有效化但尚未取得证据时先记为失败；成功摄取后置为 completed。
    supplement_status = "declined" if body.action == "decline" else "failed"

    def _summary(status: str) -> Dict[str, Any]:
        return _supplement_summary(
            status=status, reason=reason, candidates=candidates,
            ingested=ingested, skipped=skipped, failed=failed, resumed_at=resumed_at,
        )

    def _advance(status: str) -> None:
        """推进登记表状态并落盘；失败只记日志，不影响回答产出。"""
        try:
            supplement_registry.transition(supplement_id, status)
        except (KeyError, ValueError):
            logger.warning("补充请求状态推进失败：%s", status, exc_info=True)
        _persist_supplement_status(supplement_id, status)

    async def gen():
        nonlocal saved, supplement_status, resumed_at
        normalizer = EvidenceNormalizer()
        pump = _SseEventPump(asyncio.get_running_loop(), "恢复回答", run_id=run_id)

        def _persist(status: str, content: str = "") -> AnswerRun:
            nonlocal saved
            run = _make_answer_run(
                state, run_id=run_id, created_at=created_at, started_at=started_at,
                status=status, content=content, supplement=_summary(supplement_status),
            )
            chat_store.append_turn(sid, question=question, run=run)
            saved = True
            return run

        def _run_status() -> str:
            if (body.action == "decline" or failed
                    or state.had_external_failure or state.retrieval_degraded):
                return "partial"
            return "completed"

        def _done(answer: str, citations: List[Any], done_evt: Dict[str, Any]) -> str:
            run = _persist(_run_status(), answer)
            return _sse("done", {
                "answer": answer,
                "citations": citations,
                "session_id": sid,
                "tools_used": done_evt.get("tools_used", []) or [],
                "web_sources": done_evt.get("web_sources", []) or [],
                "retrieval_degraded": state.retrieval_degraded,
                "run": run.to_dict(),
                "elapsed_seconds": round(time.perf_counter() - started_at, 3),
            })

        try:
            yield _sse("session", {"session_id": sid})

            if body.action == "approve":
                # 已授权：下载 → 摄取 → 恢复回答；候选已在提出阶段受控解析。
                yield _sse("supplement_download_started", {
                    "supplement_id": supplement_id,
                    "candidate_ids": list(pending.selected_ids),
                })
                _advance("downloading")
                outcome = await asyncio.to_thread(
                    _run_supplement_executor, supplement_id, pending
                )
                ingested.extend(outcome.ingested_report_ids)
                skipped.extend(outcome.skipped_report_ids)
                failed.extend(
                    {"candidate_id": candidate_id, "reason": reason_text}
                    for candidate_id, reason_text in outcome.failure_reasons
                )
                for report_id in outcome.downloaded_report_ids:
                    yield _sse("supplement_downloaded", {"report_id": report_id, "skipped": False})
                for report_id in outcome.skipped_report_ids:
                    yield _sse("supplement_downloaded", {"report_id": report_id, "skipped": True})
                for report_id in outcome.ingested_report_ids:
                    yield _sse("supplement_ingested", {"report_id": report_id})
                for candidate_id, reason_text in outcome.failure_reasons:
                    yield _sse("supplement_failed", {
                        "candidate_id": candidate_id, "reason": reason_text,
                    })
                if outcome.ingested_report_ids:
                    _advance("ingesting")
                    _advance("resuming")
                    supplement_status = "completed"
                    # 只有真正取得 PDF 索引证据的报告才进入恢复范围。
                    state.scope = replace(
                        pending.scope,
                        report_ids=pending.scope.report_ids + tuple(outcome.ingested_report_ids),
                    )
                else:
                    # 未取得任何可用证据：授权无效化，但仍给出诚实回答。
                    _advance("failed")
                    logger.info("补充财报未取得可用证据：%s", supplement_id)

            resumed_at = dt.datetime.now().isoformat(timespec="seconds")
            yield _sse("run_started", {"run_id": run_id})

            resume_scope = state.scope
            pump.start(lambda: rag_qa.answer_stream(
                question, history=history, filters=None, tools=_resume_chat_tools(),
                priority_report_id=None, scope=resume_scope, run_id=run_id,
            ))
            while True:
                evt = await pump.queue.get()
                if evt is pump.sentinel:
                    break
                # 客户端已断开（点击「停止」）：保留已产出部分；授权已一次性消费，
                # 登记表停在此后的状态，断开也不能重放。
                if await request.is_disconnected():
                    break
                for frame in _relay_rag_event(evt, state, normalizer):
                    yield frame

                if state.empty:
                    # 补充仍未取得证据时，说明这轮回答只覆盖了现有资料。
                    answer = (
                        _SUPPLEMENT_UNAVAILABLE_TEXT
                        if body.action == "approve" and not ingested
                        else "知识库中未检索到相关内容，请补充更多报告或更换问法。"
                    )
                    if supplement_status == "completed":
                        # 已摄取完成但没有可用答题证据：登记表同步推进到 completed，
                        # 避免停在 resuming 与运行摘要不一致。
                        _advance("completed")
                    run = _persist("partial", answer)
                    yield _sse("done", {
                        "answer": answer,
                        "citations": [],
                        "session_id": sid,
                        "run": run.to_dict(),
                        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
                    })
                    return
                if state.error_text:
                    if supplement_status == "completed":
                        # 已摄取但恢复回答失败：补充摘要据实记为失败，不冒充成功。
                        _advance("failed")
                        supplement_status = "failed"
                    run = _persist("failed", "".join(state.answer_parts).strip())
                    yield _sse("error", {
                        "error": f"恢复问答失败，请重试（诊断 ID：{run_id}）",
                        "run": run.to_dict(),
                        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
                    })
                    return
                if state.done_payload is not None:
                    done_evt = state.done_payload
                    if supplement_status == "completed":
                        _advance("completed")
                    yield _done(
                        done_evt.get("answer") or "",
                        done_evt.get("citations", []) or [],
                        done_evt,
                    )
                    return
        except Exception as exc:
            logger.warning(
                "chat_supplement_resume_failed run_id=%s error_type=%s",
                run_id, type(exc).__name__,
            )
            if not saved:
                _persist("failed", "".join(state.answer_parts).strip())
            yield _sse("error", {
                "error": f"恢复问答失败，请重试（诊断 ID：{run_id}）",
                "elapsed_seconds": round(time.perf_counter() - started_at, 3),
            })
        finally:
            pump.stop()
            # 未正常完成（停止/断开/没有 done）：保存为 stopped，不伪装完整。
            # 断开时登记表停留在 resuming/failed，绝不回到 proposed：授权已一次性
            # 消费，断开也不能重放或重新下载。
            if not saved:
                _persist("stopped", "".join(state.answer_parts).strip())

    return StreamingResponse(gen(), media_type="text/event-stream")


# ── M4：研究工作台 / 显式记忆 / 导出 / 离线质量摘要 ───────────────

def _owned_answer_run(session_id: str, run_id: str) -> tuple[AnswerRun, ResearchRun | None] | None:
    """Return an immutable run only when its persisted session owner matches.

    M4 actions deliberately do not accept a bare run id: a run must be located
    through its session-owned ChatStore record before it can be favorited, saved,
    or exported.
    """
    for record in chat_store.iter_session_runs():
        if record.session_id == session_id and record.run.id == run_id:
            return record.run, record.research_run
    return None


def _owned_or_404(session_id: str, run_id: str) -> tuple[AnswerRun, ResearchRun | None]:
    owned = _owned_answer_run(session_id, run_id)
    if owned is None:
        # Do not distinguish a foreign run from an unknown run.
        raise HTTPException(404, "研究运行不存在")
    return owned


def _owned_run_inside_memory_lock(session_id: str, run_id: str) -> tuple[AnswerRun, ResearchRun | None]:
    """Re-verify run ownership while holding ``_research_memory_lock``.

    A save request can pass its lock-free pre-check and then wait for the
    coordination lock while its session is deleted by a parallel request.  The
    write must be rejected there instead of landing in a deleted session's memory.
    """
    return _owned_or_404(session_id, run_id)


def _memory_store_for_owned_run(run: AnswerRun, owner_session_id: str) -> ResearchMemoryStore:
    """Bind a short-lived store to one already owner-checked run and session.

    ``ResearchMemoryStore.save_*`` intentionally resolves the source itself.
    Giving that lookup only this request's run preserves its fail-closed contract
    without granting the memory layer a cross-session run lookup, and the owner is
    the session this request already re-verified, never a client-supplied value.
    """
    return ResearchMemoryStore(
        research_memory.path,
        run_lookup=lambda requested_run_id: run if requested_run_id == run.id else None,
        owner_session_id=owner_session_id,
    )


@app.get("/api/research/workspace")
def get_research_workspace(
    company_code: Optional[str] = Query(default=None, pattern=r"^\d{6}$"),
    company: Optional[str] = Query(default=None, pattern=r"^\d{6}$"),
    industry: Optional[str] = Query(default=None, min_length=1, max_length=100),
    period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    intent: Optional[Literal["report_fact", "company_trend", "industry_benchmark", "realtime_market", "event_attribution", "research_task"]] = None,
    status: Optional[Literal["completed", "partial", "stopped", "failed", "waiting_consent"]] = None,
    text: Optional[str] = Query(default=None, max_length=100),
    favorite_only: bool = Query(default=False),
) -> Dict[str, Any]:
    """List metadata-only workspace rows; never inspect message/PDF/tool bodies."""
    if company_code and company and company_code != company:
        raise HTTPException(422, "company 与 company_code 必须一致")
    if period is not None:
        try:
            # The query pattern only checks shape; an impossible date must not be
            # silently treated as an empty filter result.
            dt.date.fromisoformat(period)
        except ValueError as exc:
            raise HTTPException(422, "period 必须是真实存在的日期") from exc
    query = ResearchWorkspaceQuery(
        company_code=company_code or company,
        industry=industry,
        period=period,
        intent=intent,
        status=status,
        text=text,
        favorite_only=favorite_only,
    )
    return {"items": [item.to_dict() for item in research_workspace.list_items(query)]}


@app.patch("/api/research/runs/{run_id}/favorite")
def set_research_run_favorite(
    run_id: str,
    body: FavoriteRunRequest,
    session_id: str = Query(..., min_length=1, max_length=128),
) -> Dict[str, Any]:
    _owned_or_404(session_id, run_id)
    try:
        item = research_workspace.set_favorite(session_id, run_id, body.favorite)
    except ValueError as exc:
        raise HTTPException(404, "研究运行不存在") from exc
    return {"item": item.to_dict()}


@app.get("/api/research/runs/{run_id}/export")
def export_research_run(
    run_id: str,
    session_id: str = Query(..., min_length=1, max_length=128),
    format: Literal["markdown", "json"] = Query(default="markdown"),
) -> Any:
    run, research_run = _owned_or_404(session_id, run_id)
    exporter = ResearchExporter()
    try:
        if format == "json":
            return exporter.to_json(run, research_run)
        return PlainTextResponse(exporter.to_markdown(run, research_run), media_type="text/markdown")
    except ExportValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/research/memory")
def list_research_memory(
    owner_session_id: Optional[str] = Query(default=None, min_length=1, max_length=128),
) -> Dict[str, Any]:
    """List active explicit memories, optionally scoped to one owner session.

    The store records the owner session that already passed the save request's
    ownership check.  This app serves a single local user with no authentication, so
    listing this machine's own active memories does not widen access; entries saved
    before owners were recorded are excluded instead of being guessed into a scope.
    """
    entries = ResearchMemoryStore(research_memory.path).list_owned_active(owner_session_id)
    return {"entries": [entry.to_dict() for entry in entries]}


@app.post("/api/research/memory/facts")
def save_research_fact_memory(
    body: SaveFactMemoryRequest,
    session_id: str = Query(..., min_length=1, max_length=128),
) -> Dict[str, Any]:
    # Lock-free pre-check: unknown or foreign runs never wait for the lock.
    _owned_or_404(session_id, body.run_id)
    with _research_memory_lock:
        run, _research_run = _owned_run_inside_memory_lock(session_id, body.run_id)
        facts = [fact for fact in run.facts if body.fact_id in fact.evidence_ids]
        if len(facts) != 1:
            raise HTTPException(422, "事实标识不存在或不唯一")
        try:
            entry = _memory_store_for_owned_run(run, session_id).save_fact(facts[0], body.run_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return {"entry": entry.to_dict()}


@app.post("/api/research/memory/artifacts")
def save_research_artifact_memory(
    body: SaveArtifactMemoryRequest,
    session_id: str = Query(..., min_length=1, max_length=128),
) -> Dict[str, Any]:
    # Lock-free pre-check: unknown or foreign runs never wait for the lock.
    _owned_or_404(session_id, body.run_id)
    with _research_memory_lock:
        run, _research_run = _owned_run_inside_memory_lock(session_id, body.run_id)
        artifacts = [artifact for artifact in run.artifacts if body.artifact_id in artifact_evidence_ids(artifact)]
        if len(artifacts) != 1:
            raise HTTPException(422, "证据标识不存在或不唯一")
        try:
            entry = _memory_store_for_owned_run(run, session_id).save_artifact(artifacts[0], body.run_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return {"entry": entry.to_dict()}


@app.post("/api/research/memory/decisions")
def save_research_decision_memory(
    body: SaveDecisionMemoryRequest,
    session_id: str = Query(..., min_length=1, max_length=128),
) -> Dict[str, Any]:
    # Lock-free pre-check: unknown or foreign runs never wait for the lock.
    _owned_or_404(session_id, body.run_id)
    with _research_memory_lock:
        run, _research_run = _owned_run_inside_memory_lock(session_id, body.run_id)
        evidence_ids = tuple(body.evidence_ids)
        if len(set(evidence_ids)) != len(evidence_ids) or not set(evidence_ids).issubset(run_evidence_ids(run)):
            raise HTTPException(422, "研究决策必须引用来源运行中的证据")
        try:
            entry = _memory_store_for_owned_run(run, session_id).save_decision(body.text, body.run_id, evidence_ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return {"entry": entry.to_dict()}


@app.delete("/api/research/memory/{entry_id}")
def revoke_research_memory(entry_id: str) -> Dict[str, Any]:
    with _research_memory_lock:
        revoked = ResearchMemoryStore(research_memory.path).revoke(entry_id)
    if not revoked:
        raise HTTPException(404, "研究记忆不存在或已撤销")
    return {"revoked": True, "id": entry_id}


_QUALITY_METRIC_FIELDS = (
    "citation_coverage", "scope_precision", "page_link_pass_rate", "tool_success_rate",
    "stop_recovery_pass_rate", "p95_stage_duration",
)


def _safe_failure_codes(value: object) -> Dict[str, int] | None:
    """Project persisted failure counts onto the fixed public whitelist.

    An unknown code means the sidecar is not a summary this endpoint understands,
    so the whole summary becomes unavailable instead of being partially exposed.
    Only non-negative integer counts survive (booleans are rejected), and zero
    counts are dropped so a stale code can never be revived.
    """
    if not isinstance(value, Mapping):
        return None
    counts: Dict[str, int] = {}
    for code, count in value.items():
        if code not in FAILURE_CODES:
            return None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        if count:
            counts[code] = count
    return {code: counts[code] for code in sorted(counts)}


def _load_precomputed_quality_summary() -> Dict[str, Any]:
    """Read only safe aggregate fields; this endpoint never executes evaluation."""
    try:
        with open(QUALITY_SUMMARY_PATH, encoding="utf-8") as source:
            raw = json.load(source)
    except (OSError, json.JSONDecodeError):
        return {"available": False}
    if not isinstance(raw, Mapping):
        return {"available": False}
    case_count = raw.get("case_count")
    if not isinstance(raw.get("passed"), bool) or isinstance(case_count, bool) or not isinstance(case_count, int):
        return {"available": False}
    if any(isinstance(raw.get(name), bool) or not isinstance(raw.get(name), (int, float)) for name in _QUALITY_METRIC_FIELDS):
        return {"available": False}
    failure_codes = _safe_failure_codes(raw.get("failure_codes"))
    if failure_codes is None:
        return {"available": False}
    return {
        "available": True,
        "passed": raw["passed"],
        "case_count": case_count,
        "failure_codes": failure_codes,
        **{name: raw[name] for name in _QUALITY_METRIC_FIELDS},
    }


@app.get("/api/research/quality")
def get_research_quality() -> Dict[str, Any]:
    return _load_precomputed_quality_summary()


@app.get("/api/chat/sessions")
def list_chat_sessions() -> Dict[str, Any]:
    """会话列表（摘要，按更新时间降序）"""
    return {"sessions": chat_store.list_sessions()}


@app.get("/api/chat/sessions/{sid}")
def get_chat_session(sid: str) -> Dict[str, Any]:
    """会话详情（含完整消息），用于跳转历史会话继续问答"""
    session = chat_store.get_session(sid)
    if session is None:
        raise HTTPException(404, f"未知会话：{sid}")
    return session


@app.post("/api/chat/sessions")
def create_chat_session() -> Dict[str, Any]:
    """显式新建空会话；若已有未对话的空会话则直接复用（锚定过去）"""
    return {"session_id": chat_store.get_or_create_empty()["id"]}


@app.patch("/api/chat/sessions/{sid}")
def rename_chat_session(sid: str, body: RenameSessionRequest) -> Dict[str, Any]:
    """重命名会话标题"""
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    session = chat_store.rename_session(sid, title)
    if session is None:
        raise HTTPException(404, f"未知会话：{sid}")
    return session


@app.delete("/api/chat/sessions/{sid}")
def delete_chat_session(sid: str) -> Dict[str, Any]:
    """Delete a session while retaining independent explicit memory records.

    The deletion commits first and the disclosed count is then read while holding
    the coordination lock.  Any save that resolved its immutable run before the
    deletion is therefore counted (it wrote inside the same lock), while a save
    that reaches the lock afterwards re-verifies ownership and is rejected instead
    of silently dropping out of the count.
    """
    owned_run_ids = {record.run.id for record in chat_store.iter_session_runs() if record.session_id == sid}
    if not chat_store.delete_session(sid):
        raise HTTPException(404, f"未知会话：{sid}")
    with _research_memory_lock:
        retained_memory_count = sum(
            entry.source_run_id in owned_run_ids for entry in ResearchMemoryStore(research_memory.path).list_active()
        )
    return {
        "ok": True,
        "session_id": sid,
        "retained_memory_count": retained_memory_count,
    }


@app.get("/api/rag/status")
def rag_status() -> Dict[str, Any]:
    if rag_service is None:
        return {"enabled": False, "reports": {}, "total_chunks": 0}
    st = rag_service.status()
    return {"enabled": True, **st}


@app.post("/api/rag/ingest")
def rag_ingest() -> Dict[str, Any]:
    if rag_service is None:
        raise HTTPException(503, "RAG 知识库未初始化：请配置 rag.enabled")
    task_id = task_manager.submit(lambda: asdict(rag_service.ingest_all()))
    if task_id is None:
        raise HTTPException(409, "已有任务进行中，请稍候")
    return {"task_id": task_id}


@app.get("/api/rag/files")
def rag_files() -> Dict[str, Any]:
    if rag_service is None:
        return {"enabled": False, "items": [], "stats": {"added": 0, "not_added": 0, "total_chunks": 0}}
    items = rag_service.list_files()
    stats = {
        "added": sum(1 for it in items if it["added"]),
        "not_added": sum(1 for it in items if not it["added"]),
        "total_chunks": rag_store.count_chunks() if rag_store is not None else 0,
    }
    return {"enabled": True, "items": items, "stats": stats}


@app.post("/api/rag/ingest/one")
def rag_ingest_one(body: RagIngestOneRequest) -> Dict[str, Any]:
    if rag_service is None:
        raise HTTPException(503, "RAG 知识库未初始化：请配置 rag.enabled")
    if body.source not in ("pdf", "analysis"):
        raise HTTPException(400, "source 仅支持 pdf / analysis")

    def _run() -> None:
        rag_service.ingest_file(body.report_id, body.source)

    task_id = task_manager.submit(_run)
    if task_id is None:
        raise HTTPException(409, "已有任务进行中，请稍候")
    return {"task_id": task_id}


@app.delete("/api/rag/index/{report_id}/{source}")
def rag_delete_index(report_id: str, source: str) -> Dict[str, Any]:
    if rag_service is None:
        raise HTTPException(503, "RAG 知识库未初始化：请配置 rag.enabled")
    if source not in ("pdf", "analysis"):
        raise HTTPException(400, "source 仅支持 pdf / analysis")
    rag_service.delete_file_index(report_id, source)
    return {"ok": True}


# ── 静态页面 ───────────────────────────────────────────────

def _render_index() -> HTMLResponse:
    """渲染首页 HTML，并把前端脚本版本号替换为最新修改时间。

    这样每次修改前端脚本后版本号自动变化，浏览器强制加载新版，
    无需手动清缓存，也无需手动维护版本号。
    """
    index_path = os.path.join(STATIC_DIR, "index.html")
    versioned_assets = (
        os.path.join(STATIC_DIR, "app.js"),
        os.path.join(STATIC_DIR, "analysis_workflow.js"),
        os.path.join(STATIC_DIR, "analysis_visualizations.js"),
        os.path.join(STATIC_DIR, "chat_rendering.js"),
        os.path.join(STATIC_DIR, "style.css"),
    )
    with open(index_path, encoding="utf-8") as f:
        html = f.read()
    try:
        version = str(int(max(os.path.getmtime(path) for path in versioned_assets)))
    except OSError:
        version = "0"
    # 首页禁用缓存（no-cache：每次重新验证），确保拿到最新版本号从而加载最新静态资源
    return HTMLResponse(
        html.replace("__APP_VERSION__", version),
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/")
def index() -> HTMLResponse:
    return _render_index()


@app.get("/analysis")
def analysis_page() -> HTMLResponse:
    return _render_index()


@app.get("/history")
def history_page() -> HTMLResponse:
    return _render_index()


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
