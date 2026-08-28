"""reranker 模块单元测试：自适应判断 + CrossEncoder 精排 + 统一接入辅助。

全部使用 fake model / fake reranker，不依赖网络与真实模型下载。
"""

import pytest

from financial_report_fetcher.rag.reranker import (
    CrossEncoderReranker,
    _maybe_rerank,
    should_rerank,
)


def _hit(doc_id, text="片段内容", distance=0.5):
    return {"id": doc_id, "text": text, "distance": distance, "section": "财务报告"}


# ── should_rerank 边界 ─────────────────────────────────────────────

def test_should_rerank_good_retrieval_returns_false():
    """高分 + 明显断层 → 质量好，不触发 rerank"""
    # 分数：1/1.1≈0.909、1/2≈0.5，断层约 0.409 ≥ 0.05
    assert should_rerank([0.1, 1.0]) is False


def test_should_rerank_low_top1_triggers():
    """top1 分数低于阈值 → 触发 rerank"""
    # top1 分数 1/3≈0.333 < 0.5
    assert should_rerank([2.0, 2.5]) is True


def test_should_rerank_small_margin_triggers():
    """top1 够高但 top1-top2 断层小 → 不确定，触发 rerank"""
    # top1≈0.667 ≥ 0.5，但断层 0.667-0.645≈0.022 < 0.05
    assert should_rerank([0.5, 0.55]) is True


def test_should_rerank_empty_returns_false():
    """空候选不触发无意义 rerank"""
    assert should_rerank([]) is False


def test_should_rerank_single_candidate_returns_false():
    """单候选无需 rerank"""
    assert should_rerank([0.2]) is False


def test_should_rerank_boundary_thresholds():
    """阈值边界：断层恰好达标不触发，略低于阈值触发；低 top1 必触发"""
    # [1.0, 2.0]：top1=0.5（恰好达标），断层=0.5-1/3≈0.1667
    # margin_threshold=0.1 → 断层达标 → 不触发
    assert should_rerank([1.0, 2.0], score_threshold=0.5, margin_threshold=0.1) is False
    # margin_threshold=0.2 → 断层不足 → 触发
    assert should_rerank([1.0, 2.0], score_threshold=0.5, margin_threshold=0.2) is True
    # top1 低于阈值：即使断层大也触发
    assert should_rerank([2.0, 3.0], score_threshold=0.5, margin_threshold=0.05) is True


# ── CrossEncoderReranker ───────────────────────────────────────────

class FakeCrossEncoder:
    """fake CrossEncoder：predict 返回预设分数，记录输入 pairs"""

    def __init__(self, scores):
        self.scores = scores
        self.pairs = None

    def predict(self, pairs):
        self.pairs = list(pairs)
        return self.scores


class FakeLoadReranker:
    """可注入内部 model 的 fake，绕过 sentence-transformers 导入"""

    def __init__(self, model_name="BAAI/bge-reranker-base", top_k=8):
        self.model_name = model_name
        self.top_k = top_k
        self.model = None

    def _load(self):
        return self.model


def test_cross_encoder_rerank_sorts_by_score_and_truncates(monkeypatch):
    """按分数降序重排、截断 top_k、候选写入 score 字段"""
    fake = FakeLoadReranker(top_k=2)
    fake.model = FakeCrossEncoder([0.3, 0.9, 0.6])
    # 直接借用 CrossEncoderReranker 的方法（避免实例化重写）
    reranker = CrossEncoderReranker(top_k=2)
    reranker._model = fake.model

    hits = [_hit("a", "A"), _hit("b", "B"), _hit("c", "C")]
    out = reranker.rerank("查询", hits)
    # 按分数降序：b(0.9) → c(0.6) → a(0.3)，截断 top_k=2
    assert [h["id"] for h in out] == ["b", "c"]
    assert out[0]["score"] == pytest.approx(0.9)
    assert out[0]["text"] == "B"
    # 输入 pairs 为 (query, text)
    assert fake.model.pairs == [("查询", "A"), ("查询", "B"), ("查询", "C")]


