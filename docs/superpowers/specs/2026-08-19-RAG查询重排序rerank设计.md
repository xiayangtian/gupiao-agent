# RAG 查询重排序（Rerank）设计文档

> 日期：2026-08-19
> 状态：设计方案（未实施）
> 前置：RAG 知识库（已实现，纯向量检索无 rerank）

## 1. 背景与目标

### 1.1 现状

- 检索为**单阶段**：`bge-small-zh-v1.5` 向量 + ChromaDB HNSW 近似检索，`top_k=8`
  直接按向量距离取回，无 cross-encoder 精排。
- 局限：向量召回对「语义近似但词面不同」「跨章节长文档」等场景命中精度有限，
  靠 LLM 自行在片段中取舍。

### 1.2 目标

1. 引入 **cross-encoder 精排（rerank）** 提升检索命中精度。
2. **自适应 rerank**：根据首轮检索分数判断检索质量，仅在「效果不好」时才走
   rerank，质量好时跳过以节省延迟与算力（用户核心诉求）。
3. 零回归：默认关闭（`rag.rerank: false`）时行为与现状完全一致；
   启用失败（未装依赖/模型下载失败）自动回退纯向量检索。

### 1.3 方案选择

- **方案 A（选定）**：`sentence-transformers` 的 `CrossEncoder("BAAI/bge-reranker-base")`
  本地精排（模型约 1GB，惰性加载）。
  理由：fastembed 0.8.0 无 `TextReranking`（已实测导入失败），CrossEncoder API 简单、
  与现有 Embedder 注入式设计同构，测试可用 fake model 注入。

---

## 2. 总体流程（自适应）

```text
query
  → store.query(top_k=rerank_candidates=30)      # 放宽召回
  → should_rerank(distances)                      # 按检索分数判断
       ├─ 分数好（top1 高 + 断层大）→ 直接取前 top_k（跳过 rerank）
       └─ 分数差 / 不确定 → CrossEncoder 精排 → 取前 top_k
  → 上下文组装（[n] 编号 + 章节 + 页码）→ LLM
```

---

## 3. 模块设计

### 3.1 `rag/reranker.py`（新增）

```python
class Reranker(Protocol):
    """重排序协议：对候选片段按 query 相关度降序重排"""
    def rerank(self, query: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]: ...


class CrossEncoderReranker:
    """sentence-transformers CrossEncoder 本地精排（惰性加载、失败回退）"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-base",
                 top_k: int = 8):
        self._model = None          # 惰性加载
        self._load_error: Optional[Exception] = None

    def rerank(self, query, candidates) -> List[Dict[str, Any]]:
        # 首次调用加载模型；失败抛错由调用方回退
        scores = self._model.predict([(query, c["text"]) for c in candidates])
        # 按分数降序，返回 [{"score": ..., **candidate}]，截断 top_k
```

### 3.2 自适应判断（纯函数，核心）

```python
def should_rerank(distances: List[float],
                  score_threshold: float = 0.5,
                  margin_threshold: float = 0.05) -> bool:
    """检索质量差/不确定时才需要 rerank。

    - 相似度分数：score = 1 / (1 + distance)（L2 距离越小分数越高，0~1）
    - 好检索：top1 分数足够高（≥ score_threshold）
      且 top1 与 top2 有明显断层（margin ≥ margin_threshold）
    - 否则返回 True（触发 rerank）
    """
```

- 空候选返回 False（不触发无意义 rerank）。
- 阈值可配置；分数用相对 margin 判断比绝对阈值更稳健（不同库距离尺度不同）。

### 3.3 统一接入辅助

`_maybe_rerank(query, hits, reranker, top_k, ...) -> List[Dict]`（放 reranker.py）：
1. 无 reranker → 原样返回（现状）；
2. `should_rerank([h["distance"] for h in hits])` 为 False → 取前 `top_k` 返回；
3. 为 True → `reranker.rerank(query, hits)`（内部截断 top_k），失败回退前 top_k。

---

## 4. 接入点

### 4.1 `RagQA.answer` / `answer_stream`（rag/qa.py）

