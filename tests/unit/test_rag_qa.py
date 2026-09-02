from financial_report_fetcher.rag.qa import RagQA
from financial_report_fetcher.rag.store import RagStore
from financial_report_fetcher.rag.chunking import Chunk


class FakeAI:
    def __init__(self, content="根据[1]显示，营业收入为862亿元。"):
        self.content = content
        self.last_messages = None
        self.last_system = None

    def chat(self, messages, *, system=None, **kwargs):
        self.last_messages = messages
        self.last_system = system
        return {"content": self.content, "usage": {"total_tokens": 10}}


def _chunk(text, section="财务摘要", source="analysis", rid="600900:2025-12-31:annual"):
    return Chunk(report_id=rid, source=source, text=text, section=section, page=None, chunk_index=0)


def test_answer_returns_answer_and_valid_citations(tmp_path, fake_embedder):
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("营业收入为862亿元"), _chunk("净利润345亿元")])
    ai = FakeAI()
    qa = RagQA(store, ai, top_k=4)
    result = qa.answer("营收多少？")
    assert result is not None
    assert result["answer"] == ai.content
    # [1] 引用有效 → citations 含第一条片段
    assert len(result["citations"]) == 1
    assert result["citations"][0]["snippet"] == "营业收入为862亿元"


def test_answer_invalid_citation_filtered(tmp_path, fake_embedder):
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("只有一条内容")])
    ai = FakeAI(content="根据[9]说明……")  # 引用了不存在的编号
    qa = RagQA(store, ai, top_k=4)
    result = qa.answer("x")
    assert result["citations"] == []


def test_answer_empty_retrieval_returns_none(tmp_path, fake_embedder):
    store = RagStore(str(tmp_path), fake_embedder)  # 空库
    qa = RagQA(store, FakeAI(), top_k=4)
    assert qa.answer("随便问问") is None


def test_try_answer_report_filters_by_report_id(tmp_path, fake_embedder):
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("A公司内容", rid="600900:2025-12-31:annual")])
    qa = RagQA(store, FakeAI(), top_k=4)
    result = qa.try_answer_report("600900", "2025-12-31", "营收？")
    assert result is not None
    assert result["citations"][0]["report_id"] == "600900:2025-12-31:annual"


def test_try_answer_report_unknown_report_returns_none(tmp_path, fake_embedder):
    store = RagStore(str(tmp_path), fake_embedder)  # 空库
    qa = RagQA(store, FakeAI(), top_k=4)
    assert qa.try_answer_report("600519", "2025-12-31", "营收？") is None


def test_build_report_id_keeps_q3_period():
    """Q3 查询必须构造与入库一致的 09-30 身份。"""
    assert RagQA.build_report_id("600900", "2025-09-30") == "600900:2025-09-30:quarterly"


def test_try_answer_report_september_preserves_q3_period(tmp_path, fake_embedder):
    """Q3 检索只命中 Q3 的报告身份。"""
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("三季报内容", rid="600900:2025-09-30:quarterly")])
    qa = RagQA(store, FakeAI(), top_k=4)
    result = qa.try_answer_report("600900", "2025-09-30", "三季报？")
    assert result is not None
    assert result["citations"][0]["report_id"] == "600900:2025-09-30:quarterly"


class FakeAIStream:
    """流式 fake：chat_stream 产出 delta / done 事件"""

    def __init__(self, deltas=("营业收入", "为862亿"), answer="营业收入为862亿"):
        self.deltas = deltas
        self.answer = answer
        self.last_messages = None
        self.last_system = None

    def chat_stream(self, messages, *, system=None, **kwargs):
        self.last_messages = messages
        self.last_system = system
        for chunk in self.deltas:
            yield {"type": "delta", "text": chunk, "reasoning": ""}
        yield {"type": "done", "answer": self.answer, "reasoning": "",
               "model": "test-model", "usage": {"total_tokens": 10}}


def test_answer_stream_yields_deltas_and_done_with_citations(tmp_path, fake_embedder):
    """流式回答：增量逐条 yield，done 携带完整答案与有效引用"""
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("营业收入为862亿元"), _chunk("净利润345亿元")])
    ai = FakeAIStream(answer="根据[1]显示，营业收入为862亿元。")
    qa = RagQA(store, ai, top_k=4)
    events = list(qa.answer_stream("营收多少？"))
    delta_texts = [e["text"] for e in events if e["type"] == "delta"]
    assert delta_texts == ["营业收入", "为862亿"]
    done = events[-1]
    assert done["type"] == "done"
    assert done["answer"] == "根据[1]显示，营业收入为862亿元。"
    assert len(done["citations"]) == 1
    assert done["citations"][0]["snippet"] == "营业收入为862亿元"
    # history 透传给模型
    assert ai.last_messages[-1] == {"role": "user", "content": "营收多少？"}


