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
import logging
import logging.handlers
import os
import re
import threading
import time
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from financial_report_fetcher.ai_client import AIClient
from financial_report_fetcher.analyzer import (
    ANALYSIS_TEMPLATES,
    ReportAnalyzer,
    clean_analysis_payload,
)
from financial_report_fetcher.datasource import CNINFODatasource
from financial_report_fetcher.downloader import ReportDownloader
from financial_report_fetcher.models import DownloadStatus, ReportMeta, ReportType
from financial_report_fetcher.market import stock_mcp, tencent_quote
from financial_report_fetcher.rag.analysis import RagAnalysis
from financial_report_fetcher.rag.mcp_tools import build_tool_defs
from financial_report_fetcher.rag.config import RagConfig
from financial_report_fetcher.rag.embedding import LocalEmbedder
from financial_report_fetcher.rag.ingest import IngestionService
from financial_report_fetcher.rag.qa import RagQA
from financial_report_fetcher.rag.store import RagStore

from .autocomplete import StockIndex
from .chat_store import ChatStore
from .mcp_guard import McpCircuitBreaker
from .history import analyzed_periods_for_code, build_flat_history, get_analysis_detail
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
downloader = ReportDownloader()
task_manager: Optional[TaskManager] = None
_task_manager_lifecycle_lock = threading.Lock()
chat_sessions: Dict[str, List[Dict[str, str]]] = {}
# 单报告会话内存表并发读改写锁
_chat_lock = threading.Lock()
# 智能问答历史会话（JSON 文件持久化，data/chat_sessions.json）
chat_store = ChatStore()


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


def _mcp_tool_defs() -> Optional[List[Dict[str, Any]]]:
    """返回注入模型的工具定义；MCP 不可用时返回 None（不注入，避免模型调用必失败的工具）"""
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
        listed = stock_mcp.list_tools(timeout=timeout)
    except Exception:
        logger.warning("MCP 工具清单获取失败，问答将不启用 MCP 工具")
        _mcp_tool_defs_cache = None
        return None
    if not listed:
        _mcp_tool_defs_cache = None
        return None
    _mcp_tool_input_schemas = {
        str(tool.get("name")): tool.get("input_schema")
        for tool in listed
        if tool.get("name") and isinstance(tool.get("input_schema"), dict)
    }
    _mcp_tool_defs_cache = build_tool_defs(
        lambda: listed,
        whitelist=whitelist,
        max_tools=12,
    ) or None
    return _mcp_tool_defs_cache


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
            else:
                mcp_breaker.record_success()
            return result
        # 只有工具 schema 声明了 output_format 才补默认值；get_time_info 等
        # 无参数工具不能接收通用股票参数。
        if schema is None or "output_format" in (schema.get("properties") or {}):
            args.setdefault("output_format", "json")
        try:
            result = stock_mcp.call_tool(name, args, timeout=timeout)
        except Exception as exc:
            mcp_breaker.record_failure(exc)
            return f"工具调用失败：{exc}"
        if _tool_result_failed(result):
            mcp_breaker.record_failure(result)
            return f"工具调用失败：{result}"
        mcp_breaker.record_success()
        return result

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
            tool_executor=_build_mcp_tool_executor(cfg),
            max_tool_rounds=getattr(cfg, "mcp_max_tool_rounds", 3),
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
    except Exception:
        logger.exception("RAG 初始化失败，问答将回退传统模式")
        rag_store = rag_service = rag_qa = None


_init_rag()  # 模块加载时尝试初始化（失败不阻塞启动）

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
    dimensions: List[str] = Field(default_factory=list)


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


class RagIngestOneRequest(BaseModel):
    report_id: str
    source: str


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
    """查询单份财报元信息（含公司名与下载地址）；查无 → 404"""
    rt = _report_type_for_period(period)
    reports = datasource.fetch_reports(
        stock_code=code,
        report_types=[rt],
        start_date=period,
        end_date=period,
    )
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
    """启动 MCP 服务检测：尝试连接服务器并列出工具清单"""
    try:
        tools = stock_mcp.list_tools(timeout=25)
        return {"ok": True, "message": f"MCP 服务正常，共 {len(tools)} 个工具"}
    except Exception as exc:
        return {"ok": False, "message": f"MCP 服务异常：{exc}"}