def test_cross_encoder_rerank_load_failure_raises():
    """加载失败时 rerank 抛出异常（由调用方 _maybe_rerank 回退）"""
    reranker = CrossEncoderReranker()
    reranker._load_error = RuntimeError("模型下载失败")
    with pytest.raises(RuntimeError):
        reranker.rerank("q", [_hit("a")])


def test_cross_encoder_rerank_empty_candidates():
    """空候选直接返回空列表，不触发加载"""
    reranker = CrossEncoderReranker()
    assert reranker.rerank("q", []) == []


def test_cross_encoder_lazy_loads_on_first_call(monkeypatch):
    """首次调用才加载模型；构造不触发 import（惰性）"""
    calls = []

    class FakeCE:
        def __init__(self, name):
            calls.append(name)

        def predict(self, pairs):
            return [0.5, 0.5]

    import sys

    fake_module = type(sys)("sentence_transformers")
    fake_module.CrossEncoder = FakeCE
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    reranker = CrossEncoderReranker(model_name="fake-model", top_k=2)
    assert calls == []  # 构造不加载
    reranker.rerank("q", [_hit("a"), _hit("b")])
    assert calls == ["fake-model"]


# ── _maybe_rerank 统一接入辅助 ─────────────────────────────────────

class FakeReranker:
    def __init__(self, scores=None, fail=False):
        self.scores = scores or [0.9, 0.8, 0.7]
        self.fail = fail
        self.calls = 0

    def rerank(self, query, candidates):
        self.calls += 1
        if self.fail:
            raise RuntimeError("模型不可用")
        scored = sorted(
            ({"score": s, **h} for h, s in zip(candidates, self.scores)),
            key=lambda h: h["score"], reverse=True,
        )
        return scored[:2]


def test_maybe_rerank_without_reranker_returns_as_is():
    """无 reranker → 原样返回（现状不变）"""
    hits = [_hit("a", distance=0.1), _hit("b", distance=1.0)]
    assert _maybe_rerank("q", hits, None, top_k=8) == hits


def test_maybe_rerank_good_quality_skips_reranker():
    """质量好（高分 + 断层大）→ 跳过 rerank，取前 top_k"""
    reranker = FakeReranker()
    hits = [_hit("a", distance=0.1), _hit("b", distance=1.0), _hit("c", distance=1.1)]
    out = _maybe_rerank("q", hits, reranker, top_k=2)
    assert reranker.calls == 0
    assert [h["id"] for h in out] == ["a", "b"]


def test_maybe_rerank_poor_quality_triggers_and_truncates():
    """质量差（top1 低分）→ 触发 rerank，结果截断"""
    reranker = FakeReranker(scores=[0.9, 0.8, 0.7])
    hits = [_hit("a", distance=2.0), _hit("b", distance=2.1), _hit("c", distance=2.2)]
    out = _maybe_rerank("q", hits, reranker, top_k=8)
    assert reranker.calls == 1
    # FakeReranker 内部截断 top_k=2
    assert len(out) == 2


def test_maybe_rerank_failure_falls_back_to_top_k():
    """rerank 抛错 → 回退纯向量检索前 top_k"""
    reranker = FakeReranker(fail=True)
    hits = [_hit("a", distance=0.2), _hit("b", distance=0.3), _hit("c", distance=0.4)]
    out = _maybe_rerank("q", hits, reranker, top_k=2)
    assert [h["id"] for h in out] == ["a", "b"]


# ── RagQA 接入：候选放宽 + 自适应触发 ──────────────────────────────

from financial_report_fetcher.rag.qa import RagQA


class FakeStoreQA:
    """记录每次检索 top_k 的 fake store"""

    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def query(self, text, top_k=8, where=None):
        self.calls.append({"text": text, "top_k": top_k, "where": where})
        return [dict(h) for h in self.hits]


