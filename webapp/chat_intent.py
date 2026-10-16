"""Validated, provider-independent contract for model-led chat intent analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

ChatIntentCategory = Literal[
    "market_analysis",
    "financial_report",
    "financial_knowledge",
    "non_financial",
    "unclear",
]
ChatSubjectKind = Literal[
    "equity",
    "industry",
    "broad_market",
    "commodity",
    "overseas_market",
    "other",
    "none",
]
ChatTaskKind = Literal[
    "quote",
    "trend",
    "recap",
    "attribution",
    "report_fact",
    "report_trend",
    "research",
    "explain",
    "none",
]

_CATEGORIES = frozenset(
    {"market_analysis", "financial_report", "financial_knowledge", "non_financial", "unclear"}
)
_SUBJECT_KINDS = frozenset(
    {"equity", "industry", "broad_market", "commodity", "overseas_market", "other", "none"}
)
_TASK_KINDS = frozenset(
    {"quote", "trend", "recap", "attribution", "report_fact", "report_trend", "research", "explain", "none"}
)
_REQUIRED_KEYS = frozenset(
    {
        "category",
        "subject_kind",
        "subject_name",
        "stock_code",
        "task_kind",
        "time_expression",
        "needs_clarification",
        "clarification_question",
    }
)
_STOCK_CODE_RE = re.compile(r"^\d{6}$")
_MAX_TEXT_CHARS = 256


@dataclass(frozen=True)
class ChatIntentAnalysis:
    """Model intent output after strict schema and consistency validation."""

    category: ChatIntentCategory
    subject_kind: ChatSubjectKind
    subject_name: str
    stock_code: str
    task_kind: ChatTaskKind
    time_expression: str
    needs_clarification: bool
    clarification_question: str


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if len(value) > _MAX_TEXT_CHARS:
        raise ValueError(f"{key} is too long")
    return value


def parse_chat_intent_analysis(raw: Mapping[str, Any]) -> ChatIntentAnalysis:
    """Validate an untrusted model JSON mapping; never infer tool authorization."""
    if not isinstance(raw, Mapping):
        raise ValueError("chat intent analysis must be an object")
    keys = frozenset(raw.keys())
    if keys != _REQUIRED_KEYS:
        missing = _REQUIRED_KEYS - keys
        extra = keys - _REQUIRED_KEYS
        raise ValueError(f"invalid chat intent fields (missing={sorted(missing)}, extra={sorted(extra)})")

    category = raw["category"]
    subject_kind = raw["subject_kind"]
    task_kind = raw["task_kind"]
    if not isinstance(category, str) or category not in _CATEGORIES:
        raise ValueError("category is not supported")
    if not isinstance(subject_kind, str) or subject_kind not in _SUBJECT_KINDS:
        raise ValueError("subject_kind is not supported")
    if not isinstance(task_kind, str) or task_kind not in _TASK_KINDS:
        raise ValueError("task_kind is not supported")

    subject_name = _required_string(raw, "subject_name")
    stock_code = _required_string(raw, "stock_code")
    time_expression = _required_string(raw, "time_expression")
    clarification_question = _required_string(raw, "clarification_question")
    needs_clarification = raw["needs_clarification"]
    if not isinstance(needs_clarification, bool):
        raise ValueError("needs_clarification must be a boolean")
    if stock_code and not _STOCK_CODE_RE.fullmatch(stock_code):
        raise ValueError("stock_code must be empty or exactly six digits")

    if needs_clarification != bool(clarification_question.strip()):
        raise ValueError("clarification_question must be present only when clarification is required")
    if category == "unclear" and not needs_clarification:
        raise ValueError("unclear requests must require clarification")
    if category == "non_financial" and (
        stock_code or subject_kind != "none" or task_kind != "none"
    ):
        raise ValueError("non_financial requests must not carry financial entity or task fields")

    return ChatIntentAnalysis(
        category=category,  # type: ignore[arg-type]
        subject_kind=subject_kind,  # type: ignore[arg-type]
        subject_name=subject_name,
        stock_code=stock_code,
        task_kind=task_kind,  # type: ignore[arg-type]
        time_expression=time_expression,
        needs_clarification=needs_clarification,
        clarification_question=clarification_question,
    )
