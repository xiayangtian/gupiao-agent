"""RAG 查询重排序（rerank）模块。

自适应 rerank：仅当首轮向量检索质量不佳 / 不确定时才用 cross-encoder 精排，
质量好时直接返回前 top_k，节省延迟与算力。默认关闭（调用方不注入 reranker
即保持纯向量检索现状）。模型惰性加载，构造 / 加载失败由调用方回退纯向量检索。
"""

import logging
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


class Reranker(Protocol):
    """重排序协议：对候选片段按 query 相关度降序重排"""

    def rerank(self, query: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 query 对候选片段重排，返回降序后的列表（含 score 字段）"""
        ...


def similarity_score(distance: float) -> float:
    """L2 距离 → 相似度分数（0~1，距离越小分数越高）"""
    return 1.0 / (1.0 + float(distance))


def should_rerank(
    distances: List[float],
    score_threshold: float = 0.5,
    margin_threshold: float = 0.05,
) -> bool:
    """检索质量差 / 不确定时才需要 rerank。

    - 相似度分数：score = 1 / (1 + distance)；
    - 好检索：top1 分数 ≥ score_threshold，且 top1 与 top2 断层
      （top1 - top2）≥ margin_threshold；
    - top1 分数低或断层小 → True（触发 rerank）；
    - 空候选 / 单候选返回 False（无需无意义 rerank）。
    """
    if len(distances) < 2:
        return False
    top1 = similarity_score(distances[0])
    top2 = similarity_score(distances[1])
    if top1 < score_threshold:
        return True
    return (top1 - top2) < margin_threshold


class CrossEncoderReranker:
    """sentence-transformers CrossEncoder 本地精排（惰性加载、失败抛错回退）。

    首次调用 rerank 时才加载模型；构造 / 加载失败抛出的异常由调用方捕获
    并回退纯向量检索（零回归）。
    """

    def __init__(self, model_name: str = "BAAI/bge-reranker-base", top_k: int = 8) -> None:
        self.model_name = model_name
        self.top_k = top_k
        self._model = None  # 惰性加载
        self._load_error: Optional[Exception] = None

    def _load(self):
        """惰性加载 CrossEncoder；失败缓存异常，后续调用直接抛出"""
        if self._model is not None:
            return self._model
        if self._load_error is not None:
            raise self._load_error
        try:
            # 惰性 import：未安装 sentence-transformers 时仅 rerank 路径受影响
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name)
        except Exception as exc:  # 未安装依赖 / 下载失败等
            self._load_error = exc
            raise
        return self._model

    def rerank(self, query: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 query 相关度降序重排；候选写入 score 字段并截断 top_k"""
        if not candidates:
            return []
        model = self._load()
        pairs = [(query, c.get("text") or "") for c in candidates]
        scores = model.predict(pairs)
        ranked = sorted(
            ({"score": float(s), **c} for c, s in zip(candidates, scores)),
            key=lambda h: h["score"],
            reverse=True,
        )
        return ranked[: self.top_k]


def _maybe_rerank(
    query: str,
    hits: List[Dict[str, Any]],
    reranker: Optional[Reranker],
    top_k: int,
    score_threshold: float = 0.5,
    margin_threshold: float = 0.05,
) -> List[Dict[str, Any]]:
    """统一接入辅助：自适应重排。

    1. 无 reranker → 原样返回（现状）；
    2. 检索质量好 → 取前 top_k 返回；
    3. 质量差 / 不确定 → reranker.rerank（内部截断 top_k），失败回退前 top_k。
    """
    if not reranker:
        return hits
    distances = [float(h.get("distance", 0.0)) for h in hits]
    if not should_rerank(distances, score_threshold, margin_threshold):
        return hits[:top_k]
    try:
        return reranker.rerank(query, hits)
    except Exception as exc:
        logger.warning("rerank 失败，回退纯向量检索前 %d 条：%s", top_k, exc)
        return hits[:top_k]