class FakeAIChat:
    def __init__(self, content="根据[1]显示，结果为X。"):
        self.content = content

    def chat(self, messages, *, system=None, **kwargs):
        return {"content": self.content, "usage": {}}


def test_ragqa_with_reranker_broadens_candidates_and_skips_good():
    """注入 reranker：store 收到 top_k=30；质量好时不调用 reranker"""
    store = FakeStoreQA([
        _hit("a", "高分片段A", distance=0.1),
        _hit("b", "次高片段B", distance=1.0),
    ])
    reranker = FakeReranker()
    qa = RagQA(store, FakeAIChat(), top_k=2, reranker=reranker,
               rerank_candidates=30)
    result = qa.answer("查询")
    assert store.calls[0]["top_k"] == 30  # 候选放宽
    assert reranker.calls == 0            # 质量好跳过精排
    assert result is not None
    # 引用编号跟随重排后顺序（未精排时即原顺序 a→[1]）
    assert result["citations"][0]["snippet"] == "高分片段A"


def test_ragqa_with_reranker_poor_quality_triggers():
    """质量差（低分）：触发 rerank，上下文截断到 top_k"""
    store = FakeStoreQA([
        _hit("a", "低分片段A", distance=2.0),
        _hit("b", "低分片段B", distance=2.1),
        _hit("c", "低分片段C", distance=2.2),
    ])
    # 分数按原顺序 [a,b,c]=[0.5,0.7,0.9] → 精排后 c,b 靠前，截断 top_k=2
    reranker = FakeReranker(scores=[0.5, 0.7, 0.9])
    qa = RagQA(store, FakeAIChat(content="根据[1]显示，C为最佳。"),
               top_k=2, reranker=reranker, rerank_candidates=30)
    result = qa.answer("查询")
    assert reranker.calls == 1
    # 精排后只有 2 条，[1] 指向重排后的第一位（C）
    assert len(result["citations"]) == 1
    assert result["citations"][0]["snippet"] == "低分片段C"


def test_ragqa_without_reranker_keeps_original_top_k():
    """未注入 reranker：store 收到 top_k=8（现状不变）"""
    store = FakeStoreQA([_hit("a", "内容A", distance=0.3)])
    qa = RagQA(store, FakeAIChat(), top_k=8)
    qa.answer("查询")
    assert store.calls[0]["top_k"] == 8


def test_ragqa_stream_broadens_candidates_with_reranker():
    """answer_stream 同样放宽候选并自适应重排"""
    store = FakeStoreQA([
        _hit("a", "高分A", distance=0.1),
        _hit("b", "高分B", distance=1.0),
    ])
    reranker = FakeReranker()

    class FakeAIStreamQA:
        def chat_stream(self, messages, *, system=None, **kwargs):
            yield {"type": "done", "answer": "答案", "reasoning": "",
                   "model": "m", "usage": {}}

    qa = RagQA(store, FakeAIStreamQA(), top_k=2, reranker=reranker,
               rerank_candidates=30)
    events = list(qa.answer_stream("查询"))
    assert store.calls[0]["top_k"] == 30
    assert reranker.calls == 0
    assert events[-1]["type"] == "done"


# ── RagAnalysis 接入：放宽 + 合并去重 + 自适应精排 ──────────────────

from financial_report_fetcher.rag.analysis import (
    MAX_MERGED_CANDIDATES,
    RagAnalysis,
)


def _analysis_hit(doc_id, text, distance=0.5):
    return {
        "id": doc_id, "text": text, "section": "财务报告", "page": 3,
        "distance": distance, "report_id": "600900:2025-12-31:annual",
        "source": "pdf",
    }