- 构造新增 `reranker: Optional[Reranker] = None`、`rerank_candidates: int = 30`；
- 检索改为 `store.query(question, top_k=rerank_candidates if reranker else self.top_k, ...)`；
- 检索后 `hits = _maybe_rerank(question, hits, ...)`，再组装上下文；
- 引用校验机制不变（[n] 编号跟随重排后的 hits 顺序）。

### 4.2 `RagAnalysis.build_context`（rag/analysis.py）

- 构造新增 `reranker` / `rerank_candidates`；
- 每路 `store.query(top_k=rerank_candidates)` → 合并去重（上限如 60）→
  基于合并结果的 distance 判断 → 需要则对合并结果精排 → 取 top_k → 组装；
- 未注入 reranker 时维持现有「按 distance 排序」逻辑。

### 4.3 server / CLI 注入

- `_init_rag()` / `_build_rag_components()`：`cfg.rerank` 为 true 时构造
  `CrossEncoderReranker(model, top_k=cfg.top_k)` 注入 `RagQA` 与 `RagAnalysis`；
  构造/加载失败 → 日志警告并回退 None（纯向量，零回归）。
- CLI `analyze` / `rag chat` 同样注入（复用同一构建函数）。

---

## 5. 配置（config.example.yaml 扩展）

```yaml
rag:
  rerank: false               # 启用 cross-encoder 精排（需安装 sentence-transformers 并下载模型 ~1GB）
  rerank_model: BAAI/bge-reranker-base
  rerank_candidates: 30       # 精排前放宽召回数
  rerank_score_threshold: 0.5 # top-1 相似度低于此值 → 判定检索质量差，触发 rerank
  rerank_margin_threshold: 0.05  # top-1 与 top-2 差距小于此值 → 判定不确定，触发 rerank
```

`RagConfig` 新增对应字段（默认如上）；bool 解析沿用 `_to_bool`。

---

## 6. 依赖

`requirements.txt` 增加（可选注释说明）：

```
# rerank（RAG 精排；模型首次运行时自动下载 ~1GB）
sentence-transformers>=3.0
```

不强制安装：未安装时 `CrossEncoderReranker` 构造失败 → 回退纯向量检索。

---

## 7. 测试计划

| 层 | 用例 |
|---|---|
| reranker 纯函数 | `should_rerank`：高分跳过（False）/ 低分触发（True）/ 断层小触发 / 空候选 False / 边界值 |
| CrossEncoderReranker | 注入 fake model（`predict` 返回预设分数）：按分数降序、截断 top_k、`score` 字段写入 |
| RagQA | fake reranker：候选放宽（store 收到 top_k=30）；好分数不调 reranker；差分数调用且结果截断 top_k；未注入时现状（top_k=8） |
| RagAnalysis | 多路检索合并后 rerank；未注入时维持 distance 排序 |
| RagConfig | 新字段默认值/显式解析（含字符串 false 防御） |
| server/CLI | `_init_rag` 注入 reranker；构造失败回退 None |
| 回归 | 默认 `rerank=false` 全量 pytest 无回归 |

---

## 8. 实施分期

| 阶段 | 内容 |
|---|---|
| **本次（待批准/空闲时段）** | T1 reranker 模块（协议 + CrossEncoder + should_rerank）；T2 RagQA 接入；T3 RagAnalysis 接入；T4 RagConfig + server/CLI + requirements；T5 README + config.example + 全量回归 |
| **Phase 2** | 阈值自动校准（按库统计分布）、rerank 结果缓存、批量分析也走精排的开关、前端展示「已重排」标记 |

---

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| 模型下载大（~1GB）/慢 | 惰性加载 + 首次失败回退纯向量；可离线预下载 |
| 阈值不通用（库/embedding 不同） | 阈值可配置；用 top1/top2 相对 margin 判断更稳健 |
| rerank 增加延迟 | 自适应跳过（质量好直接返回）+ 候选数上限 30 |
| 重排改变片段顺序影响引用 | [n] 编号跟随重排结果，引用校验机制不变，编号始终落在检索片段内 |
| sentence-transformers 与 fastembed 冲突 | 均为独立依赖；CrossEncoder 仅 rerank 时惰性 import |
