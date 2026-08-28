import hashlib
import math
import re

import pytest


class FakeEmbedder:
    """确定性伪 embedding：基于字符 bigram 哈希生成 8 维向量，词面越相似向量越接近。

    相比纯文本 hash，bigram 向量能反映字符级词面重叠，使查询与语义相关的片段
    （如"营收多少？" → "营业收入为862亿元"）检索时距离更近，更贴近真实 embedding 行为。
    同输入同输出、维度固定，测试用。
    """

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim

    def _vectorize(self, text: str):
        """文本 → 字符 bigram 计数、归一化后的 dim 维向量"""
        bigrams = set()
        compact = re.sub(r"\s+", "", text)
        for i in range(len(compact) - 1):
            bigrams.add(compact[i:i + 2])
        vec = [0.0] * self.dim
        for bg in bigrams:
            h = hashlib.sha256(bg.encode("utf-8")).digest()
            vec[h[0] % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed(self, texts):
        return [self._vectorize(t) for t in texts]


@pytest.fixture
def fake_embedder():
    return FakeEmbedder()