class FakeAnalysisStore:
    """按查询返回 hit 并记录 top_k 的 fake store"""

    def __init__(self, hits_by_query):
        self.hits_by_query = hits_by_query
        self.calls = []

    def query(self, text, top_k=8, where=None):
        self.calls.append({"text": text, "top_k": top_k})
        return [dict(h) for h in self.hits_by_query.get(text, [])]


def test_raganalysis_with_reranker_broadens_and_reranks_poor():
    """注入 reranker：每路放宽到 30；低分合并结果触发精排并截断 top_k"""
    store = FakeAnalysisStore({
        "查询A": [_analysis_hit("a", "A内容", distance=2.0),
                  _analysis_hit("b", "B内容", distance=2.1)],
        "查询B": [_analysis_hit("c", "C内容", distance=2.2)],
    })
    # 分数按合并顺序 [a,b,c] 给 [0.3,0.6,0.9] → 精排后 c,b 靠前，截断 top_k=2
    reranker = FakeReranker(scores=[0.3, 0.6, 0.9])
    analysis = RagAnalysis(store, top_k=2, reranker=reranker,
                           rerank_candidates=30)
    ctx = analysis.build_context(
        report_id="r", dimension="d",
        queries=["查询A", "查询B"],
    )
    assert all(c["top_k"] == 30 for c in store.calls)  # 候选放宽
    assert reranker.calls == 1
    lines = [ln for ln in ctx.splitlines() if ln.strip()]
    assert len(lines) == 2  # 截断到 top_k
    # 精排后 c 排第一 → [1] 为 C 内容
    assert "C内容" in lines[0]


def test_raganalysis_reranker_good_quality_skips():
    """合并结果质量好（高分 + 断层大）→ 跳过精排，取前 top_k"""
    store = FakeAnalysisStore({
        "查询A": [_analysis_hit("a", "A内容", distance=0.1),
                  _analysis_hit("b", "B内容", distance=1.0),
                  _analysis_hit("c", "C内容", distance=1.1)],
    })
    reranker = FakeReranker()
    analysis = RagAnalysis(store, top_k=2, reranker=reranker,
                           rerank_candidates=30)
    ctx = analysis.build_context(report_id="r", dimension="d", queries=["查询A"])
    assert reranker.calls == 0
    lines = [ln for ln in ctx.splitlines() if ln.strip()]
    assert len(lines) == 2  # 质量好取前 top_k
    assert "A内容" in lines[0]  # 维持按距离排序


def test_raganalysis_without_reranker_keeps_original_behavior():
    """未注入 reranker：每路 top_k 现状（8）、不截断合并结果"""
    store = FakeAnalysisStore({
        "查询A": [_analysis_hit("a", "A内容", distance=0.1),
                  _analysis_hit("b", "B内容", distance=0.2),
                  _analysis_hit("c", "C内容", distance=0.3)],
    })
    analysis = RagAnalysis(store, top_k=2)
    ctx = analysis.build_context(report_id="r", dimension="d", queries=["查询A"])
    assert store.calls[0]["top_k"] == 2  # 无 reranker 时用维度 top_k
    lines = [ln for ln in ctx.splitlines() if ln.strip()]
    assert len(lines) == 3  # 不截断（现状行为）


