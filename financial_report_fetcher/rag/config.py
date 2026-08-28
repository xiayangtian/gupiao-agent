"""RAG 配置：读取 config.yaml 的 rag: 段；缺省关闭。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List

import yaml

_CONFIG_PATHS = ["config.yaml", "config.yml"]


def _load_yaml() -> Dict[str, Any]:
    for name in _CONFIG_PATHS:
        try:
            with open(name, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                return data
        except OSError:
            continue
    return {}


def _to_bool(value: Any, default: bool) -> bool:
    """把 YAML 值转 bool；防御字符串 "false"/"0" 被 bool() 误判为 True 的陷阱"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


@dataclass
class RagConfig:
    enabled: bool = False
    store_path: str = "data/rag"
    chunk_size: int = 800
    chunk_overlap: int = 100
    top_k: int = 8
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    auto_ingest: bool = True
    # 增强分析：分析时用 RAG 检索上下文替代截断全文（需 enabled: true）
    enhanced_analysis: bool = True
    # 分析页默认勾选维度；空列表时用 analyzer 内置默认 5 个维度
    analysis_dimensions: List[str] = field(default_factory=list)
    # 问答接入 MCP 工具：允许模型按需调用外部数据工具（需 enabled: true）
    mcp_tools: bool = True
    mcp_tool_timeout: int = 30          # 单次工具调用超时（秒）
    mcp_max_tool_rounds: int = 3        # 最大工具调用轮数
    mcp_tool_whitelist: List[str] = field(default_factory=list)  # 空=自动常用清单
    # 自适应 rerank：仅检索质量差/不确定时用 cross-encoder 精排（需安装 sentence-transformers）
    rerank: bool = False
    rerank_model: str = "BAAI/bge-reranker-base"
    rerank_candidates: int = 30      # 精排前放宽召回数
    rerank_score_threshold: float = 0.5    # top-1 相似度低于此值 → 触发 rerank
    rerank_margin_threshold: float = 0.05  # top-1 与 top-2 断层小于此值 → 触发 rerank

    @classmethod
    def load(cls) -> "RagConfig":
        raw = (_load_yaml().get("rag") or {})
        dims_raw = raw.get("analysis_dimensions")
        dims = [str(d) for d in dims_raw] if isinstance(dims_raw, list) else []
        return cls(
            enabled=_to_bool(raw.get("enabled"), False),
            store_path=str(raw.get("store_path", "data/rag")),
            chunk_size=int(raw.get("chunk_size", 800)),
            chunk_overlap=int(raw.get("chunk_overlap", 100)),
            top_k=int(raw.get("top_k", 8)),
            embedding_model=str(raw.get("embedding_model", "BAAI/bge-small-zh-v1.5")),
            auto_ingest=_to_bool(raw.get("auto_ingest"), True),
            enhanced_analysis=_to_bool(raw.get("enhanced_analysis"), True),
            analysis_dimensions=dims,
            mcp_tools=_to_bool(raw.get("mcp_tools"), True),
            mcp_tool_timeout=int(raw.get("mcp_tool_timeout", 30)),
            mcp_max_tool_rounds=int(raw.get("mcp_max_tool_rounds", 3)),
            mcp_tool_whitelist=[str(w) for w in raw.get("mcp_tool_whitelist", [])]
            if isinstance(raw.get("mcp_tool_whitelist"), list) else [],
            rerank=_to_bool(raw.get("rerank"), False),
            rerank_model=str(raw.get("rerank_model", "BAAI/bge-reranker-base")),
            rerank_candidates=int(raw.get("rerank_candidates", 30)),
            rerank_score_threshold=float(raw.get("rerank_score_threshold", 0.5)),
            rerank_margin_threshold=float(raw.get("rerank_margin_threshold", 0.05)),
        )
