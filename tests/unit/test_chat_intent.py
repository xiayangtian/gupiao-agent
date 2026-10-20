import json

import pytest

from webapp.chat_intent import ChatIntentAnalysis, parse_chat_intent_analysis
from webapp.chat_intent_analyzer import ChatIntentAnalysisError, ChatIntentAnalyzer


BASE = {
    "category": "market_analysis",
    "subject_kind": "broad_market",
    "subject_name": "A股",
    "stock_code": "",
    "task_kind": "recap",
    "time_expression": "今天",
    "needs_clarification": False,
    "clarification_question": "",
}


@pytest.mark.parametrize(
    ("category", "subject_kind", "task_kind"),
    [
        ("market_analysis", "equity", "quote"),
        ("market_analysis", "industry", "trend"),
        ("market_analysis", "broad_market", "recap"),
        ("market_analysis", "commodity", "trend"),
        ("market_analysis", "overseas_market", "recap"),
        ("financial_report", "equity", "report_trend"),
        ("financial_knowledge", "none", "explain"),
        ("financial_knowledge", "other", "research"),
        ("non_financial", "none", "none"),
    ],
)
def test_parse_accepts_supported_chat_intent_categories(category, subject_kind, task_kind):
    raw = {
        **BASE,
        "category": category,
        "subject_kind": subject_kind,
        "task_kind": task_kind,
    }
    if category == "non_financial":
        raw.update({"subject_name": "", "time_expression": ""})

    result = parse_chat_intent_analysis(raw)

    assert isinstance(result, ChatIntentAnalysis)
    assert result.category == category
    assert result.subject_kind == subject_kind
    assert result.task_kind == task_kind


def test_parse_preserves_market_entities_for_lowercase_a_share_gold_and_us_market():
    cases = [
        ("分析今日a股行情", "broad_market", "A股"),
        ("分析黄金近期走势", "commodity", "黄金"),
        ("看看美股今天表现", "overseas_market", "美股"),
    ]

    for question, subject_kind, subject_name in cases:
        result = parse_chat_intent_analysis({
            **BASE,
            "subject_kind": subject_kind,
            "subject_name": subject_name,
        })
        assert question
        assert result.category == "market_analysis"
        assert result.subject_kind == subject_kind
        assert result.subject_name == subject_name


def test_parse_requires_six_digit_code_or_empty_string():
    for code in ("000001", "600900", ""):
        result = parse_chat_intent_analysis({**BASE, "stock_code": code})
        assert result.stock_code == code

    for code in ("1", "1234567", "600900.SH", 600900, None):
        with pytest.raises(ValueError):
            parse_chat_intent_analysis({**BASE, "stock_code": code})


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown": "extra"},
        {"category": "stock_quote"},
        {"subject_kind": "crypto_exchange"},
        {"task_kind": "buy"},
        {"subject_name": "x" * 257},
        {"time_expression": "x" * 257},
        {"needs_clarification": "false"},
        {"clarification_question": 1},
        {"category": "non_financial", "stock_code": "600900"},
    ],
)
def test_parse_rejects_invalid_or_contradictory_payloads(changes):
    with pytest.raises(ValueError):
        parse_chat_intent_analysis({**BASE, **changes})


def test_unclear_request_requires_a_clarification_question():
    result = parse_chat_intent_analysis({
        **BASE,
        "category": "unclear",
        "subject_kind": "none",
        "subject_name": "",
        "task_kind": "none",
        "needs_clarification": True,
        "clarification_question": "你想了解哪方面？",
    })

    assert result.needs_clarification is True
    assert result.clarification_question == "你想了解哪方面？"

    with pytest.raises(ValueError):
        parse_chat_intent_analysis({
            **BASE,
            "category": "unclear",
            "subject_kind": "none",
            "subject_name": "",
            "task_kind": "none",
            "needs_clarification": True,
            "clarification_question": "",
        })


def test_analyzer_uses_bounded_context_and_parses_structured_model_json():
    calls = []
    payload = {**BASE, "subject_name": "A股"}

    def json_call(system_prompt, user_prompt):
        calls.append((system_prompt, user_prompt))
        return json.dumps(payload, ensure_ascii=False)

    result = ChatIntentAnalyzer(json_call).analyze("分析今日a股行情", context="最近一轮: 用户问大盘")

    assert result.category == "market_analysis"
    assert len(calls) == 1
    system_prompt, user_prompt = calls[0]
    assert "JSON" in system_prompt
    assert "思维链" in system_prompt
    assert "分析今日a股行情" in user_prompt
    assert "最近一轮: 用户问大盘" in user_prompt
    assert "缺少市场时默认 A 股" in system_prompt
    assert "缺少时间范围时默认今日" in system_prompt
    assert "待澄清槽位" in system_prompt


def test_analyzer_rejects_invalid_json_and_invalid_schema_without_fallback():
    for response in ("not json", json.dumps({**BASE, "category": "stock_quote"})):
        calls = []

        def json_call(system_prompt, user_prompt):
            calls.append(user_prompt)
            return response

        with pytest.raises(ChatIntentAnalysisError):
            ChatIntentAnalyzer(json_call).analyze("分析今日a股行情")
        assert len(calls) == 1


def test_analyzer_normalizes_model_call_errors_without_exposing_raw_exception():
    def json_call(system_prompt, user_prompt):
        raise RuntimeError("provider secret detail")

    with pytest.raises(ChatIntentAnalysisError) as error:
        ChatIntentAnalyzer(json_call).analyze("分析今日a股行情")

    assert "provider secret detail" not in str(error.value)


def test_analyzer_rejects_empty_question_and_bounds_context():
    captured = []

    def json_call(system_prompt, user_prompt):
        captured.append(user_prompt)
        return json.dumps({**BASE, "subject_name": "A股"}, ensure_ascii=False)

    with pytest.raises(ChatIntentAnalysisError):
        ChatIntentAnalyzer(json_call).analyze("  ")
    assert not captured

    ChatIntentAnalyzer(json_call).analyze("分析行情", context="x" * 20_000)
    assert len(captured[0]) < 10_000
