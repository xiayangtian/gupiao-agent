"""ReportAnalyzer 单元测试（Web 端新增能力）"""

import json
from unittest.mock import MagicMock

import pytest

from financial_report_fetcher.analyzer import ReportAnalyzer


def _mock_client():
    client = MagicMock()
    client.default_model = "test-model"
    client.chat.return_value = {
        "content": "分析结果",
        "model": "test-model",
        "usage": {"total_tokens": 10},
    }
    return client


def _stub_pdf_text(analyzer, monkeypatch, text="长江电力股份有限公司2025年年度报告\n公司简称：长江电力"):
    """
    单测中把 PDF 文本抽取替换为固定桩文本（避免依赖真实 PDF 文件）。
    Web 端文件路径由调用方传入、文件名任意，桩文本同样覆盖该场景。
    """
    monkeypatch.setattr(analyzer, "_extract_pdf_text", lambda *args, **kwargs: text)


class TestAnalyzeWithMeta:
    def test_meta_overrides_filename_parsing(self, monkeypatch):
        """meta 传入时跳过文件名正则解析（文件名可以是任意字符串）"""
        client = _mock_client()
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        report = analyzer.analyze(
            "whatever.pdf",
            dimensions=["financial_summary"],
            meta={"ticker": "600900", "year": 2025, "company": "长江电力"},
        )
        assert "长江电力" in report.company
        assert "600900" in report.company
        assert report.report_year == 2025
        assert report.dimensions[0].content == "分析结果"

    def test_meta_absent_keeps_filename_parsing(self, monkeypatch):
        """不传 meta 时保持原文件名解析行为（回归）"""
        client = _mock_client()
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        report = analyzer.analyze(
            "600519_年报_2024.pdf",
            dimensions=["financial_summary"],
        )
        assert report.report_year == 2024
        assert "600519" in report.company


class TestExtractMetrics:
    def test_metrics_extracted_and_period_persisted(self, monkeypatch):
        """维度分析后追加一次结构化调用：按年升序、period 落入 meta"""
        client = _mock_client()
        client.chat.side_effect = [
            {"content": "分析结果", "model": "test-model", "usage": {"total_tokens": 10}},
            {
                "content": json.dumps(
                    {
                        "metrics": [
                            {"year": "2024", "revenue": 700.5, "net_profit": 300.2, "roe": 15.8, "gross_margin": 60.0, "debt_ratio": 61.1},
                            {"year": 2025, "revenue": 853.6, "net_profit": 325.8, "roe": 17.2, "gross_margin": 61.3, "debt_ratio": 62.5},
                        ]
                    },
                    ensure_ascii=False,
                ),
                "usage": {"total_tokens": 7},
            },
        ]
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        report = analyzer.analyze(
            "600519_年报_2025-12-31.pdf",
            dimensions=["financial_summary"],
            meta={"ticker": "600519", "year": 2025, "company": "贵州茅台", "period": "2025-12-31"},
        )
        assert report.period == "2025-12-31"
        assert report.metrics == [
            {"year": 2024, "revenue": 700.5, "net_profit": 300.2, "roe": 15.8, "gross_margin": 60.0, "debt_ratio": 61.1},
            {"year": 2025, "revenue": 853.6, "net_profit": 325.8, "roe": 17.2, "gross_margin": 61.3, "debt_ratio": 62.5},
        ]
        data = report.to_json()
        assert data["meta"]["period"] == "2025-12-31"
        assert data["meta"]["total_tokens"] == 17
        assert data["metrics"][1]["year"] == 2025

    def test_parse_meta_accepts_iso_date_filenames(self, monkeypatch):
        client = _mock_client()
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)

        ticker, year = analyzer._parse_meta("长江电力_600900_年报_2025-12-31.pdf")

        assert ticker == "600900"
        assert year == 2025

    def test_parse_meta_keeps_non_numeric_ticker_filenames(self):
        client = _mock_client()
        analyzer = ReportAnalyzer(client)

        ticker, year = analyzer._parse_meta("BABA_年报_2024.pdf")

        assert ticker == "BABA"
        assert year == 2024

    def test_bad_rows_dropped_and_strings_parsed(self, monkeypatch):
        """年份越界整行丢弃；非数字 → None；字符串数字 → float；同年后写覆盖前写"""
        client = _mock_client()
        client.chat.side_effect = [
            {"content": "分析结果", "model": "test-model", "usage": {"total_tokens": 10}},
            {
                "content": json.dumps(
                    {
                        "metrics": [
                            {"year": 1950, "revenue": 1.0},
                            {"year": "2025", "revenue": "853.6", "net_profit": "", "roe": "NaN", "gross_margin": "inf", "debt_ratio": 61.1},
                            {"year": 2025, "revenue": 900, "net_profit": 325.8, "roe": 17.2, "gross_margin": 61.3, "debt_ratio": 62.5},
                            "bad-row",
                        ]
                    },
                    ensure_ascii=False,
                )
            },
        ]
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        report = analyzer.analyze(
            "600519_年报_2025.pdf",
            dimensions=["financial_summary"],
        )
        assert report.metrics == [
            {"year": 2025, "revenue": 900.0, "net_profit": 325.8, "roe": 17.2, "gross_margin": 61.3, "debt_ratio": 62.5},
        ]

    def test_metrics_extraction_failure_returns_none(self, monkeypatch):
        """结构化调用失败时不影响主流程"""
        client = _mock_client()
        client.chat.side_effect = [
            {"content": "分析结果", "model": "test-model", "usage": {"total_tokens": 10}},
            RuntimeError("network glitch"),
        ]
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        report = analyzer.analyze(
            "600519_年报_2025.pdf",
            dimensions=["financial_summary"],
        )
        assert report.metrics is None
        assert report.dimensions[0].content == "分析结果"