def test_raganalysis_rerank_failure_falls_back():
    """rerank 抛错 → 回退按距离排序取前 top_k"""
    store = FakeAnalysisStore({
        "查询A": [_analysis_hit("a", "A内容", distance=2.0),
                  _analysis_hit("b", "B内容", distance=2.1),
                  _analysis_hit("c", "C内容", distance=2.2)],
    })
    analysis = RagAnalysis(store, top_k=2, reranker=FakeReranker(fail=True),
                           rerank_candidates=30)
    ctx = analysis.build_context(report_id="r", dimension="d", queries=["查询A"])
    lines = [ln for ln in ctx.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert "A内容" in lines[0]  # 按距离排序的前 2 条


def test_raganalysis_merge_caps_at_max_candidates():
    """合并去重候选数达到上限后停止继续合并（不注入 reranker 时也生效）"""
    hits_a = [_analysis_hit(f"a{i}", f"文本{i}", distance=float(i))
              for i in range(MAX_MERGED_CANDIDATES + 1)]
    store = FakeAnalysisStore({
        "查询A": hits_a,
        "查询B": [_analysis_hit("b0", "B文本", distance=0.1)],
    })
    analysis = RagAnalysis(store, top_k=8)
    ctx = analysis.build_context(report_id="r", dimension="d",
                                 queries=["查询A", "查询B"])
    # 第一路 61 条已达上限 60，停止合并；第二路不再检索
    assert len(store.calls) == 1
    lines = [ln for ln in ctx.splitlines() if ln.strip()]
    assert len(lines) == MAX_MERGED_CANDIDATES


# ── RagConfig 新字段 ───────────────────────────────────────────────

def test_ragconfig_rerank_fields_defaults(tmp_path, monkeypatch):
    """无配置时 rerank 相关字段使用默认值（默认关闭）"""
    from financial_report_fetcher.rag.config import RagConfig

    monkeypatch.setattr("financial_report_fetcher.rag.config._CONFIG_PATHS",
                        [str(tmp_path / "none.yaml")])
    cfg = RagConfig.load()
    assert cfg.rerank is False
    assert cfg.rerank_model == "BAAI/bge-reranker-base"
    assert cfg.rerank_candidates == 30
    assert cfg.rerank_score_threshold == 0.5
    assert cfg.rerank_margin_threshold == 0.05


def test_ragconfig_rerank_explicit_and_string_false(tmp_path, monkeypatch):
    """显式配置解析；字符串 'false' 防御不误判为 True"""
    from financial_report_fetcher.rag.config import RagConfig

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "rag:\n"
        "  enabled: true\n"
        "  rerank: \"false\"\n"
        "  rerank_model: fake-model\n"
        "  rerank_candidates: 50\n"
        "  rerank_score_threshold: 0.6\n"
        "  rerank_margin_threshold: 0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("financial_report_fetcher.rag.config._CONFIG_PATHS", [str(yaml_path)])
    cfg = RagConfig.load()
    assert cfg.rerank is False  # 字符串 false 防御
    assert cfg.rerank_model == "fake-model"
    assert cfg.rerank_candidates == 50
    assert cfg.rerank_score_threshold == 0.6
    assert cfg.rerank_margin_threshold == 0.1


def test_ragconfig_rerank_true(tmp_path, monkeypatch):
    """显式 rerank: true 时开启"""
    from financial_report_fetcher.rag.config import RagConfig

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("rag:\n  enabled: true\n  rerank: true\n", encoding="utf-8")
    monkeypatch.setattr("financial_report_fetcher.rag.config._CONFIG_PATHS", [str(yaml_path)])
    assert RagConfig.load().rerank is True


# ── server / CLI 注入与回退 ────────────────────────────────────────

def test_cli_build_reranker_disabled_returns_none():
    """cfg.rerank=false → 不构造 reranker（None）"""
    from financial_report_fetcher import __main__ as main

    cfg = type("C", (), {"rerank": False})()
    assert main._build_reranker(cfg) is None


def test_cli_build_reranker_enabled(monkeypatch):
    """cfg.rerank=true → 构造 CrossEncoderReranker（model + top_k）"""
    from financial_report_fetcher import __main__ as main
    from financial_report_fetcher.rag import reranker as reranker_mod

    created = []

    class FakeCrossEncoderReranker:
        def __init__(self, model_name, top_k=8):
            created.append((model_name, top_k))

    monkeypatch.setattr(reranker_mod, "CrossEncoderReranker", FakeCrossEncoderReranker)
    cfg = type("C", (), {"rerank": True, "rerank_model": "fake-model", "top_k": 4})()
    out = main._build_reranker(cfg)
    assert isinstance(out, FakeCrossEncoderReranker)
    assert created == [("fake-model", 4)]


def test_cli_build_reranker_failure_returns_none(monkeypatch):
    """构造抛错（未装依赖等）→ 回退 None（零回归）"""
    from financial_report_fetcher import __main__ as main
    from financial_report_fetcher.rag import reranker as reranker_mod

    def boom(*a, **kw):
        raise RuntimeError("未安装 sentence-transformers")

    monkeypatch.setattr(reranker_mod, "CrossEncoderReranker", boom)
    cfg = type("C", (), {"rerank": True, "rerank_model": "x", "top_k": 8})()
    assert main._build_reranker(cfg) is None


def test_server_build_reranker_enabled_and_failure(monkeypatch):
    """server 侧 _build_reranker：启用时构造、失败回退 None"""
    import webapp.server as server
    from financial_report_fetcher.rag import reranker as reranker_mod

    created = []

    class FakeCrossEncoderReranker:
        def __init__(self, model_name, top_k=8):
            created.append((model_name, top_k))

    monkeypatch.setattr(reranker_mod, "CrossEncoderReranker", FakeCrossEncoderReranker)
    cfg = type("C", (), {"rerank": True, "rerank_model": "m", "top_k": 6})()
    out = server._build_reranker(cfg)
    assert isinstance(out, FakeCrossEncoderReranker)
    assert created == [("m", 6)]

    def boom(*a, **kw):
        raise RuntimeError("模型下载失败")

    monkeypatch.setattr(reranker_mod, "CrossEncoderReranker", boom)
    assert server._build_reranker(type("C", (), {"rerank": True, "rerank_model": "x", "top_k": 8})()) is None


def test_init_rag_injects_reranker_into_qa_and_analysis(monkeypatch, tmp_path):
    """_init_rag 把构造的 reranker 注入 RagQA 与 RagAnalysis"""
    import webapp.server as server
    from types import SimpleNamespace

    fake_reranker = object()
    fake_cfg = SimpleNamespace(
        enabled=True, store_path=str(tmp_path), chunk_size=800, chunk_overlap=100,
        top_k=8, embedding_model="fake-model", auto_ingest=True,
        enhanced_analysis=True, rerank_candidates=30,
        rerank_score_threshold=0.5, rerank_margin_threshold=0.05,
    )
    received = {}

    class FakeEmbedder:
        def __init__(self, *a, **kw):
            pass

    class FakeRagStore:
        def __init__(self, *a, **kw):
            pass

    class FakeSvc:
        def __init__(self, *a, **kw):
            pass

    class FakeQA:
        def __init__(self, *a, **kw):
            received["qa"] = kw

    class FakeRagAnalysis:
        def __init__(self, *a, **kw):
            received["analysis"] = kw

    monkeypatch.setattr(server, "RagConfig",
                        type("C", (), {"load": staticmethod(lambda: fake_cfg)}))
    monkeypatch.setattr(server, "LocalEmbedder", FakeEmbedder)
    monkeypatch.setattr(server, "RagStore", FakeRagStore)
    monkeypatch.setattr(server, "IngestionService", FakeSvc)
    monkeypatch.setattr(server, "RagQA", FakeQA)
    monkeypatch.setattr(server, "RagAnalysis", FakeRagAnalysis)
    monkeypatch.setattr(server, "_build_reranker", lambda cfg: fake_reranker)

    orig = (server.analyzer, server.rag_store, server.rag_service, server.rag_qa)
    try:
        server._init_rag()
        assert received["qa"]["reranker"] is fake_reranker
        assert received["qa"]["rerank_candidates"] == 30
        assert received["analysis"]["reranker"] is fake_reranker
        assert received["analysis"]["rerank_candidates"] == 30
    finally:
        server.analyzer, server.rag_store, server.rag_service, server.rag_qa = orig
