"""Deterministic, query-centered evidence-window selection for retrieved passages."""
from __future__ import annotations

import re
from collections import Counter


_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%-]*|[\u3400-\u9fff]+")


def _query_terms(query: str) -> tuple[str, ...]:
    terms: set[str] = set()
    for match in _WORD_RE.finditer(str(query or "")):
        token = match.group(0).lower()
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            terms.add(token)
            for size in (4, 3, 2):
                if len(token) >= size:
                    terms.update(token[index:index + size] for index in range(len(token) - size + 1))
        elif len(token) >= 2:
            terms.add(token)
    return tuple(sorted(terms, key=lambda item: (-len(item), item)))


def diagnose_retrieval(gold_evidence_id: str, *, candidate_ids: tuple[str, ...],
                       ranked_ids: tuple[str, ...], windowed_ids: tuple[str, ...],
                       cited_ids: tuple[str, ...]) -> str:
    """Locate a retrieval failure stage using evidence identity at each pipeline boundary."""
    if gold_evidence_id not in candidate_ids:
        return "not_recalled"
    if gold_evidence_id not in ranked_ids:
        return "ranked_out"
    if gold_evidence_id not in windowed_ids:
        return "window_clipped"
    if gold_evidence_id not in cited_ids:
        return "citation_mismatch"
    return "supported"


def select_evidence_window(text: str, query: str, *, max_chars: int) -> str:
    """Return a bounded contiguous passage around query terms; preserve legacy prefix on miss."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if len(text) <= max_chars:
        return text

    terms = _query_terms(query)
    if not terms:
        return text[:max_chars]

    position_scores: Counter[int] = Counter()
    for term in terms:
        start = 0
        weight = len(term) ** 2
        while True:
            position = text.lower().find(term, start)
            if position < 0:
                break
            position_scores[position] += weight
            start = position + 1
    if not position_scores:
        return text[:max_chars]

    # Prefer a dense neighborhood of query terms, rather than the first incidental match.
    radius = max_chars // 2
    best_position = max(
        position_scores,
        key=lambda position: (
            sum(score for nearby, score in position_scores.items() if abs(nearby - position) <= radius),
            position_scores[position],
            -position,
        ),
    )
    start = max(0, best_position - max_chars // 3)
    end = min(len(text), start + max_chars)
    if end == len(text):
        start = max(0, end - max_chars)
    return text[start:end]
