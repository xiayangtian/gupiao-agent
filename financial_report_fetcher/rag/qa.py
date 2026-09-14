"""RagQA — RAG 问答编排：检索 → 拼装上下文 → LLM 生成 → 引用校验。

引用规则：system prompt 要求模型用 [n] 标注引用；回答后只保留
引用编号确实落在检索片段范围内的 citations，杜绝编造出处。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from financial_report_fetcher.report_identity import build_report_id
from webapp.chat_models import Scope

from .reranker import Reranker, _maybe_rerank
from .store import RagStore

logger = logging.getLogger(__name__)

CITE_RE = re.compile(r"\[(\d+)\]")

SYSTEM_PROMPT_TEMPLATE = """你是一位专业的金融分析师，基于检索到的财报片段回答用户问题。
规则：
1. 只能使用下方提供的片段作答，引用时用 [n] 标注（n 为片段编号）。
2. 片段信息不足时明确回答"检索内容中未找到相关信息"，不得编造。
3. 涉及数字时保持与片段一致，可补充说明数据来源（公司、年份、章节）。
4. 回答使用简体中文，结构清晰简洁。
5. 若提供工具，先判断现有证据能否可靠回答；仅在缺少必要的实时、外部或结构化信息时调用最少的工具。涉及今日、近期、最新、公告、新闻或股价涨跌原因时，优先用 web_search；财报数字以本地片段为准。工具结果返回后重新核验，避免重复相同查询，网页内容仅作为补充并明确标示来源。

