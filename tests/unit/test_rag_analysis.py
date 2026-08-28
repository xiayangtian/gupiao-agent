"""RagAnalysis 单元测试：按维度检索 + 上下文组装。

全部使用 fake store，不依赖真实 ChromaDB / embedding / 网络。
"""

import pytest

from financial_report_fetcher.rag.analysis import RagAnalysis


class FakeStore:
    """按查询词返回预设 hit 列表的 fake store；可注入单路异常"""

    def __init__(self, hits_by_query=None, fail_queries=None):
        self.hits_by_query = hits_by_query or {}
        self.fail_queries = set(fail_queries or [])
        self.calls = []

    def query(self, text, top_k=8, where=None):
        self.calls.append({"text": text, "top_k": top_k, "where": where})
        if text in self.fail_queries:
            raise RuntimeError(f"检索失败：{text}")
        return [dict(h) for h in self.hits_by_query.get(text, [])]


def _hit(doc_id, text, section="财务报告", page=3, distance=0.5):
    return {
        "id": doc_id,
        "text": text,
        "section": section,
        "page": page,
        "distance": distance,
        "report_id": "600900:2025-12-31:annual",
        "source": "pdf",
    }


def test_merges_and_dedups_across_queries():
    """多路检索结果按 chunk id 合并去重，编号连续"""
    store = FakeStore({
        "营业收入 净利润": [_hit("a", "营业收入862亿元"), _hit("b", "净利润345亿元")],
        "毛利率 ROE": [_hit("b", "净利润345亿元"), _hit("c", "毛利率28%")],
    })
    ctx = RagAnalysis(store).build_context(
        report_id="600900:2025-12-31:annual",
        dimension="financial_summary",
        queries=["营业收入 净利润", "毛利率 ROE"],
    )
    # 3 个片段、编号 [1][2][3]（b 去重只出现一次）
    assert ctx.count("[") == 3
    assert "营业收入862亿元" in ctx
    assert "净利润345亿元" in ctx
    assert "毛利率28%" in ctx
    # 每路检索都限定 report_id 过滤
    assert all(c["where"] == {"report_id": "600900:2025-12-31:annual"} for c in store.calls)


def test_section_filter_keeps_only_matching():
    """sections 按 metadata.section 模糊过滤"""
    store = FakeStore({
        "风险 负债": [
            _hit("a", "负债率高", section="财务报告"),
            _hit("b", "治理风险", section="公司治理"),
            _hit("c", "现金流失", section="管理层讨论与分析"),
        ],
    })
    ctx = RagAnalysis(store).build_context(
        report_id="r", dimension="risk_warning",
        queries=["风险 负债"], sections=["财务报告", "管理层讨论"],
    )
    assert "负债率高" in ctx
    assert "现金流失" in ctx
    assert "治理风险" not in ctx


def test_section_filter_no_match_returns_empty():
    """章节定向无命中时返回空字符串"""
    store = FakeStore({"行业 竞争": [_hit("a", "市占率第一", section="财务报告")]})
    ctx = RagAnalysis(store).build_context(
        report_id="r", dimension="industry_competition",
        queries=["行业 竞争"], sections=["公司治理"],
    )
    assert ctx == ""


def test_empty_retrieval_returns_empty_string():
    """检索结果为空 → 空字符串（由 analyzer 回退截断全文）"""
    ctx = RagAnalysis(FakeStore()).build_context(
        report_id="r", dimension="growth", queries=["新市场 新业务"],
    )
    assert ctx == ""


def test_single_query_exception_does_not_break_others():
    """单路检索异常只跳过该路，其余路正常组装"""
    store = FakeStore(
        {"正常查询": [_hit("a", "正常片段")]},
        fail_queries={"坏查询"},
    )
    ctx = RagAnalysis(store).build_context(
        report_id="r", dimension="d", queries=["坏查询", "正常查询"],
    )
    assert "正常片段" in ctx
    assert "[1]" in ctx


def test_context_format_contains_section_and_page():
    """片段格式：[n] 章节（第X页）: 文本"""
    store = FakeStore({"查询": [_hit("a", "关键内容", section="现金流量表", page=88)]})
    ctx = RagAnalysis(store).build_context(
        report_id="r", dimension="cashflow", queries=["查询"],
    )
    assert "[1] 现金流量表（第88页）: 关键内容" in ctx


def test_empty_queries_returns_empty():
    """queries 为空时不发起检索，直接返回空串"""
    ctx = RagAnalysis(FakeStore()).build_context(report_id="r", dimension="d", queries=[])
    assert ctx == ""


def test_section_match_is_case_insensitive():
    """section 匹配大小写不敏感（英文章节名）"""
    store = FakeStore({"q": [_hit("a", "text", section="Financial Report")]})
    ctx = RagAnalysis(store).build_context(
        report_id="r", dimension="d", queries=["q"], sections=["financial"],
    )
    assert "text" in ctx


def test_real_store_smoke(tmp_path, fake_embedder):
    """真实 RagStore + fake_embedder 冒烟：能检索并组装上下文"""
    from financial_report_fetcher.rag.chunking import Chunk
    from financial_report_fetcher.rag.store import RagStore

    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([
        Chunk(
            report_id="600900:2025-12-31:annual", source="pdf",
            text="营业收入 净利润 毛利率 ROE 每股收益 主要财务指标数据",
            section="财务报告", page=12, chunk_index=0,
        ),
        Chunk(
            report_id="600900:2025-12-31:annual", source="pdf",
            text="公司治理 董事会 高管 关联交易 内部控制情况",
            section="公司治理", page=50, chunk_index=1,
        ),
    ])
    ctx = RagAnalysis(store, top_k=4).build_context(
        report_id="600900:2025-12-31:annual",
        dimension="financial_summary",
        queries=["营业收入 净利润 毛利率 ROE 每股收益"],
        sections=["财务报告"],
    )
    assert "营业收入" in ctx
    assert "财务报告" in ctx