def test_answer_stream_empty_retrieval_yields_empty(tmp_path, fake_embedder):
    """检索为空时产出 empty 事件（调用方决定兜底文案）"""
    store = RagStore(str(tmp_path), fake_embedder)  # 空库
    qa = RagQA(store, FakeAIStream(), top_k=4)
    events = list(qa.answer_stream("随便问问"))
    assert events == [{"type": "empty"}]


def test_answer_stream_empty_retrieval_still_allows_mcp_tools(tmp_path, fake_embedder):
    store = RagStore(str(tmp_path), fake_embedder)
    ai = FakeToolAI([
        [_tool_calls_event([{
            "id": "call_empty", "name": "get_financial_metrics",
            "arguments": '{"symbol":"600519"}',
        }])],
        [_done_event("根据 MCP 数据回答")],
    ])
    executed = []
    qa = RagQA(store, ai, top_k=4, tool_executor=lambda name, args: (
        executed.append((name, args)) or '{"revenue": 100}'
    ))

    events = list(qa.answer_stream("营收多少？", tools=[{"type": "function"}]))

    assert events[-1]["type"] == "done"
    assert executed == [("get_financial_metrics", {"symbol": "600519"})]
    assert events[-1]["citations"] == []
    assert "可以调用提供的 MCP 工具" in ai.calls[0]["system"]


def test_answer_stream_embedding_failure_degrades_without_citations():
    class BrokenStore:
        def query(self, *args, **kwargs):
            raise TimeoutError("Hugging Face unavailable")

    ai = FakeAIStream(answer="当前无法核验具体财报数字。")
    qa = RagQA(BrokenStore(), ai, top_k=4)

    events = list(qa.answer_stream("营收多少？"))

    done = events[-1]
    assert done["type"] == "done"
    assert done["retrieval_degraded"] is True
    assert done["citations"] == []
    assert "本地财报检索服务暂时不可用" in ai.last_system


def test_answer_stream_passthrough_error(tmp_path, fake_embedder):
    """模型流式出错时透传 error 事件"""
    class ErrAI:
        def chat_stream(self, messages, *, system=None, **kwargs):
            yield {"type": "error", "error": "AI 服务返回 HTTP 500，请稍后重试"}

    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("营业收入为862亿元")])
    qa = RagQA(store, ErrAI(), top_k=4)
    events = list(qa.answer_stream("营收多少？"))
    assert events[-1]["type"] == "error"
    assert "HTTP 500" in events[-1]["error"]


class FakeToolAI:
    """流式 fake：按调用次数返回预设事件序列，记录 messages/tools"""

    def __init__(self, sequences):
        self.sequences = sequences
        self.calls = []

    def chat_stream(self, messages, *, system=None, **kwargs):
        self.calls.append({"messages": list(messages), "system": system, "kwargs": kwargs})
        idx = len(self.calls) - 1
        for evt in self.sequences[min(idx, len(self.sequences) - 1)]:
            yield dict(evt)


def _tool_calls_event(calls):
    return {"type": "tool_calls", "tool_calls": calls}


def _done_event(answer, citations_hint=""):
    return {"type": "done", "answer": answer, "reasoning": "", "model": "m", "usage": {}}


def test_answer_stream_with_tools_executes_and_finishes(tmp_path, fake_embedder):
    """第一轮模型请求工具 → 执行 MCP → 第二轮生成最终答案（含引用与 tools_used）"""
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("营业收入为862亿元")])
    ai = FakeToolAI([
        [
            _tool_calls_event([{
                "id": "call_1", "name": "get_financial_metrics",
                "arguments": '{"symbol":"600519"}',
            }]),
        ],
        [
            {"type": "delta", "text": "根据[1]与", "reasoning": ""},
            {"type": "delta", "text": "MCP 数据", "reasoning": ""},
            _done_event("根据[1]与MCP数据，营收862亿"),
        ],
    ])
    executed = []
    qa = RagQA(store, ai, top_k=4, tool_executor=lambda name, args: (
        executed.append((name, args)) or '{"net_profit": 345}'
    ))
    events = list(qa.answer_stream("营收如何？", tools=[{"type": "function"}]))
    types = [e["type"] for e in events]
    assert "tool_call" in types and "tool_result" in types
    assert executed == [("get_financial_metrics", {"symbol": "600519"})]
    done = events[-1]
    assert done["type"] == "done"
    assert done["tools_used"] == ["get_financial_metrics"]
    assert len(done["citations"]) == 1  # RAG 引用仍有效
    # 第二轮仍带 tools，允许模型在参数失败时修正调用
    assert ai.calls[1]["kwargs"].get("tools") == [{"type": "function"}]
    # assistant tool_calls + tool 消息已追加
    roles = [m["role"] for m in ai.calls[1]["messages"]]
    assert roles == ["user", "assistant", "tool"]


