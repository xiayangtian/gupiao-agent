from financial_report_fetcher.rag.embedding import Embedder, LocalEmbedder


def test_local_embedder_matches_protocol(monkeypatch):
    """LocalEmbedder 可用且向量维度一致（不真正下载模型，直接注入 fake 模型对象）"""
    fake_model = type("FakeModel", (), {})()
    calls = []

    def fake_embed(texts):
        calls.append(list(texts))
        return [[0.1, 0.2], [0.3, 0.4]]

    fake_model.embed = fake_embed
    monkeypatch.setattr("financial_report_fetcher.rag.embedding.TextEmbedding",
                        lambda model_name: fake_model)

    emb = LocalEmbedder(model_name="BAAI/bge-small-zh-v1.5")
    assert emb.model_name == "BAAI/bge-small-zh-v1.5"
    result = emb.embed(["你好", "年报"])
    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert calls == [["你好", "年报"]]


def test_local_embedder_lazy_loads_model(monkeypatch):
    """模型只在首次 embed 时加载"""
    import financial_report_fetcher.rag.embedding as mod

    loaded = []

    class FakeTextEmbedding:
        def __init__(self, model_name):
            loaded.append(model_name)

        def embed(self, texts):
            return (x for x in [[0.1], [0.2]])

    monkeypatch.setattr(mod, "TextEmbedding", FakeTextEmbedding)
    emb = LocalEmbedder()
    assert loaded == []          # 未调用 embed 前不加载
    assert emb.embed(["a"]) == [[0.1], [0.2]]  # fake 模型返回两个向量，忠实透传
    assert loaded == ["BAAI/bge-small-zh-v1.5"]
    emb.embed(["b"])                # 第二次 embed 复用已加载模型
    assert loaded == ["BAAI/bge-small-zh-v1.5"]  # 缓存：仍只有一次加载


def test_embedder_is_protocol():
    """Embedder 应可作为 Protocol 校验"""
    assert hasattr(Embedder, "embed")
