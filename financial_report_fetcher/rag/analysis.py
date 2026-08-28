"""RagAnalysis — 按维度从 RAG 定向检索并组装分析上下文。

analyzer 在构造 user_content 时以本模块产出的片段上下文替代截断全文，
覆盖年报全文任意章节；库未就绪 / 检索为空 / 检索异常时由 analyzer
回退到现有截断全文路径（行为与未启用 RAG 时一致）。
"""

import logging
from typing import Any, Dict, List, Optional

from .reranker import Reranker, should_rerank
from .store import RagStore

logger = logging.getLogger(__name__)

# 单条片段文本长度上限（防止上下文过长，见设计文档 §10）
SNIPPET_MAX_CHARS = 300

# 多路合并去重后的候选上限（精排前放宽召回的兜底，防止上下文过大）
MAX_MERGED_CANDIDATES = 60


def _matches_section(section: Optional[str], patterns: List[str]) -> bool:
    """章节名是否模糊命中任一关键词（大小写不敏感）"""
    if not section:
        return False
    lower = section.lower()
    return any(p.lower() in lower for p in patterns)


class RagAnalysis:
    """按维度检索报告片段并组装为带编号的上下文文本。

    用法：analyzer 对每个维度取 ANALYSIS_TEMPLATES[dim]["retrieval"] 的
    queries / sections / top_k 调用 build_context；返回非空字符串时用其
    作为该维度分析的「财报内容」。
    """

    def __init__(
        self,
        store: RagStore,
        top_k: int = 8,
        reranker: Optional[Reranker] = None,
        rerank_candidates: int = 30,
        rerank_score_threshold: float = 0.5,
        rerank_margin_threshold: float = 0.05,
    ) -> None:
        self.store = store
        self.top_k = top_k
        self.reranker = reranker
        self.rerank_candidates = rerank_candidates
        self.rerank_score_threshold = rerank_score_threshold
        self.rerank_margin_threshold = rerank_margin_threshold

    def build_context(
        self,
        report_id: str,
        dimension: str,
        queries: List[str],
        sections: Optional[List[str]] = None,
        top_k: Optional[int] = None,
    ) -> str:
        """多路检索并组装上下文；无结果返回空字符串。

        - 每个 query 检索一次（where 限定 report_id）；sections 非空时按
          metadata.section 模糊过滤（大小写不敏感）。
        - 结果按 chunk id 合并去重（上限 MAX_MERGED_CANDIDATES），
          片段按距离升序排列并连续编号。
        - 注入 reranker 时每路放宽到 rerank_candidates，按合并结果质量
          自适应精排取前 top_k；未注入时维持现状（距离排序、不截断）。
        - 单路检索异常仅跳过该路，不影响其他路。
        """
        if not queries:
            return ""
        k = top_k or self.top_k
        # 注入 reranker 时放宽每路召回（供精排挑选），否则维持现状 top_k
        query_top_k = self.rerank_candidates if self.reranker else k
        merged: Dict[str, Dict[str, Any]] = {}
        for query in queries:
            try:
                hits = self.store.query(query, top_k=query_top_k, where={"report_id": report_id})
            except Exception as exc:
                logger.warning("维度 %s 检索失败（query=%s）：%s", dimension, query, exc)
                continue
            for hit in hits:
                if sections and not _matches_section(hit.get("section"), sections):
                    continue
                # 按 chunk id 去重，先到先得（靠前 query 命中优先保留）
                merged.setdefault(hit["id"], hit)
                if len(merged) >= MAX_MERGED_CANDIDATES:
                    logger.debug("维度 %s 合并候选已达上限 %d，停止继续合并",
                                 dimension, MAX_MERGED_CANDIDATES)
                    break
            else:
                continue
            break

        if not merged:
            return ""

        ordered = sorted(merged.values(), key=lambda h: float(h.get("distance", 0.0)))
        if self.reranker:
            distances = [float(h.get("distance", 0.0)) for h in ordered]
            if should_rerank(distances, self.rerank_score_threshold, self.rerank_margin_threshold):
                try:
                    ordered = self.reranker.rerank(queries[0], ordered)[:k]
                except Exception as exc:
                    logger.warning("维度 %s rerank 失败，回退按距离排序取前 %d 条：%s",
                                   dimension, k, exc)
                    ordered = ordered[:k]
            else:
                ordered = ordered[:k]
        lines = []
        for i, hit in enumerate(ordered, start=1):
            text = (hit.get("text") or "").strip()
            if not text:
                continue
            section = hit.get("section") or "未知章节"
            page = hit.get("page")
            loc = f"{section}（第{page}页）" if page else section
            lines.append(f"[{i}] {loc}: {text[:SNIPPET_MAX_CHARS]}")
        return "\n".join(lines)
