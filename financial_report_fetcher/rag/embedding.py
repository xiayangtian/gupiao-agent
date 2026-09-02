"""Embedding 抽象与本地实现。

设计：通过 Embedder Protocol 解耦，生产用 fastembed 加载 bge 中文模型（惰性），
测试可注入确定性伪实现（见 tests/unit/conftest.py）。
"""

import os
from typing import Any, List, Optional, Protocol


# 保留可注入的模块级工厂，同时避免在模块加载时导入 fastembed。
TextEmbedding: Any = None


class Embedder(Protocol):
    """文本向量化协议：embed(texts) -> 等长向量列表"""

    def embed(self, texts: List[str]) -> List[List[float]]:
        ...


class LocalEmbedder:
    """基于 fastembed（ONNX 运行时）的本地中文 Embedding。

    模型默认 BAAI/bge-small-zh-v1.5（约 100MB，首次 embed 时自动下载）。
    惰性加载：构造时不加载模型，首次 embed 才初始化，避免拖慢 import。
    """

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5",
                 hf_endpoint: Optional[str] = None) -> None:
        self.model_name = model_name
        self.hf_endpoint = (hf_endpoint or "").strip() or None
        self._model = None

    def _ensure_model(self) -> None:
        if self._model is None:
            # huggingface_hub 在 import 时读取 HF_ENDPOINT，因此 fastembed 必须
            # 在设置镜像后再延迟导入，否则运行期配置不会生效。
            if self.hf_endpoint and not os.environ.get("HF_ENDPOINT"):
                os.environ["HF_ENDPOINT"] = self.hf_endpoint
            factory = TextEmbedding
            if factory is None:
                from fastembed import TextEmbedding as factory

            self._model = factory(model_name=self.model_name)

    def embed(self, texts: List[str]) -> List[List[float]]:
        self._ensure_model()
        # tolist 优先（fastembed 返回 numpy 向量 -> List[float]）；纯 list 兼容测试注入的 fake 模型
        return [emb.tolist() if hasattr(emb, "tolist") else list(emb) for emb in self._model.embed(texts)]