class TestQA:
    def test_qa_returns_chat_content_with_history(self, monkeypatch):
        """qa() 构造带 system 上下文与历史的 messages，返回模型回复"""
        client = _mock_client()
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        history = [{"role": "user", "content": "问1"}, {"role": "assistant", "content": "答1"}]
        answer = analyzer.qa("whatever.pdf", question="现金流如何？", history=history)
        assert answer == "分析结果"
        # 断言 messages 结构：system + 历史 + 当前问题（共 4 条）
        sent = client.chat.call_args.kwargs["messages"]
        assert sent[0]["role"] == "system"
        assert sent[1:3] == history
        assert sent[3] == {"role": "user", "content": "现金流如何？"}

    def test_qa_raises_on_empty_pdf(self, monkeypatch):
        """PDF 无可抽取文字时报错"""
        client = _mock_client()
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch, text="   \n  ")
        with pytest.raises(ValueError):
            analyzer.qa("whatever.pdf", question="x")

class _FakeRagAnalysis:
    """注入用 fake：记录 build_context 调用；result 为返回文本或异常"""

    def __init__(self, result=""):
        self.result = result
        self.calls = []

    def build_context(self, report_id, dimension, queries, sections=None, top_k=None):
        self.calls.append({
            "report_id": report_id,
            "dimension": dimension,
            "queries": queries,
            "sections": sections,
            "top_k": top_k,
        })
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class TestRagEnhancedAnalysis:
    """RAG 增强：上下文优先 + 空/异常回退截断全文（向后兼容）"""

    def _user_content(self, client):
        return client.chat.call_args_list[0].kwargs["messages"][0]["content"]

    def test_no_rag_analysis_keeps_pdf_text(self, monkeypatch):
        """未注入 rag_analysis 时行为与现状完全一致（用截断全文）"""
        client = _mock_client()
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        analyzer.analyze("whatever.pdf", dimensions=["financial_summary"],
                         meta={"ticker": "600900", "year": 2025, "company": "长江电力"})
        content = self._user_content(client)
        assert "长江电力股份有限公司2025年年度报告" in content

    def test_rag_context_replaces_pdf_text_when_non_empty(self, monkeypatch):
        """检索上下文非空时替代截断全文作为财报内容"""
        client = _mock_client()
        rag = _FakeRagAnalysis("RAG检索片段内容")
        analyzer = ReportAnalyzer(client, rag_analysis=rag)
        _stub_pdf_text(analyzer, monkeypatch)
        analyzer.analyze("whatever.pdf", dimensions=["financial_summary"],
                         meta={"ticker": "600900", "year": 2025, "company": "长江电力"})
        content = self._user_content(client)
        assert "RAG检索片段内容" in content
        assert "长江电力股份有限公司2025年年度报告" not in content

    def test_empty_rag_context_falls_back_to_pdf_text(self, monkeypatch):
        """检索为空（空串）回退截断全文"""
        client = _mock_client()
        analyzer = ReportAnalyzer(client, rag_analysis=_FakeRagAnalysis(""))
        _stub_pdf_text(analyzer, monkeypatch)
        analyzer.analyze("whatever.pdf", dimensions=["financial_summary"],
                         meta={"ticker": "600900", "year": 2025, "company": "长江电力"})
        content = self._user_content(client)
        assert "长江电力股份有限公司2025年年度报告" in content

    def test_rag_context_exception_falls_back_to_pdf_text(self, monkeypatch):
        """检索异常回退截断全文，不影响维度分析"""
        client = _mock_client()
        analyzer = ReportAnalyzer(client, rag_analysis=_FakeRagAnalysis(RuntimeError("boom")))
        _stub_pdf_text(analyzer, monkeypatch)
        report = analyzer.analyze("whatever.pdf", dimensions=["financial_summary"],
                                  meta={"ticker": "600900", "year": 2025, "company": "长江电力"})
        content = self._user_content(client)
        assert "长江电力股份有限公司2025年年度报告" in content
        assert report.dimensions[0].content == "分析结果"

    def test_dimension_without_retrieval_uses_pdf_text(self, monkeypatch):
        """模板缺失 retrieval 配置的维度回退全文检索（不调 build_context）"""
        import financial_report_fetcher.analyzer as analyzer_mod

        monkeypatch.setitem(
            analyzer_mod.ANALYSIS_TEMPLATES,
            "temp_dim",
            {"name": "临时维度", "description": "", "prompt": "请分析", "schema": None},
        )
        client = _mock_client()
        rag = _FakeRagAnalysis("不应使用")
        analyzer = ReportAnalyzer(client, rag_analysis=rag)
        _stub_pdf_text(analyzer, monkeypatch)
        analyzer.analyze("whatever.pdf", dimensions=["temp_dim"],
                         meta={"ticker": "600900", "year": 2025, "company": "长江电力"})
        content = self._user_content(client)
        assert "长江电力股份有限公司2025年年度报告" in content
        assert rag.calls == []

    def test_report_id_and_retrieval_passed_to_build_context(self, monkeypatch):
        """meta 无 period 时按 ticker+year 推导年报 report_id；retrieval 参数透传"""
        client = _mock_client()
        rag = _FakeRagAnalysis("片段")
        analyzer = ReportAnalyzer(client, rag_analysis=rag)
        _stub_pdf_text(analyzer, monkeypatch)
        analyzer.analyze("whatever.pdf", dimensions=["financial_summary"],
                         meta={"ticker": "600900", "year": 2025, "company": "长江电力"})
        assert len(rag.calls) == 1
        call = rag.calls[0]
        assert call["report_id"] == "600900:2025-12-31:annual"
        assert call["dimension"] == "financial_summary"
        assert call["queries"]
        assert call["sections"]

    def test_report_id_from_filename_when_meta_absent(self, monkeypatch):
        """无 meta 时从 PDF 文件名解析 report_id"""
        client = _mock_client()
        rag = _FakeRagAnalysis("片段")
        analyzer = ReportAnalyzer(client, rag_analysis=rag)
        _stub_pdf_text(analyzer, monkeypatch)
        analyzer.analyze("贵州茅台_600519_年报_2024.pdf", dimensions=["financial_summary"])
        assert rag.calls[0]["report_id"] == "600519:2024-12-31:annual"


