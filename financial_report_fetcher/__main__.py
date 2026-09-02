"""
financial_report_fetcher.__main__

CLI 入口模块，支持六个子命令：

    download      拉取财报（原有功能）
    analyze       分析财报
    chat          对指定财报交互问答
    quote         腾讯免费行情（实时/指数/K线）
    mcp           china-stock-mcp 财务/基本面工具（30 个）
    rag           RAG 知识库：摄取 / 状态 / 通用问答

使用方式：
    python -m financial_report_fetcher download --config config.yaml
    python -m financial_report_fetcher analyze --pdf reports/xxx.pdf
    python -m financial_report_fetcher analyze --all          # 批量分析全部 PDF
    python -m financial_report_fetcher chat --pdf reports/xxx.pdf
    python -m financial_report_fetcher quote realtime --symbols 600519,000001
    python -m financial_report_fetcher quote kline --symbol 600519 --period day --count 10
    python -m financial_report_fetcher mcp info --symbol 600519
    python -m financial_report_fetcher mcp financials --symbol 600519
    python -m financial_report_fetcher rag ingest --all        # 全量摄取
    python -m financial_report_fetcher rag status              # 知识库状态
    python -m financial_report_fetcher rag chat                # 通用问答
"""

import argparse
import datetime
import logging
import os
import sys

from .config import ConfigLoader
from .datasource import CNINFODatasource
from .downloader import ReportDownloader
from .exceptions import FetcherBaseError
from .fetcher import ReportFetcher
from .models import ReportMeta
from .ai_client import AIClient
from .analyzer import ReportAnalyzer
from .market import stock_mcp, tencent_quote
from .rag.analysis import RagAnalysis
from .rag.config import RagConfig
from .rag.embedding import LocalEmbedder
from .rag.ingest import IngestionService
from .rag.qa import RagQA
from .rag.store import RagStore

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# 子命令: download（原始拉取功能，未变）
# ════════════════════════════════════════════════════════════════════

def cmd_download(args: argparse.Namespace) -> None:
    """执行财报拉取 + 下载流水线"""
    # 第一步：初始化巨潮资讯网数据源
    logger.info("正在连接巨潮资讯网，构建上市公司索引...")
    ds = CNINFODatasource()
    known_companies = ds.build_known_companies()
    logger.info("上市公司索引构建完成，共 %d 条记录", len(known_companies))

    # 第二步：加载并验证配置文件
    logger.info("正在加载配置文件：%s", args.config)
    config = ConfigLoader().load(args.config)
    logger.info(
        "配置加载成功：%d 家公司，存储目录：%s",
        len(config.companies),
        config.storage_dir,
    )

    # 第三步：按配置的时间范围从 CNINFO 拉取各家公司的财报
    logger.info("开始拉取财报元信息...")
    if config.start_date is None or config.end_date is None:
        today = datetime.date.today()
        prev_year = today.year - 1
        effective_start = datetime.date(prev_year, 1, 1)
        effective_end = datetime.date(prev_year, 12, 31)
        logger.info(
            "默认时间范围已应用：start=%s, end=%s",
            effective_start,
            effective_end,
        )
    else:
        effective_start = config.start_date
        effective_end = config.end_date

    # 构建 reports_db
    reports_db: dict = {}
    for company in config.companies:
        stock_code = ds.resolve_company(company.ticker, company.name)
        if stock_code is None:
            raw_id = company.ticker or company.name
            logger.warning("无法匹配公司 \"%s\"，已跳过", raw_id)
            continue

        logger.info(
            "正在查询 %s 的财报（时间范围 %s ~ %s）...",
            stock_code,
            effective_start,
            effective_end,
        )
        reports_db[stock_code] = ds.fetch_reports(
            stock_code=stock_code,
            report_types=config.report_types,
            start_date=effective_start,
            end_date=effective_end,
        )
        logger.info("  %s 查询到 %d 条财报记录", stock_code, len(reports_db[stock_code]))

    # 第四步：通过 ReportFetcher 应用过滤/排序/截取
    fetcher = ReportFetcher(known_companies=known_companies, reports_db=reports_db)
    reports = fetcher.fetch(config)

    # 第五步前：记录已有 PDF 集合（供自动摄取差集使用）
    try:
        _before_pdfs = set(f for f in os.listdir(config.storage_dir) if f.endswith(".pdf"))
    except OSError:
        _before_pdfs = set()

    # 第五步：批量下载
    logger.info("开始下载财报文件，目标目录：%s", config.storage_dir)
    summary = ReportDownloader().download_all(reports, config.storage_dir)

    # 第六步：汇总
    logger.info(
        "全部任务完成 —— 成功：%d 份，跳过：%d 份，失败：%d 份，共处理：%d 份",
        summary.success,
        summary.skipped,
        summary.failed,
        summary.total,
    )

    # 第七步：自动摄取本次新增 PDF 到 RAG（可选，需 rag.enabled + auto_ingest）
    try:
        cfg, svc, _store = _build_rag_components()
    except Exception:
        logger.exception("RAG 组件初始化失败，跳过自动摄取")
        svc = None
    if svc is not None and cfg.auto_ingest:
        try:
            _after_pdfs = set(f for f in os.listdir(config.storage_dir) if f.endswith(".pdf"))
        except OSError:
            _after_pdfs = set()
        for name in sorted(_after_pdfs - _before_pdfs):
            svc.auto_ingest_pdf(os.path.join(config.storage_dir, name))


