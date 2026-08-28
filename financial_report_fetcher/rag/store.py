"""RagStore — ChromaDB 持久化封装。

职责：chunk upsert（幂等）、向量检索 + 元数据过滤、按报告删除、状态统计。
Embedding 通过构造注入的 embedder 计算（生产 LocalEmbedder，测试 FakeEmbedder）。
"""

import logging
from typing import Any, Dict, List, Optional

import chromadb

from .chunking import Chunk

logger = logging.getLogger(__name__)


def _clean_meta(chunk: Chunk) -> Dict[str, Any]:
    """ChromaDB metadata 只接受标量；None 值剔除"""
    meta: Dict[str, Any] = {
        "report_id": chunk.report_id,
        "source": chunk.source,
        "section": chunk.section,
        "chunk_index": chunk.chunk_index,
        "year": int(chunk.report_id.split(":")[1][:4]),
    }
    if chunk.page is not None:
        meta["page"] = chunk.page
    meta.update({k: v for k, v in chunk.meta.items() if v is not None})
    return meta


class RagStore:
    def __init__(self, path: str, embedder, collection_name: str = "annual_reports") -> None:
        self._client = chromadb.PersistentClient(path=path)
        self._collection = self._client.get_or_create_collection(collection_name)
        self._embedder = embedder

    def upsert(self, chunks: List[Chunk]) -> int:
        if not chunks:
            return 0
        self._collection.upsert(
            ids=[c.id for c in chunks],
            documents=[c.text for c in chunks],
            metadatas=[_clean_meta(c) for c in chunks],
            embeddings=self._embedder.embed([c.text for c in chunks]),
        )
        return len(chunks)

    def query(self, text: str, top_k: int = 8, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        if top_k <= 0:
            return []
        emb = self._embedder.embed([text])[0]
        res = self._collection.query(
            query_embeddings=[emb],
            n_results=top_k,
            where=where,
        )
        out: List[Dict[str, Any]] = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for i, doc_id in enumerate(ids):
            m = dict(metas[i] or {})
            out.append({
                "id": doc_id,
                "text": docs[i],
                "distance": dists[i],
                **m,
            })
        return out

    def delete_report(self, report_id: str) -> None:
        self._collection.delete(where={"report_id": report_id})

    def delete_file(self, report_id: str, source: str) -> None:
        """删除单文件（report_id + source）的全部 chunk"""
        # chromadb 1.5.9 delete 的 where 顶层只允许一个操作符，多字段需用 $and
        self._collection.delete(where={"$and": [{"report_id": report_id}, {"source": source}]})

    def count_chunks(self, report_id: Optional[str] = None) -> int:
        if report_id is None:
            return self._collection.count()
        # chromadb 1.5.9 的 count() 不支持 where 参数，改用 get(where=...) 统计
        res = self._collection.get(where={"report_id": report_id}, include=["metadatas"])
        return len(res.get("ids", []))

    def list_report_ids(self) -> List[str]:
        res = self._collection.get(include=["metadatas"])
        seen: Dict[str, None] = {}
        for m in res.get("metadatas", []) or []:
            if m and m.get("report_id"):
                seen[m["report_id"]] = None
        return sorted(seen)