def test_answer_stream_without_executor_ignores_tools(tmp_path, fake_embedder):
    """未注入 tool_executor 时即使传 tools 也走纯 RAG 路径（现状）"""
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("营业收入为862亿元")])
    ai = FakeToolAI([[_done_event("纯RAG答案")]])
    qa = RagQA(store, ai, top_k=4)  # 无 executor
    events = list(qa.answer_stream("营收如何？", tools=[{"type": "function"}]))
    assert [e["type"] for e in events] == ["done"]
    assert ai.calls[0]["kwargs"].get("tools") is None


def test_answer_stream_tool_error_passed_back(tmp_path, fake_embedder):
    """工具执行异常：错误文本作为 tool 消息回传，模型可解释"""
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("内容A")])
    ai = FakeToolAI([
        [_tool_calls_event([{"id": "c9", "name": "get_realtime_quote", "arguments": "{}"}])],
        [_done_event("行情数据暂不可得")],
    ])

    def _fail(name, args):
        raise RuntimeError("MCP 超时")

    qa = RagQA(store, ai, top_k=4, tool_executor=_fail)
    events = list(qa.answer_stream("现价？", tools=[{"type": "function"}]))
    tool_msgs = [m for m in ai.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs and "MCP 超时" in tool_msgs[0]["content"]


def test_answer_stream_respects_max_tool_rounds(tmp_path, fake_embedder):
    """每轮都请求工具时，达到上限后强制生成最终答案"""
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("内容B")])
    ai = FakeToolAI([
        [_tool_calls_event([{"id": "c1", "name": "get_news_data", "arguments": "{}"}])],
        [_tool_calls_event([{"id": "c2", "name": "get_news_data", "arguments": "{}"}])],
        [_done_event("最终答案")],
    ])
    qa = RagQA(store, ai, top_k=4, max_tool_rounds=2,
               tool_executor=lambda name, args: "新闻内容")
    events = list(qa.answer_stream("有何新闻？", tools=[{"type": "function"}]))
    done = events[-1]
    assert done["type"] == "done"
    # 相同工具和参数的第二次调用被去重，避免模型空转。
    assert done["tools_used"] == ["get_news_data"]
    # 共 3 次模型调用：2 轮工具 + 1 轮最终答案
    assert len(ai.calls) == 3


def test_answer_stream_emits_stages_and_web_sources_across_rounds(tmp_path, fake_embedder):
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("本地财报内容")])
    ai = FakeToolAI([
        [_tool_calls_event([{"id": "c1", "name": "web_search", "arguments": '{"query":"某公司公告"}'}])],
        [_tool_calls_event([{"id": "c2", "name": "get_news_data", "arguments": '{"symbol":"600519"}'}])],
        [_done_event("综合本地财报和网页来源后的回答")],
    ])

    def _execute(name, args):
        if name == "web_search":
            return '{"source":"web_search","results":[{"title":"公告","url":"https://example.com/a","content":"摘要","published_date":"2026-09-01"}]}'
        return '{"news":"最新公告"}'

    qa = RagQA(store, ai, top_k=4, max_tool_rounds=3, tool_executor=_execute)
    events = list(qa.answer_stream("请结合最新公告分析", tools=[{"type": "function"}]))

    stages = [event["stage"] for event in events if event["type"] == "reasoning_stage"]
    assert stages == ["assess", "retrieve", "review", "assess", "retrieve", "review", "assess", "answer"]
    done = events[-1]
    assert done["tools_used"] == ["web_search", "get_news_data"]
    assert done["web_sources"] == [{"title": "公告", "url": "https://example.com/a", "content": "摘要", "published_date": "2026-09-01"}]