# ════════════════════════════════════════════════════════════════════
# 子命令: analyze
# ════════════════════════════════════════════════════════════════════

def cmd_analyze(args: argparse.Namespace) -> None:
    """分析财报 PDF"""
    # 初始化 AI 客户端
    client = AIClient(default_model=args.model)

    # 构建 RAG 组件（未启用 / 初始化失败时 cfg/svc/store 均为 None，行为与现状一致）
    try:
        cfg, svc, store = _build_rag_components()
    except Exception:
        logger.exception("RAG 组件初始化失败，跳过 RAG 增强")
        cfg, svc, store = None, None, None
    # RAG 启用时注入按维度检索上下文；默认维度优先取配置 analysis_dimensions
    if store is not None:
        rag_analysis = RagAnalysis(
            store, top_k=cfg.top_k, reranker=_build_reranker(cfg),
            rerank_candidates=getattr(cfg, "rerank_candidates", 30),
            rerank_score_threshold=getattr(cfg, "rerank_score_threshold", 0.5),
            rerank_margin_threshold=getattr(cfg, "rerank_margin_threshold", 0.05),
        )
    else:
        rag_analysis = None
    analyzer = ReportAnalyzer(client, rag_analysis=rag_analysis)
    default_dims = list(cfg.analysis_dimensions or ReportAnalyzer.DEFAULT_DIMENSIONS) if cfg is not None else None

    if args.all:
        # 全量分析
        report_dir = args.dir or "reports"
        output_dir = args.output or os.path.join(report_dir, "analysis")

        # 分析前确保 RAG 就绪（幂等；失败不影响分析）
        if svc is not None and cfg.auto_ingest:
            try:
                pdf_files = sorted(
                    os.path.join(report_dir, f)
                    for f in os.listdir(report_dir)
                    if f.endswith(".pdf")
                )
            except OSError:
                pdf_files = []
            for p in pdf_files:
                try:
                    svc.auto_ingest_report(p)
                except Exception:
                    logger.exception("分析前自动摄取失败：%s", p)

        reports = analyzer.analyze_all_in_directory(report_dir, output_dir=output_dir, dimensions=default_dims)
        print(f"\n✅ 批量分析完成：共处理 {len(reports)} 份财报，结果输出到 {output_dir}")
        for r in reports:
            print(f"   - {r.company} ({r.report_year})")

        # 批量分析后自动摄取：逐份连带分析报告双源入库（失败不影响分析结果）
        if svc is not None and cfg.auto_ingest:
            for r in reports:
                try:
                    svc.auto_ingest_report(r.source_file)
                except Exception:
                    logger.exception("分析后自动摄取失败：%s", r.source_file)
    else:
        # 单份分析
        if not args.pdf:
            print("❌ 请指定 --pdf 或使用 --all 全量分析")
            sys.exit(1)
        if not os.path.exists(args.pdf):
            print(f"❌ 文件不存在：{args.pdf}")
            sys.exit(1)

        # 分析前确保 RAG 就绪（幂等；失败不影响分析）
        if svc is not None and cfg.auto_ingest:
            try:
                svc.auto_ingest_report(args.pdf)
            except Exception:
                logger.exception("分析前自动摄取失败：%s", args.pdf)

        report = analyzer.analyze(args.pdf, model=args.model, dimensions=default_dims)
        output_dir = args.output or os.path.join(os.path.dirname(args.pdf) or "reports", "analysis")
        md_path = report.save(output_dir)
        print(f"\n✅ 分析完成！报告已保存：{md_path}")

        # 分析后自动摄取：连带分析报告双源入库（失败不影响分析结果）
        if svc is not None and cfg.auto_ingest:
            try:
                svc.auto_ingest_report(args.pdf)
            except Exception:
                logger.exception("分析后自动摄取失败：%s", args.pdf)