class TestDimensionTemplates:
    """维度模板扩展：默认 5 维度 + 新维度模板/检索策略完整性"""

    NEW_DIMENSIONS = [
        "profit_quality", "cashflow", "growth", "solvency", "operation",
        "governance", "shareholder_return", "rnd_innovation", "industry_competition",
    ]

    def test_default_dimensions_are_five(self):
        """DEFAULT_DIMENSIONS 为 5 个默认维度（财务摘要/风险识别/经营亮点/盈利质量/现金流）"""
        assert ReportAnalyzer.DEFAULT_DIMENSIONS == [
            "financial_summary", "risk_warning", "business_highlights",
            "profit_quality", "cashflow",
        ]

    def test_new_dimensions_have_prompt_and_retrieval(self):
        """每个新维度模板含非空 name/description/prompt 与 retrieval.queries"""
        from financial_report_fetcher.analyzer import ANALYSIS_TEMPLATES

        for dim_id in self.NEW_DIMENSIONS:
            cfg = ANALYSIS_TEMPLATES.get(dim_id)
            assert cfg is not None, f"缺失维度模板：{dim_id}"
            assert cfg["name"]
            assert cfg["description"]
            assert cfg["prompt"]
            retrieval = cfg.get("retrieval")
            assert retrieval, f"维度 {dim_id} 缺少 retrieval 配置"
            assert retrieval.get("queries"), f"维度 {dim_id} 的 retrieval.queries 为空"

    def test_existing_dimensions_keep_retrieval(self):
        """原有 3 个预设维度保留 retrieval（Task 2 已加）"""
        from financial_report_fetcher.analyzer import ANALYSIS_TEMPLATES

        for dim_id in ("financial_summary", "risk_warning", "business_highlights"):
            assert ANALYSIS_TEMPLATES[dim_id].get("retrieval", {}).get("queries")

    def test_custom_dimension_kept_untouched(self):
        """custom 维度保持 prompt=None，可被维度过滤逻辑正确排除"""
        from financial_report_fetcher.analyzer import ANALYSIS_TEMPLATES

        cfg = ANALYSIS_TEMPLATES["custom"]
        assert cfg["prompt"] is None
        assert "retrieval" not in cfg

    def test_all_preset_dimensions_are_filterable(self):
        """所有带 prompt 的维度都可通过现有过滤逻辑选中（server 依赖）"""
        from financial_report_fetcher.analyzer import ANALYSIS_TEMPLATES

        filterable = [d for d, c in ANALYSIS_TEMPLATES.items() if c.get("prompt")]
        for dim_id in self.NEW_DIMENSIONS:
            assert dim_id in filterable