def test_answer_stream_skips_duplicate_tool_call(tmp_path, fake_embedder):
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("内容D")])
    ai = FakeToolAI([
        [_tool_calls_event([{"id": "c1", "name": "web_search", "arguments": '{"query":"重复查询"}'}])],
        [_tool_calls_event([{"id": "c2", "name": "web_search", "arguments": '{"query":"重复查询"}'}])],
        [_done_event("最终答案")],
    ])
    calls = []
    qa = RagQA(store, ai, top_k=4, max_tool_rounds=2,
               tool_executor=lambda name, args: calls.append((name, args)) or '{"source":"web_search","results":[]}')

    events = list(qa.answer_stream("测试", tools=[{"type": "function"}]))

    assert calls == [("web_search", {"query": "重复查询"})]
    results = [event for event in events if event["type"] == "tool_result"]
    assert results[1]["ok"] is False
    assert "重复调用" in results[1]["summary"]


def test_answer_stream_tool_failure_not_in_tools_used(tmp_path, fake_embedder):
    """工具执行失败：不记入 tools_used（避免前端假徽章），tool_result 带 ok=False"""
    store = RagStore(str(tmp_path), fake_embedder)
    store.upsert([_chunk("内容C")])
    ai = FakeToolAI([
        [_tool_calls_event([{"id": "c9", "name": "get_realtime_quote", "arguments": "{}"}])],
        [_done_event("行情暂不可得")],
    ])

    def _fail(name, args):
        raise RuntimeError("MCP 超时")

    qa = RagQA(store, ai, top_k=4, tool_executor=_fail)
    events = list(qa.answer_stream("现价？", tools=[{"type": "function"}]))
    done = events[-1]
    assert done["tools_used"] == []
    result_evt = next(e for e in events if e["type"] == "tool_result")
    assert result_evt["ok"] is False
    assert "MCP 超时" in result_evt["summary"]


class PriorityFakeStore:
    """可控 fake：全库检索 vs 指定报告检索返回不同片段"""

    def __init__(self, all_hits, pri_hits):
        self.all_hits = all_hits
        self.pri_hits = pri_hits
        self.calls = []

    def query(self, text, top_k=8, where=None):
        self.calls.append({"top_k": top_k, "where": where})
        if where and where.get("report_id"):
            return [dict(h) for h in self.pri_hits]
        return [dict(h) for h in self.all_hits]


def _hit(doc_id, text, report_id):
    return {"id": doc_id, "text": text, "section": "财务报告", "page": 1,
            "distance": 0.5, "report_id": report_id, "source": "pdf"}


def test_answer_stream_priority_report_weights_first(tmp_path):
    """指定 priority_report_id 时，该报告片段优先排在上下文前面"""
    store = PriorityFakeStore(
        all_hits=[_hit("a", "全库片段A", "600900:2025-12-31:annual"),
                  _hit("b", "全库片段B", "600519:2025-12-31:annual")],
        pri_hits=[_hit("p1", "优先报告片段1", "600900:2025-12-31:annual"),
                  _hit("p2", "优先报告片段2", "600900:2025-12-31:annual")],
    )
    ai = FakeToolAI([[_done_event("结合片段回答")]])
    qa = RagQA(store, ai, top_k=3)
    events = list(qa.answer_stream(
        "营收如何？", priority_report_id="600900:2025-12-31:annual"
    ))
    assert events[-1]["type"] == "done"
    system = ai.calls[0]["system"]
    # 优先报告片段在编号 [1]/[2]，全库片段在后
    idx_p1 = system.index("优先报告片段1")
    idx_p2 = system.index("优先报告片段2")
    idx_a = system.index("全库片段A")
    assert idx_p1 < idx_p2 < idx_a, system
    # 检索调用：全库 top_k=3 + 优先报告 top_k>=3
    pri_call = [c for c in store.calls if c["where"] and c["where"].get("report_id")]
    assert pri_call and pri_call[0]["where"] == {"report_id": "600900:2025-12-31:annual"}


def test_answer_stream_without_priority_unchanged():
    """未指定 priority 时只做全库检索（现状）"""
    store = PriorityFakeStore(
        all_hits=[_hit("a", "全库片段A", "600900:2025-12-31:annual")],
        pri_hits=[],
    )
    ai = FakeToolAI([[_done_event("答案")]])
    qa = RagQA(store, ai, top_k=2)
    list(qa.answer_stream("营收如何？"))
    assert all(c["where"] is None for c in store.calls)


def test_build_report_id_preserves_report_periods():
    """build_report_id：年报、半年报、Q1 和 Q3 各自保留真实报告期。"""
    assert RagQA.build_report_id("600900", "2025-12-31") == "600900:2025-12-31:annual"
    assert RagQA.build_report_id("600900", "2025-06-30") == "600900:2025-06-30:semi_annual"
    assert RagQA.build_report_id("600900", "2025-03-31") == "600900:2025-03-31:quarterly"
    assert RagQA.build_report_id("600900", "2025-09-30") == "600900:2025-09-30:quarterly"