# ════════════════════════════════════════════════════════════════════
# 子命令: chat
# ════════════════════════════════════════════════════════════════════

def cmd_chat(args: argparse.Namespace) -> None:
    """交互式问答"""
    if not args.pdf:
        print("❌ 请指定 --pdf 参数")
        sys.exit(1)
    if not os.path.exists(args.pdf):
        print(f"❌ 文件不存在：{args.pdf}")
        sys.exit(1)

    client = AIClient()
    analyzer = ReportAnalyzer(client)
    analyzer.chat(args.pdf)


# ════════════════════════════════════════════════════════════════════
# 子命令: rag（RAG 知识库）
# ════════════════════════════════════════════════════════════════════

def _build_reranker(cfg):
    """cfg.rerank 开启时构造 CrossEncoderReranker；失败仅警告并回退 None（零回归）"""
    if not getattr(cfg, "rerank", False):
        return None
    try:
        from .rag.reranker import CrossEncoderReranker

        return CrossEncoderReranker(
            getattr(cfg, "rerank_model", "BAAI/bge-reranker-base"),
            top_k=getattr(cfg, "top_k", 8),
        )
    except Exception:
        logger.warning("CrossEncoderReranker 构造失败，回退纯向量检索（rag.rerank=%s）",
                       getattr(cfg, "rerank", False), exc_info=True)
        return None


def _build_rag_components():
    """按配置构建 RAG 组件；未启用则返回 (None, None, None)"""
    cfg = RagConfig.load()
    if not cfg.enabled:
        return None, None, None
    embedder = LocalEmbedder(cfg.embedding_model, hf_endpoint=cfg.hf_endpoint)
    store = RagStore(cfg.store_path, embedder)
    svc = IngestionService(
        store,
        chunk_size=cfg.chunk_size,
        chunk_overlap=cfg.chunk_overlap,
        manifest_path=os.path.join(cfg.store_path, "manifest.json"),
        auto_ingest=cfg.auto_ingest,
    )
    return cfg, svc, store