class TestAnalysisCancellation:
    """分析任务停止：维度循环间检查 stop_event"""

    def test_analyze_raises_cancelled_when_stop_event_pre_set(self, monkeypatch):
        """stop_event 已置位时立即抛出 AnalysisCancelledError，不调用 AI"""
        import threading

        from financial_report_fetcher.exceptions import AnalysisCancelledError

        client = _mock_client()
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        stop_event = threading.Event()
        stop_event.set()
        with pytest.raises(AnalysisCancelledError):
            analyzer.analyze(
                "whatever.pdf",
                dimensions=["financial_summary"],
                meta={"ticker": "600900", "year": 2025, "company": "长江电力"},
                stop_event=stop_event,
            )
        assert client.chat.call_count == 0

    def test_analyze_stops_between_dimensions(self, monkeypatch):
        """分析中置位 stop_event：当前维度完成后停止，不再发起后续维度"""
        import threading

        from financial_report_fetcher.exceptions import AnalysisCancelledError

        client = _mock_client()
        stop_event = threading.Event()
        calls = {"n": 0}

        def _chat(*args, **kwargs):
            calls["n"] += 1
            stop_event.set()  # 第一次维度调用后立即请求停止
            return {"content": "结果", "model": "m", "usage": {"total_tokens": 1}}

        client.chat.side_effect = _chat
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        with pytest.raises(AnalysisCancelledError):
            analyzer.analyze(
                "whatever.pdf",
                dimensions=["financial_summary", "profit_quality"],
                meta={"ticker": "600900", "year": 2025, "company": "长江电力"},
                stop_event=stop_event,
            )
        # 只执行了第一个维度；指标抽取也被跳过
        assert calls["n"] == 1


