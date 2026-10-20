import json

from webapp.chat_followups import (
    build_previous_turn_context, fallback_search_query, is_context_only_followup,
)
from webapp.chat_models import AnswerRun, CompanyRef, Fact, Scope


def _run_dict():
    run = AnswerRun(
        content="长江电力近五日收盘价采样显示上涨。",
        status="completed",
        scope=Scope.company_only("600900", "长江电力"),
        facts=(Fact(
            metric="price", value=29.38, unit="元/股", period="2026-10-12",
            period_kind="daily", entity_scope="security", company_code="600900",
            source_type="tool", evidence_ids=("source-1",), verification="reference",
            as_of="2026-10-12", source_category="market",
        ),),
        execution_plan={
            "source_mode": "external_market", "steps": [{"kind": "market_kline"}],
            "market_window": {"kind": "rolling_trading_days", "label": "近5交易日",
                              "start_date": None, "end_date": "2026-10-12", "trading_days": 5,
                              "timezone": "Asia/Shanghai"},
        },
        tool_artifacts=(),
        id="previous-run",
    )
    return run.to_dict()


def test_previous_turn_context_uses_latest_valid_run_and_excludes_internal_fields():
    messages = [
        {"role": "user", "content": "旧问题，不应进入上下文"},
        {"role": "assistant", "content": "旧回答", "run": _run_dict()},
        {"role": "user", "content": "分析长江电力近五个交易日走势"},
        {"role": "assistant", "content": "长江电力近五日收盘价采样显示上涨。", "run": _run_dict()},
    ]

    context = build_previous_turn_context(messages)

    assert context is not None
    assert context.question == "分析长江电力近五个交易日走势"
    assert context.answer == "长江电力近五日收盘价采样显示上涨。"
    assert context.run_id == "previous-run"
    assert context.market_window["label"] == "近5交易日"
    prompt = context.to_prompt_text()
    assert "600900" in prompt and "29.38" in prompt and "market_kline" in prompt
    assert "旧问题" not in prompt
    assert "reasoning" not in prompt.casefold()
    assert "raw_arguments" not in prompt


def test_previous_turn_context_is_bounded_and_ignores_missing_or_invalid_runs():
    long_answer = "回答" * 5000
    valid = {"role": "assistant", "content": long_answer, "run": _run_dict()}
    messages = [{"role": "user", "content": "问题"}, valid]
    context = build_previous_turn_context(messages)
    assert context is not None
    assert len(context.answer) <= 1000
    assert len(context.to_prompt_text()) <= 4000
    json.loads(context.to_prompt_text())

    dense_run = _run_dict()
    dense_run["facts"] = dense_run["facts"] * 12
    dense_run["tool_artifacts"] = [{
        "provider": "web", "tool_name": "web_search", "status": "success", "as_of": "2026-10-12",
        "arguments_summary": json.dumps({"query": "词" * 300}, ensure_ascii=False),
    }] * 8
    dense_context = build_previous_turn_context([
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": long_answer, "run": dense_run},
    ])
    assert dense_context is not None
    assert len(dense_context.to_prompt_text()) <= 4000
    json.loads(dense_context.to_prompt_text())

    assert build_previous_turn_context([{"role": "user", "content": "问题"}]) is None
    assert build_previous_turn_context([
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "答", "run": {"status": "unknown"}},
    ]) is None


def test_search_query_fallback_resolves_only_explicit_previous_subject_references():
    context = build_previous_turn_context([
        {"role": "user", "content": "分析长江电力近五个交易日走势"},
        {"role": "assistant", "content": "长江电力行情。", "run": _run_dict()},
    ])

    assert fallback_search_query("那上涨原因呢？", context) == "长江电力 那上涨原因呢？"
    assert fallback_search_query("查询比亚迪最新公告", context) == "查询比亚迪最新公告"
    assert fallback_search_query("这只股票相关公告", None) == "这只股票相关公告"


def test_only_previous_result_display_questions_take_context_only_path():
    context = build_previous_turn_context([
        {"role": "user", "content": "分析长江电力走势"},
        {"role": "assistant", "content": "答案", "run": _run_dict()},
    ])

    assert is_context_only_followup("这个为什么没画图？", context)
    assert is_context_only_followup("刚才那张图怎么没有显示", context)
    assert not is_context_only_followup("长江电力股价为什么涨了？", context)
    assert not is_context_only_followup("A 股今天为什么上涨？", context)
    assert not is_context_only_followup("这个为什么没画图？", None)