def cmd_rag(args: argparse.Namespace) -> None:
    """RAG 知识库：摄取 / 状态 / 通用问答"""
    cfg, svc, store = _build_rag_components()
    if cfg is None:
        print("❌ RAG 未启用：请在 config.yaml 中配置 rag.enabled: true")
        sys.exit(1)

    if args.rag_action == "ingest":
        if args.all or args.pdf is None:
            result = svc.ingest_all(force=args.force)
            print(f"\n✅ 摄取完成：处理 {result.ingested} 份，跳过 {result.skipped} 份，"
                  f"库中共 {result.total_chunks} 个片段")
            for e in result.errors:
                print(f"   ⚠️ {e}")
        else:
            if not os.path.exists(args.pdf):
                print(f"❌ 文件不存在：{args.pdf}")
                sys.exit(1)
            from financial_report_fetcher.rag.chunking import parse_pdf_report_id
            rid = parse_pdf_report_id(args.pdf)
            if rid is None:
                print(f"❌ 无法从文件名解析报告身份：{os.path.basename(args.pdf)}")
                sys.exit(1)
            svc.ingest_one(args.pdf, rid, force=args.force)
            print(f"✅ 已摄取：{rid}")
    elif args.rag_action == "status":
        st = svc.status()
        print(f"📚 RAG 知识库状态：{st['total_chunks']} 个片段，{len(st['reports'])} 份报告")
        for rid, info in sorted(st["reports"].items()):
            # R1b：manifest 已升级为 pdf_chunks / analysis_chunks，这里合并打印总 chunks
            chunks = int(info.get("pdf_chunks", 0) or 0) + int(info.get("analysis_chunks", 0) or 0)
            print(f"   - {rid}：{chunks} chunks（{info.get('updated_at', '')}）")
    elif args.rag_action == "chat":
        qa = RagQA(
            store, AIClient(), top_k=cfg.top_k, reranker=_build_reranker(cfg),
            rerank_candidates=getattr(cfg, "rerank_candidates", 30),
            rerank_score_threshold=getattr(cfg, "rerank_score_threshold", 0.5),
            rerank_margin_threshold=getattr(cfg, "rerank_margin_threshold", 0.05),
        )
        filters = {}
        if getattr(args, "ticker", None):
            filters["ticker"] = args.ticker
        if getattr(args, "year", None):
            filters["year"] = int(args.year)
        print("💬 通用 RAG 问答（输入 q 退出）\n")
        history: list = []
        while True:
            try:
                q = input("问题 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q or q.lower() in ("q", "quit"):
                break
            result = qa.answer(q, history=history, filters=filters or None)
            if result is None:
                print("  🤖 知识库中未检索到相关内容。\n")
                continue
            print(f"  🤖 {result['answer']}\n")
            history += [{"role": "user", "content": q},
                        {"role": "assistant", "content": result["answer"]}]
            history = history[-8:]


# ════════════════════════════════════════════════════════════════════
# 子命令: quote（腾讯免费行情）
# ════════════════════════════════════════════════════════════════════

def cmd_quote(args: argparse.Namespace) -> None:
    """腾讯免费行情查询：realtime / index / kline"""
    import json

    try:
        if args.quote_action == "realtime":
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
            if not symbols:
                print("❌ --symbols 不能为空")
                sys.exit(1)
            quotes = tencent_quote.realtime(symbols)
            print(json.dumps(quotes, ensure_ascii=False, indent=2))
            print(f"\n共 {len(quotes)} 条行情")

        elif args.quote_action == "index":
            codes = [c.strip() for c in args.codes.split(",") if c.strip()]
            if not codes:
                print("❌ --codes 不能为空")
                sys.exit(1)
            quotes = tencent_quote.index(codes)
            print(json.dumps(quotes, ensure_ascii=False, indent=2))
            print(f"\n共 {len(quotes)} 条指数行情")

        elif args.quote_action == "kline":
            bars = tencent_quote.kline(
                args.symbol, period=args.period, count=args.count, adjust=args.adjust
            )
            print(json.dumps(bars, ensure_ascii=False, indent=2))
            print(f"\n共 {len(bars)} 根K线（{args.symbol} {args.period} {args.adjust}）")
    except Exception as exc:  # noqa: BLE001
        logger.error("行情查询失败：%s", exc)
        sys.exit(1)


# ════════════════════════════════════════════════════════════════════
# 子命令: mcp（china-stock-mcp 财务/基本面工具）
# ════════════════════════════════════════════════════════════════════

_MCP_SYMBOL_TOOLS = {
    "info": ("get_stock_basic_info", "公司基本信息"),
    "financials": ("get_financial_metrics", "关键财务指标"),
    "balance-sheet": ("get_balance_sheet", "资产负债表"),
    "income": ("get_income_statement", "利润表"),
    "cashflow": ("get_cash_flow", "现金流量表"),
    "fund-flow": ("get_fund_flow", "资金流向(近100交易日)"),
    "shareholders": ("get_shareholder_info", "股东情况"),
    "forecast": ("get_profit_forecast", "业绩预测"),
    "news": ("get_news_data", "个股新闻"),
}


def cmd_mcp(args: argparse.Namespace) -> None:
    """调用 china-stock-mcp 的 30 个工具（财务/基本面/行情等）"""
    import json

    try:
        if args.mcp_action == "tools":
            tools = stock_mcp.list_tools()
            print(f"china-stock-mcp 共 {len(tools)} 个工具：")
            for t in tools:
                print(f"  - {t['name']}: {t['description']}")

        elif args.mcp_action == "call":
            call_args = json.loads(args.args or "{}") if args.args else {}
            result = stock_mcp.call_tool(args.tool, call_args, timeout=args.timeout)
            print(result)

        elif args.mcp_action in _MCP_SYMBOL_TOOLS:
            tool_name, label = _MCP_SYMBOL_TOOLS[args.mcp_action]
            call_args = {"symbol": args.symbol, "output_format": args.format}
            print(f"== {label}（{args.symbol}）==")
            print(stock_mcp.call_tool(tool_name, call_args, timeout=args.timeout))

        else:
            print("❌ 未知的 mcp 子命令")
            sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        logger.error("MCP 调用失败：%s", exc)
        sys.exit(1)


# ════════════════════════════════════════════════════════════════════
# 主入口：参数解析 + 子命令调度
# ════════════════════════════════════════════════════════════════════

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(module)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        prog="financial_report_fetcher",
        description="财报工具 — 下载、分析和问答"
    )
    parser.add_argument(
        "--model", default=None,
        help="AI 模型名（默认使用 AIClient 环境变量的 AI_MODEL 或 DeepSeek-V4-Flash）"
    )

    subparsers = parser.add_subparsers(dest="command", title="子命令")

    # download 子命令
    dl = subparsers.add_parser("download", help="下载财报 PDF")
    dl.add_argument("--config", required=True, help="配置文件路径")

    # analyze 子命令
    an = subparsers.add_parser("analyze", help="分析财报 PDF")
    an.add_argument("--pdf", help="单份财报 PDF 路径")
    an.add_argument("--all", action="store_true", help="全量分析 reports/ 目录下所有 PDF")
    an.add_argument("--dir", default="reports", help="PDF 所在目录（配合 --all）")
    an.add_argument("--output", help="分析报告输出目录（默认 reports/analysis）")

    # chat 子命令
    ch = subparsers.add_parser("chat", help="与指定财报交互问答")
    ch.add_argument("--pdf", required=True, help="财报 PDF 路径")

    # rag 子命令（RAG 知识库）
    rg = subparsers.add_parser("rag", help="RAG 知识库：摄取 / 状态 / 通用问答")
    rsub = rg.add_subparsers(dest="rag_action", required=True, title="rag 子命令")
    r_ing = rsub.add_parser("ingest", help="摄取 PDF 与分析报告")
    r_ing.add_argument("--all", action="store_true", help="全量摄取 reports/ 与 reports/analysis/")
    r_ing.add_argument("--pdf", help="单份 PDF 路径（连带摄取对应分析报告）")
    r_ing.add_argument("--force", action="store_true", help="忽略 manifest 强制重建")
    rsub.add_parser("status", help="查看知识库状态")
    r_chat = rsub.add_parser("chat", help="通用 RAG 问答")
    r_chat.add_argument("--ticker", help="限定公司代码")
    r_chat.add_argument("--year", help="限定年份")

    # quote 子命令（腾讯免费行情）
    qt = subparsers.add_parser("quote", help="腾讯免费行情：实时 / 指数 / K线")
    qsub = qt.add_subparsers(dest="quote_action", required=True, title="quote 子命令")
    q_rt = qsub.add_parser("realtime", help="个股实时行情（批量）")
    q_rt.add_argument("--symbols", required=True, help="股票代码，逗号分隔，如 600519,000001")
    q_ix = qsub.add_parser("index", help="指数实时行情（批量）")
    q_ix.add_argument("--codes", required=True, help="指数代码（带前缀），如 sh000001,sz399001")
    q_kl = qsub.add_parser("kline", help="历史K线（日/周/月）")
    q_kl.add_argument("--symbol", required=True, help="股票代码，如 600519")
    q_kl.add_argument("--period", default="day", choices=["day", "week", "month"], help="周期")
    q_kl.add_argument("--count", type=int, default=320, help="K线根数（1~800）")
    q_kl.add_argument("--adjust", default="qfq", choices=["qfq", "hfq", "none"], help="复权方式")

    # mcp 子命令（china-stock-mcp）
    mc = subparsers.add_parser("mcp", help="china-stock-mcp 财务/基本面工具")
    msub = mc.add_subparsers(dest="mcp_action", required=True, title="mcp 子命令")
    msub.add_parser("tools", help="列出全部可用工具")
    for _name in _MCP_SYMBOL_TOOLS:
        p = msub.add_parser(_name, help=_MCP_SYMBOL_TOOLS[_name][1])
        p.add_argument("--symbol", required=True, help="股票代码，如 600519")
        p.add_argument("--format", default="json", choices=["json", "markdown", "csv"], help="输出格式")
        p.add_argument("--timeout", type=float, default=90, help="调用超时秒数")
    m_call = msub.add_parser("call", help="通用调用任意 MCP 工具")
    m_call.add_argument("--tool", required=True, help="工具名，如 get_realtime_data")
    m_call.add_argument("--args", help='JSON 参数字符串，如 {"symbol":"600519"}')
    m_call.add_argument("--timeout", type=float, default=90, help="调用超时秒数")

    args = parser.parse_args()

    # 默认未指定子命令时兼容旧式：如果给了 --config 就直接进入 download
    if args.command is None:
        if hasattr(args, "config") and args.config:
            args.command = "download"
        else:
            parser.print_help()
            sys.exit(1)

    # 根据子命令调度
    try:
        if args.command == "download":
            cmd_download(args)
        elif args.command == "analyze":
            cmd_analyze(args)
        elif args.command == "chat":
            cmd_chat(args)
        elif args.command == "quote":
            cmd_quote(args)
        elif args.command == "mcp":
            cmd_mcp(args)
        elif args.command == "rag":
            cmd_rag(args)
        else:
            parser.print_help()
            sys.exit(1)
    except FetcherBaseError as exc:
        logger.error("执行失败：%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
