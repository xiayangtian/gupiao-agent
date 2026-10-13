"""Bounded, session-local context for follow-up resolution and answers."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence

from webapp.chat_models import AnswerRun

_MAX_QUESTION_CHARS = 400
_MAX_ANSWER_CHARS = 1000
_MAX_FACTS = 12
_MAX_TOOLS = 8
_MAX_CONTEXT_CHARS = 4000


def _bounded_text(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _tool_query(tool: Mapping[str, Any]) -> str:
    raw = tool.get("arguments_summary")
    if not isinstance(raw, str):
        return ""
    try:
        arguments = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    query = arguments.get("query") if isinstance(arguments, Mapping) else None
    return _bounded_text(query, 300)


@dataclass(frozen=True)
class PreviousTurnContext:
    question: str
    answer: str
    run_id: str
    status: str
    source_mode: str
    steps: tuple[str, ...]
    companies: tuple[Mapping[str, str], ...]
    facts: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_user_question": self.question,
            "previous_assistant_answer": self.answer,
            "run_id": self.run_id,
            "run_status": self.status,
            "source_mode": self.source_mode,
            "steps": list(self.steps),
            "companies": [dict(company) for company in self.companies],
            "facts": [dict(fact) for fact in self.facts],
            "tools": [dict(tool) for tool in self.tools],
        }

    def to_prompt_text(self) -> str:
        data = self.to_dict()
        while True:
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if len(text) <= _MAX_CONTEXT_CHARS:
                return text
            reduced = False
            for key in ("tools", "facts", "steps", "companies"):
                if data[key]:
                    data[key].pop()
                    reduced = True
                    break
            if reduced:
                continue
            for key in ("previous_assistant_answer", "previous_user_question"):
                value = data[key]
                if len(value) > 80:
                    data[key] = value[:max(80, len(value) // 2)]
                    reduced = True
                    break
            if not reduced:
                return json.dumps({"run_id": self.run_id, "run_status": self.status}, ensure_ascii=False)

    @property
    def subject(self) -> str:
        if self.companies:
            company = self.companies[0]
            return _bounded_text(company.get("name") or company.get("code"), 80)
        return ""


def build_previous_turn_context(messages: Sequence[Mapping[str, Any]]) -> PreviousTurnContext | None:
    """Project only the last assistant run and its preceding user message; never trust raw run fields."""
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return None
    assistant_index = next((
        index for index in range(len(messages) - 1, -1, -1)
        if isinstance(messages[index], Mapping) and messages[index].get("role") == "assistant"
    ), None)
    if assistant_index is None:
        return None
    assistant = messages[assistant_index]
    run_raw = assistant.get("run")
    if not isinstance(run_raw, Mapping):
        return None
    try:
        run = AnswerRun.from_dict(run_raw)
    except (TypeError, ValueError):
        return None
    if run.status not in {"completed", "partial"}:
        return None
    user_message = next((
        messages[index] for index in range(assistant_index - 1, -1, -1)
        if isinstance(messages[index], Mapping)
        and messages[index].get("role") == "user"
        and isinstance(messages[index].get("content"), str)
        and messages[index].get("content", "").strip()
    ), None)
    if user_message is None:
        return None

    plan = run.execution_plan if isinstance(run.execution_plan, Mapping) else {}
    steps = tuple(
        _bounded_text(item.get("kind"), 60)
        for item in plan.get("steps", [])[:8]
        if isinstance(item, Mapping) and isinstance(item.get("kind"), str)
    ) if isinstance(plan.get("steps", []), (list, tuple)) else ()
    companies = tuple(
        {"code": _bounded_text(company.code, 16), "name": _bounded_text(company.name, 80)}
        for company in (run.scope.companies if run.scope else ())[:4]
    )
    facts = tuple({
        "metric": _bounded_text(fact.metric, 80), "value": fact.value,
        "unit": _bounded_text(fact.unit, 30), "period": _bounded_text(fact.period, 40),
        "period_kind": _bounded_text(fact.period_kind, 30),
        "entity_scope": _bounded_text(fact.entity_scope, 40),
        "company_code": _bounded_text(fact.company_code, 16),
        "source_category": _bounded_text(fact.source_category, 30),
    } for fact in run.facts[:_MAX_FACTS])
    tools = tuple({
        "provider": _bounded_text(tool.provider, 40),
        "tool_name": _bounded_text(tool.tool_name, 80),
        "status": _bounded_text(tool.status, 20),
        "as_of": _bounded_text(tool.as_of, 40),
        "query": _tool_query(tool.to_dict()),
    } for tool in run.tool_artifacts[:_MAX_TOOLS])
    return PreviousTurnContext(
        question=_bounded_text(user_message.get("content"), _MAX_QUESTION_CHARS),
        answer=_bounded_text(assistant.get("content") or run.content, _MAX_ANSWER_CHARS),
        run_id=_bounded_text(run.id, 80), status=run.status,
        source_mode=_bounded_text(plan.get("source_mode"), 50),
        steps=steps, companies=companies, facts=facts, tools=tools,
    )


def fallback_search_query(question: str, context: PreviousTurnContext | None) -> str:
    """Resolve a clear prior-subject reference if planner query generation is unavailable."""
    if not isinstance(question, str):
        return ""
    text = " ".join(question.split())
    references = ("这个", "这只", "这家公司", "刚才", "上轮", "上一轮", "那", "它", "其")
    if context is not None and context.subject and any(term in text for term in references):
        text = context.subject + " " + text
    return text[:300]


def is_context_only_followup(question: str, context: PreviousTurnContext | None) -> bool:
    """Recognize narrow prior-result display/provenance questions, not market causation."""
    if not isinstance(question, str) or context is None:
        return False
    text = "".join(question.casefold().split())
    references = ("这个", "这次", "这条", "刚才", "刚刚", "上轮", "上一轮", "上一个回答", "上面的", "前面")
    presentation = ("图", "曲线", "图表", "画图", "表格", "显示", "展示", "来源", "搜索词", "引用", "渲染")
    market_causation = ("为什么涨", "为什么跌", "为何上涨", "为何下跌", "涨了", "跌了", "上涨原因", "下跌原因")
    return (any(term in text for term in references)
            and any(term in text for term in presentation)
            and not any(term in text for term in market_causation))