@app.get("/api/mcp/status")
def mcp_status() -> Dict[str, Any]:
    """MCP 状态检查：熔断器状态 + 工具注入情况 + 最近诊断"""
    st = mcp_breaker.status()
    st["tools_injected"] = bool(
        mcp_breaker.allow() and _mcp_tool_defs_ready and _mcp_tool_defs_cache
    )
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


@app.post("/api/reports/{code}/{period}/analyze")
def analyze_report(code: str, period: str, body: AnalyzeRequest) -> Dict[str, Any]:
    _require_ai()
    p = _parse_period(period)
    meta = _find_report_meta(code, p)

    # 仅保留已知且带预设提示词的维度；过滤后为空则回退配置/内置默认维度
    dims = [d for d in body.dimensions if ANALYSIS_TEMPLATES.get(d, {}).get("prompt")]
    if not dims:
        dims = _default_analysis_dimensions()

    # 停止信号：POST /api/tasks/{id}/cancel 时置位，analyze 维度循环间感知后中断
    stop_event = threading.Event()

    def _run(report_progress: Callable[[float], None]) -> Dict[str, Any]:
        report_progress(0.03)
        path = _ensure_pdf(meta)
        report_progress(0.08)
        _auto_ingest_report(path)  # 分析前确保 RAG 已就绪（幂等，失败不影响分析）
        report_progress(0.18)
        report = analyzer.analyze(
            path,
            dimensions=dims,
            meta={
                "ticker": meta.company_id,
                "year": p.year,
                "period": p.isoformat(),
                "company": meta.company_name,
            },
            stop_event=stop_event,
            progress_callback=lambda event: report_progress(
                _analysis_progress_value(event)
            ),
        )
        report_progress(0.94)
        md_path = report.save(ANALYSIS_DIR)  # 落盘 reports/analysis/（与 CLI 互通）
        report_progress(0.97)
        _auto_ingest_report(path)  # RAG 自动摄取，连带分析报告双源（失败不影响分析结果）
        data = report.to_json()
        data["markdown_path"] = md_path
        report_progress(1.0)
        return data

    task_id = task_manager.submit(_run, stop_event=stop_event, progressive=True)
    if task_id is None:
        raise HTTPException(409, "已有分析任务进行中，请稍候")
    return {"task_id": task_id, "dimensions": dims}


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
        except Exception:
            logger.exception("RAG 问答失败，回退传统模式")
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
    resp: Dict[str, Any] = {"answer": answer}
    if citations:
        resp["citations"] = citations
    return resp


# ── RAG：通用对话 / 状态 / 摄取 ─────────────────────────────

@app.post("/api/chat")
def global_chat(body: GlobalChatRequest) -> Dict[str, Any]:
    _require_ai()
    if not body.question.strip():
        raise HTTPException(400, "问题不能为空")
    if rag_qa is None:
        raise HTTPException(503, "RAG 知识库未初始化：请配置 rag.enabled 并执行索引")
    result = rag_qa.answer(body.question, filters=body.filters)
    if result is None:
        return {"answer": "知识库中未检索到相关内容，请补充更多报告或更换问法。", "citations": []}
    return result


# ── 智能问答：流式 + 历史会话 ─────────────────────────────