检索片段：
{context}"""

RETRIEVAL_FALLBACK_PROMPT = """你是一位专业的金融分析师。当前本地财报检索服务暂时不可用，
因此没有可核验的财报原文上下文。请根据通用知识和可用工具结果回答；涉及今日、近期、最新、公告、新闻或股价涨跌原因时优先使用 web_search；若问题依赖具体财报数据，
必须明确说明暂时无法从本地财报核验，不得编造数字、出处或引用。回答使用简体中文。"""

EMPTY_RETRIEVAL_TOOL_PROMPT = """你是一位专业的金融分析师。本地知识库没有检索到相关财报片段，
但你可以调用提供的 MCP 工具查询实时行情、财务指标和公司基本面。涉及今日、近期、最新、公告、新闻或股价涨跌原因时优先使用网页搜索补充公开信息；先判断是否需要补充信息；没有工具数据支撑时明确说明无法核验，
不得编造 PDF 引用、具体数字或出处。回答使用简体中文。"""


@dataclass
class Citation:
    report_id: str
    source: str
    section: str
    page: Optional[int]
    snippet: str


class RagQA:
    def __init__(
        self,
        store: RagStore,
        ai_client,
        top_k: int = 8,
        tool_executor: Optional[Callable[[str, Dict[str, Any]], str]] = None,
        max_tool_rounds: int = 3,
        max_tool_calls: int = 10,
        tool_result_max_chars: int = 2000,
        reranker: Optional[Reranker] = None,
        rerank_candidates: int = 30,
        rerank_score_threshold: float = 0.5,
        rerank_margin_threshold: float = 0.05,
    ) -> None:
        """tool_executor: (name, arguments) -> str，用于执行 MCP 等外部工具；
        None 表示不启用工具调用（纯 RAG 路径）。
        reranker: 注入后检索放宽到 rerank_candidates 并按质量自适应精排；
        None 保持纯向量检索现状。"""
        self.store = store
        self.ai_client = ai_client
        self.top_k = top_k
        self.tool_executor = tool_executor
        self.max_tool_rounds = max_tool_rounds
        self.max_tool_calls = max(1, max_tool_calls)
        self.tool_result_max_chars = tool_result_max_chars
        self.reranker = reranker
        self.rerank_candidates = rerank_candidates
        self.rerank_score_threshold = rerank_score_threshold
        self.rerank_margin_threshold = rerank_margin_threshold

    def answer(
        self,
        question: str,
        history: Optional[List[Dict[str, str]]] = None,
        filters: Optional[Dict[str, Any]] = None,
        priority_report_id: Optional[str] = None,
        scope: Optional[Scope] = None,
    ) -> Optional[Dict[str, Any]]:
        """检索并回答；检索为空返回 None（调用方决定兜底）

        scope: company_only/company_industry 时按 report_ids 硬过滤，
        whole_corpus 不做过滤；priority_report_id 仅在 Scope 允许范围内生效。
        """
        try:
            hits = self._query_with_priority(question, scope, priority_report_id, filters)
        except Exception as exc:  # embedding 模型未就绪/网络不可达时降级直答
            logger.exception("RAG 检索失败，降级为无检索回答：%s", exc)
            messages = list(history or [])
            messages.append({"role": "user", "content": question})
            resp = self.ai_client.chat(messages=messages, system=RETRIEVAL_FALLBACK_PROMPT)
            return {"answer": resp["content"], "citations": [], "retrieval_report_ids": [], "retrieval_degraded": True}
        if not hits:
            return None

        system = SYSTEM_PROMPT_TEMPLATE.format(context="\n".join(self._context_lines(hits)))

        messages: List[Dict[str, str]] = []
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": question})

        resp = self.ai_client.chat(messages=messages, system=system)
        answer_text = resp["content"]
        citations = self._build_citations(hits, answer_text)
        return {
            "answer": answer_text,
            "citations": citations,
            "retrieval_report_ids": self._retrieval_report_ids(hits),
        }

    @staticmethod
    def build_report_id(code: str, period_iso: str) -> str:
        """委托统一身份模块推导与入库一致的 report_id。"""
        return build_report_id(code, period_iso)

    def _query_with_priority(
        self,
        question: str,
        scope: Optional[Scope],
        priority_report_id: Optional[str],
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """检索 + Scope 硬过滤 + 聚焦报告加权 + 自适应 rerank（可选）统一入口。

        - 候选数：注入 reranker 时放宽到 rerank_candidates（供精排），否则 top_k；
        - Scope 为 company_only/company_industry 时，主查询与 priority 查询都使用
          同一 ``$in`` 硬过滤，绝不越界；whole_corpus 传 None；
        - priority_report_id 仅在 Scope 允许范围内生效，否则忽略并记录 debug 日志；
        - 最后走 _maybe_rerank（无 reranker 时直接取前 top_k，保持优先级）。
        """
        query_top_k = self.rerank_candidates if self.reranker else self.top_k
        where = self._resolve_where(scope, filters)
        hits = self.store.query(question, top_k=query_top_k, where=where)
        if priority_report_id and self._priority_allowed(scope, priority_report_id):
            pri_where = self._priority_where(scope, priority_report_id)
            try:
                pri = self.store.query(
                    question,
                    top_k=max(3, self.top_k // 2),
                    where=pri_where,
                )
            except Exception:
                pri = []
            if pri:
                seen = {h["id"] for h in pri}
                hits = list(pri) + [h for h in hits if h["id"] not in seen]
        elif priority_report_id:
            logger.debug("priority_report_id=%s 超出当前 Scope，已忽略", priority_report_id)
        return _maybe_rerank(
            question, hits, self.reranker, self.top_k,
            self.rerank_score_threshold, self.rerank_margin_threshold,
        )

    @staticmethod
    def _resolve_where(
        scope: Optional[Scope],
        filters: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """把 Scope 解析为 Chroma where 硬过滤；无 Scope 时沿用旧 filters。"""
        if scope is None:
            return filters
        if scope.mode == "whole_corpus":
            return None
        return {"report_id": {"$in": list(scope.report_ids)}}

    @staticmethod
    def _priority_allowed(scope: Optional[Scope], priority_report_id: str) -> bool:
        """priority_report_id 是否在当前 Scope 允许范围内。

        Scope 为 None 或 whole_corpus 时无边界，允许全局聚焦；
        company_only/company_industry 时必须在 scope.report_ids 中。
        """
        if scope is None or scope.mode == "whole_corpus":
            return True
        return priority_report_id in scope.report_ids

    @staticmethod
    def _priority_where(
        scope: Optional[Scope],
        priority_report_id: str,
    ) -> Dict[str, Any]:
        """聚焦报告查询的 where；硬 Scope 下用单元素 $in 保持同一过滤语义。"""
        if scope is not None and scope.mode != "whole_corpus":
            return {"report_id": {"$in": [priority_report_id]}}
        return {"report_id": priority_report_id}

    def _context_lines(self, hits: List[Dict[str, Any]]) -> List[str]:
        """构建受控上下文行：只暴露报告身份、章节、页码与来源摘要，绝不暴露内部距离分数。"""
        lines: List[str] = []
        for i, h in enumerate(hits, start=1):
            rid = str(h.get("report_id") or "?")
            company, period, kind = self._split_report_id(rid)
            where = f"{rid}「{h.get('section', '?')}」"
            if h.get("page"):
                where += f" 第{h['page']}页"
            summary = f"公司:{company} 期次:{period} 来源类型:{h.get('source', '?')}/{kind}"
            lines.append(f"[{i}] {where}（{summary}）：{h['text'][:300]}")
        return lines

    @staticmethod
    def _split_report_id(report_id: str) -> tuple[str, str, str]:
        """按 ``code:period:type`` 报告身份前缀约定拆分公司、期次、报告类型。"""
        parts = report_id.split(":")
        company = parts[0] if len(parts) > 0 and parts[0] else "?"
        period = parts[1] if len(parts) > 1 and parts[1] else "?"
        kind = parts[2] if len(parts) > 2 and parts[2] else "?"
        return company, period, kind

    @staticmethod
    def _retrieval_report_ids(hits: List[Dict[str, Any]]) -> List[str]:
        """按命中顺序去重返回报告身份列表，供 AnswerRun.retrieval_report_ids 使用。"""
        seen: List[str] = []
        for h in hits:
            rid = h.get("report_id")
            if rid and rid not in seen:
                seen.append(rid)
        return seen

    @staticmethod
    def _build_citations(hits: List[Dict[str, Any]], answer_text: str) -> List[Dict[str, Any]]:
        """校验答案中的 [n] 引用并去重，返回 dict 列表（兼容引用卡片渲染）"""
        citations: List[Citation] = []
        for num_str in CITE_RE.findall(answer_text):
            idx = int(num_str)
            if 1 <= idx <= len(hits):
                h = hits[idx - 1]
                citations.append(Citation(
                    report_id=h.get("report_id", ""),
                    source=h.get("source", ""),
                    section=h.get("section", ""),
                    page=h.get("page"),
                    snippet=h["text"][:200],
                ))
        seen = set()
        unique = []
        for c in citations:
            key = (c.report_id, c.section, c.page, c.snippet[:50])
            if key not in seen:
                seen.add(key)
                unique.append(c)
        return [c.__dict__ for c in unique]

    @staticmethod
    def _parse_args(raw: str) -> Dict[str, Any]:
        """解析工具参数 JSON 字符串；非法时返回空 dict"""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _web_sources(result: str) -> List[Dict[str, str]]:
        """从网页搜索工具的受控 JSON 中提取可展示来源。"""
        try:
            data = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(data, dict) or data.get("source") != "web_search":
            return []
        sources = []
        for row in data.get("results") or []:
            if not isinstance(row, dict) or not row.get("url"):
                continue
            sources.append({
                "title": str(row.get("title") or row["url"]),
                "url": str(row["url"]),
                "content": str(row.get("content") or "")[:300],
                "published_date": str(row.get("published_date") or ""),
            })
        return sources

    def answer_stream(
        self,
        question: str,
        history: Optional[List[Dict[str, str]]] = None,
        filters: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        priority_report_id: Optional[str] = None,
        scope: Optional[Scope] = None,
    ):
        """流式检索回答，可选工具调用编排。事件：

            {"type": "empty"}                                  # 检索为空
            {"type": "delta", "text", "reasoning"}             # 模型内容/推理增量
            {"type": "tool_call", "name", "arguments"}         # 开始调用工具
            {"type": "tool_result", "name", "summary"}         # 工具返回摘要
            {"type": "done", "answer", "reasoning",
             "citations", "model", "usage", "tools_used",
             "retrieval_report_ids"}                           # 完成
            {"type": "error", "error"}                         # 出错

        工具编排：首轮 LLM 带 tools；若模型请求工具则执行（tool_executor）并把
        assistant(tool_calls) + tool(结果) 追加到消息，最多 max_tool_rounds 轮，
        之后强制生成最终答案。未注入 tool_executor 或未传 tools 时走纯 RAG 路径。
        """
        retrieval_degraded = False
        try:
            hits = self._query_with_priority(question, scope, priority_report_id, filters)
        except Exception as exc:  # 首次 embedding 下载失败时不让整条流式问答中断
            logger.exception("RAG 检索失败，降级为无检索流式回答：%s", exc)
            hits = []
            retrieval_degraded = True
        retrieval_report_ids = self._retrieval_report_ids(hits)
        can_use_tools = bool(tools and self.tool_executor is not None)
        if not hits and not retrieval_degraded and not can_use_tools:
            yield {"type": "empty"}
            return

        if retrieval_degraded:
            system = RETRIEVAL_FALLBACK_PROMPT
        elif not hits:
            system = EMPTY_RETRIEVAL_TOOL_PROMPT
        else:
            system = SYSTEM_PROMPT_TEMPLATE.format(context="\n".join(self._context_lines(hits)))

        messages: List[Dict[str, str]] = []
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": question})

        # 未启用工具：现状路径
        if not tools or self.tool_executor is None:
            for evt in self.ai_client.chat_stream(messages=messages, system=system):
                if evt["type"] == "delta":
                    yield evt
                elif evt["type"] == "error":
                    yield evt
                    return
                elif evt["type"] == "done":
                    answer_text = evt.get("answer") or ""
                    yield {
                        "type": "done",
                        "answer": answer_text,
                        "reasoning": evt.get("reasoning") or "",
                        "citations": self._build_citations(hits, answer_text),
                        "model": evt.get("model"),
                        "usage": evt.get("usage") or {},
                        "tools_used": [],
                        "retrieval_report_ids": retrieval_report_ids,
                        "retrieval_degraded": retrieval_degraded,
                    }
                    return

        # 工具编排路径
        tools_used: List[str] = []
        web_sources: List[Dict[str, str]] = []
        seen_tool_calls = set()
        total_tool_calls = 0
        round_no = 0
        while True:
            round_no += 1
            yield {"type": "reasoning_stage", "stage": "assess", "round": round_no,
                   "message": "正在判断现有证据是否足够…"}
            # 每轮都保留工具定义，允许模型在参数校验失败后修正并重试；达到
            # max_tool_rounds 后才进入下方无工具的最终总结调用。
            use_tools = tools
            got_tool_calls = False
            for evt in self.ai_client.chat_stream(messages=messages, system=system, tools=use_tools):
                if evt["type"] == "delta":
                    yield evt
                elif evt["type"] == "error":
                    yield evt
                    return
                elif evt["type"] == "tool_calls":
                    got_tool_calls = True
                    calls = evt["tool_calls"]
                    # assistant tool_calls 消息（OpenAI 格式）
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": c["id"], "type": "function",
                             "function": {"name": c["name"], "arguments": c["arguments"]}}
                            for c in calls
                        ],
                    })
                    for c in calls:
                        name = c["name"]
                        args = self._parse_args(c.get("arguments"))
                        identity = f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
                        yield {"type": "reasoning_stage", "stage": "retrieve", "round": round_no,
                               "message": f"正在补充信息：调用 {name}…"}
                        yield {"type": "tool_call", "name": name, "arguments": args}
                        if identity in seen_tool_calls:
                            result = "工具调用失败：检测到重复调用，已使用此前结果，请基于已有信息继续回答"
                            ok = False
                        elif total_tool_calls >= self.max_tool_calls:
                            result = (
                                f"工具调用失败：本次问答已达到工具调用上限（{self.max_tool_calls} 次）；"
                                "重新发送问题会重置，请基于已有信息回答"
                            )
                            ok = False
                        else:
                            seen_tool_calls.add(identity)
                            total_tool_calls += 1
                            try:
                                result = self.tool_executor(name, args)
                                ok = not result.startswith((
                                    "工具调用失败", "MCP 服务暂不可用",
                                    "无法解析股票", "未获取到",
                                ))
                            except Exception as exc:
                                result = f"工具调用失败：{exc}"
                                ok = False
                        if ok:
                            tools_used.append(name)  # 仅成功执行的工具计入（避免假徽章）
                            if name == "web_search":
                                web_sources.extend(self._web_sources(result))
                        yield {"type": "tool_result", "name": name, "summary": result[:200], "ok": ok}
                        yield {"type": "reasoning_stage", "stage": "review", "round": round_no,
                               "message": "已获取补充信息，正在核验并决定是否还需查询…"}
                        messages.append({
                            "role": "tool",
                            "tool_call_id": c["id"],
                            "content": result[: self.tool_result_max_chars],
                        })
                elif evt["type"] == "done":
                    answer_text = evt.get("answer") or ""
                    yield {"type": "reasoning_stage", "stage": "answer", "round": round_no,
                           "message": "正在基于已核验的信息组织回答…"}
                    yield {
                        "type": "done",
                        "answer": answer_text,
                        "reasoning": evt.get("reasoning") or "",
                        "citations": self._build_citations(hits, answer_text),
                        "model": evt.get("model"),
                        "usage": evt.get("usage") or {},
                        "tools_used": tools_used,
                        "web_sources": web_sources,
                        "retrieval_report_ids": retrieval_report_ids,
                        "retrieval_degraded": retrieval_degraded,
                    }
                    return
            if got_tool_calls and round_no < self.max_tool_rounds:
                continue
            break

        # 达到工具轮数上限：最后用已有上下文生成最终答案（不带工具）
        yield {"type": "reasoning_stage", "stage": "answer", "round": round_no,
               "message": "正在基于已核验的信息组织回答…"}
        for evt in self.ai_client.chat_stream(messages=messages, system=system):
            if evt["type"] == "delta":
                yield evt
            elif evt["type"] == "error":
                yield evt
                return
            elif evt["type"] == "done":
                answer_text = evt.get("answer") or ""
                yield {
                    "type": "done",
                    "answer": answer_text,
                    "reasoning": evt.get("reasoning") or "",
                    "citations": self._build_citations(hits, answer_text),
                    "model": evt.get("model"),
                    "usage": evt.get("usage") or {},
                    "tools_used": tools_used,
                    "web_sources": web_sources,
                    "retrieval_report_ids": retrieval_report_ids,
                    "retrieval_degraded": retrieval_degraded,
                }
                return

    def try_answer_report(
        self,
        code: str,
        period_iso: str,
        question: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """单报告 RAG 问答：按 report_id 过滤检索；无结果返回 None"""
        report_id = self.build_report_id(code, period_iso)
        return self.answer(question, history=history, filters={"report_id": report_id})