class TestEmptyDimensionContent:
    """模型空返回防护：空内容重试一次，仍空则标记错误（前端兜底展示"暂无对应数据"）"""

    def test_empty_content_retries_once(self, monkeypatch):
        """模型首轮返回空内容时自动重试一次，重试成功以重试结果为准"""
        client = _mock_client()
        client.chat.side_effect = [
            {"content": "", "model": "test-model", "usage": {"total_tokens": 5}},
            {"content": "重试后的财务摘要", "model": "test-model", "usage": {"total_tokens": 8}},
            # 第 3 次调用是 _extract_metrics 的结构化指标抽取
            {"content": json.dumps({"metrics": [{"year": 2024, "revenue": 700.0}]}), "usage": {"total_tokens": 2}},
        ]
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        report = analyzer.analyze(
            "whatever.pdf",
            dimensions=["financial_summary"],
            meta={"ticker": "601288", "year": 2024, "company": "农业银行"},
        )
        assert report.dimensions[0].content == "重试后的财务摘要"
        assert report.dimensions[0].error is None
        # 调用次数 = 维度首轮 + 维度重试 + _extract_metrics 共 3 次
        assert client.chat.call_count == 3

    def test_empty_content_marks_error_after_retry(self, monkeypatch):
        """重试后仍为空时标记错误，提示用户而非静默空白"""
        client = _mock_client()
        client.chat.side_effect = [
            {"content": "", "model": "test-model", "usage": {"total_tokens": 5}},
            {"content": "", "model": "test-model", "usage": {"total_tokens": 5}},
            # 第 3 次调用是 _extract_metrics 的结构化指标抽取
            {"content": json.dumps({"metrics": [{"year": 2024, "revenue": 700.0}]}), "usage": {"total_tokens": 2}},
        ]
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        report = analyzer.analyze(
            "whatever.pdf",
            dimensions=["financial_summary"],
            meta={"ticker": "601288", "year": 2024, "company": "农业银行"},
        )
        dim = report.dimensions[0]
        assert dim.content == ""
        assert dim.error is not None
        assert "未返回" in dim.error

    def test_to_markdown_empty_content_shows_placeholder(self, monkeypatch):
        """Markdown 导出时，空内容（无错误）输出"暂无对应数据"占位"""
        client = _mock_client()
        client.chat.side_effect = [
            {"content": "", "model": "test-model", "usage": {"total_tokens": 5}},
            {"content": "", "model": "test-model", "usage": {"total_tokens": 5}},
            # 第 3 次调用是 _extract_metrics 的结构化指标抽取
            {"content": json.dumps({"metrics": [{"year": 2024, "revenue": 700.0}]}), "usage": {"total_tokens": 2}},
        ]
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        report = analyzer.analyze(
            "whatever.pdf",
            dimensions=["financial_summary"],
            meta={"ticker": "601288", "year": 2024, "company": "农业银行"},
        )
        md = report.to_markdown()
        assert "暂无对应数据" in md
        # 空内容且已标记错误时，导出仍以错误提示为主，不出现空白段
        assert "模型未返回有效内容" in md

    def test_empty_content_reasoning_explains_cause(self, monkeypatch):
        """空返回且带思考过程时，错误信息说明是"仅输出思考过程"而非笼统无数据"""
        client = _mock_client()
        client.chat.side_effect = [
            {"content": "", "reasoning": "思考……", "model": "test-model", "usage": {"total_tokens": 5}},
            {"content": "", "reasoning": "思考……", "model": "test-model", "usage": {"total_tokens": 5}},
            # 第 3 次调用是 _extract_metrics 的结构化指标抽取
            {"content": json.dumps({"metrics": [{"year": 2024, "revenue": 700.0}]}), "usage": {"total_tokens": 2}},
        ]
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        report = analyzer.analyze(
            "whatever.pdf",
            dimensions=["financial_summary"],
            meta={"ticker": "601288", "year": 2024, "company": "农业银行"},
        )
        dim = report.dimensions[0]
        assert dim.error is not None
        assert "思考过程" in dim.error
        assert "请重新分析" in dim.error

    def test_empty_content_finish_length_explains_cause(self, monkeypatch):
        """空返回且 finish_reason=length 时，说明是输出超长被截断"""
        client = _mock_client()
        client.chat.side_effect = [
            {"content": "", "finish_reason": "length", "model": "test-model", "usage": {"total_tokens": 5}},
            {"content": "", "finish_reason": "length", "model": "test-model", "usage": {"total_tokens": 5}},
            # 第 3 次调用是 _extract_metrics 的结构化指标抽取
            {"content": json.dumps({"metrics": [{"year": 2024, "revenue": 700.0}]}), "usage": {"total_tokens": 2}},
        ]
        analyzer = ReportAnalyzer(client)
        _stub_pdf_text(analyzer, monkeypatch)
        report = analyzer.analyze(
            "whatever.pdf",
            dimensions=["financial_summary"],
            meta={"ticker": "601288", "year": 2024, "company": "农业银行"},
        )
        dim = report.dimensions[0]
        assert dim.error is not None
        assert "截断" in dim.error
        assert "请重新分析" in dim.error