def _sse(event: str, data: Dict[str, Any]) -> str:
    """SSE 帧：event: xxx\ndata: {...}\n\n"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(body: StreamChatRequest, request: Request) -> StreamingResponse:
    """流式全局问答（SSE）。事件：session / delta / done / error。

    - session: 会话 id（新建或沿用 body.session_id）
    - delta:   模型回答内容增量 {text}；首个 delta 前前端显示「思考中」
    - done:    完成 {answer, citations, session_id}
    - error:   失败 {error}
    回答后自动把 {user, assistant} 写入会话历史（多轮上下文用最近 4 轮）。
    用户主动停止（前端断开连接）时，已生成的部分内容也会保存进历史。
    """
    _require_ai()
    if not body.question.strip():
        raise HTTPException(400, "问题不能为空")
    if rag_qa is None:
        raise HTTPException(503, "RAG 知识库未初始化：请配置 rag.enabled 并执行索引")

    session = chat_store.get_or_create(body.session_id)
    sid = session["id"]
    history = session.get("messages", [])[-8:]  # 传给模型的最近 4 轮
    # MCP 工具：默认开启；use_mcp=false 或工具定义不可用时不注入（纯 RAG）
    tools = _mcp_tool_defs() if body.use_mcp else None
    # 聚焦报告：解析为 report_id 后提升其检索权重（历史记录跳转场景）
    priority_report_id = None
    fr = body.focus_report or {}
    if fr.get("code") and fr.get("period"):
        try:
            priority_report_id = RagQA.build_report_id(fr["code"], fr["period"])
        except Exception:
            priority_report_id = None

    async def gen():
        # 同步生成器（rag_qa.answer_stream 内部为阻塞式 requests 流）放在独立
        # 生产者线程执行，经 asyncio.Queue 转发到事件循环 —— 这样多个会话的
        # 流式请求真正并行，一个会话的模型调用不会阻塞其他会话的响应。
        q: "asyncio.Queue" = asyncio.Queue(maxsize=64)
        SENTINEL = object()
        stop_producer = threading.Event()
        loop = asyncio.get_running_loop()

        def _safe_put(item: Any) -> None:
            """从生产者线程安全地把事件调度到事件循环（asyncio.Queue 非线程安全）"""
            try:
                asyncio.run_coroutine_threadsafe(q.put(item), loop)
            except RuntimeError:
                # 事件循环已关闭（客户端断开后清理）
                pass

        def _produce() -> None:
            try:
                for evt in rag_qa.answer_stream(body.question, history=history,
                                                filters=body.filters, tools=tools,
                                                priority_report_id=priority_report_id):
                    if stop_producer.is_set():
                        return
                    _safe_put(evt)
            except Exception as exc:
                logger.exception("流式问答生产失败")
                if not stop_producer.is_set():
                    _safe_put({"type": "error", "error": f"流式问答失败：{exc}"})
            finally:
                if not stop_producer.is_set():
                    _safe_put(SENTINEL)

        producer = threading.Thread(target=_produce, daemon=True, name="chat-stream-producer")
        producer.start()

        answer_parts = []
        saved = False
        try:
            yield _sse("session", {"session_id": sid})
            while True:
                evt = await q.get()
                if evt is SENTINEL:
                    break
                # 客户端已断开（点击「停止」）：终止生成，保留已产出部分
                if await request.is_disconnected():
                    break
                if evt["type"] == "delta":
                    text = evt.get("text", "")
                    if text:
                        answer_parts.append(text)
                        yield _sse("delta", {"text": text})
                elif evt["type"] == "tool_call":
                    # 前端展示「正在调用 MCP 工具 xxx」
                    yield _sse("tool_call", {
                        "name": evt.get("name", ""),
                        "arguments": evt.get("arguments", {}),
                    })
                elif evt["type"] == "tool_result":
                    yield _sse("tool_result", {
                        "name": evt.get("name", ""),
                        "summary": evt.get("summary", ""),
                        "ok": evt.get("ok", True),
                    })
                elif evt["type"] == "empty":
                    default = "知识库中未检索到相关内容，请补充更多报告或更换问法。"
                    chat_store.append_messages(sid, [
                        {"role": "user", "content": body.question},
                        {"role": "assistant", "content": default},
                    ])
                    saved = True
                    yield _sse("done", {"answer": default, "citations": [], "session_id": sid})
                    return
                elif evt["type"] == "error":
                    yield _sse("error", {"error": evt.get("error", "未知错误")})
                elif evt["type"] == "done":
                    answer = evt.get("answer") or ""
                    chat_store.append_messages(sid, [
                        {"role": "user", "content": body.question},
                        {"role": "assistant", "content": answer},
                    ])
                    saved = True
                    yield _sse("done", {
                        "answer": answer,
                        "citations": evt.get("citations", []),
                        "session_id": sid,
                        "tools_used": evt.get("tools_used", []),
                        "retrieval_degraded": evt.get("retrieval_degraded", False),
                    })
                    return
        except Exception as exc:
            logger.exception("流式问答失败")
            yield _sse("error", {"error": f"流式问答失败：{exc}"})
        finally:
            stop_producer.set()
            # 未正常完成（停止/断开/异常）：把已生成的部分内容保存进历史
            if not saved:
                partial = "".join(answer_parts).strip()
                if partial:
                    chat_store.append_messages(sid, [
                        {"role": "user", "content": body.question},
                        {"role": "assistant", "content": partial},
                    ])

    return StreamingResponse(gen(), media_type="text/event-stream")


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
    """删除会话"""
    if not chat_store.delete_session(sid):
        raise HTTPException(404, f"未知会话：{sid}")
    return {"ok": True, "session_id": sid}


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
