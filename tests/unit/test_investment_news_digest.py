from dataclasses import replace
import json

from tests.unit.investment_news_fixtures import FakeNewsSource, RecordingAIClient, make_item, make_service
from webapp.investment_news import InvestmentNewsService


def test_manual_digest_reads_all_day_items_in_batches_without_collecting(tmp_path):
    items = [replace(make_item(index), snippet="摘要" * 600) for index in range(30)]
    source = FakeNewsSource(market_items=items)
    ai = RecordingAIClient()
    service, store = make_service(tmp_path, source=source, ai_client=ai)
    service.collect_now("manual")
    source.calls.clear()

    result = service.generate_digest("2026-10-14", force=True)

    assert result["status"] == "succeeded"
    assert result["input_count"] == 30
    assert {item.id for item in items} <= ai.seen_item_ids
    point = result["market_themes"][0]
    assert point["citations"]
    assert point["citations"][0]["url"].startswith("https://")
    assert len(ai.calls) >= 3
    assert source.calls == []
    service.shutdown()


def test_monday_digest_uses_weekend_window_and_weekend_only_citations(tmp_path):
    class WeekendAwareAI:
        def __init__(self):
            self.calls = []

        def chat(self, messages, **kwargs):
            content = json.loads(messages[0]["content"])
            self.calls.append(content)
            ids = content.get("allowed_item_ids") or [item["id"] for item in content.get("items", [])]
            weekend_ids = content.get("allowed_weekend_item_ids", [])
            return {
                "content": json.dumps({
                    "market_themes": [{"text": "本周摘要", "item_ids": ids[:1]}] if ids else [],
                    "policy_regulatory": [],
                    "watchlist": [],
                    "risks": [],
                    "weekend_review": [{"text": "周末重点", "item_ids": weekend_ids}] if weekend_ids else [],
                }, ensure_ascii=False),
                "model": "weekend-test-model",
            }

    weekend_items = [
        make_item(921, day="2026-10-17"),
        make_item(922, day="2026-10-18"),
    ]
    monday_item = make_item(923, day="2026-10-19")
    tuesday_item = make_item(924, day="2026-10-20")
    ai = WeekendAwareAI()
    service, store = make_service(
        tmp_path,
        source=FakeNewsSource(market_items=[*weekend_items, monday_item, tuesday_item]),
        ai_client=ai,
    )
    service.collect_now("manual")

    monday = service.generate_digest("2026-10-19", force=True)
    monday_call = next(call for call in ai.calls if call["stage"] == "combine_daily_summary")

    assert monday["status"] == "succeeded"
    assert monday["input_count"] == 3
    assert monday["coverage_start_day"] == "2026-10-17"
    assert monday["coverage_end_day"] == "2026-10-19"
    assert monday["weekend_input_count"] == 2
    assert monday_call["allowed_weekend_item_ids"] == ["item-921", "item-922"]
    assert monday["weekend_review"][0]["item_ids"] == ["item-921", "item-922"]
    assert {citation["item_id"] for citation in monday["weekend_review"][0]["citations"]} == {
        "item-921", "item-922",
    }

    tuesday = service.generate_digest("2026-10-20", force=True)
    assert tuesday["status"] == "succeeded"
    assert tuesday["input_count"] == 1
    assert tuesday["coverage_start_day"] == "2026-10-20"
    assert tuesday["weekend_input_count"] == 0
    assert tuesday["weekend_review"] == []
    service.shutdown()


def test_monday_digest_generates_when_no_weekend_items_are_stored(tmp_path):
    monday_item = make_item(925, day="2026-10-19")
    service, store = make_service(
        tmp_path,
        source=FakeNewsSource(market_items=[monday_item]),
        ai_client=RecordingAIClient(),
    )
    service.collect_now("manual")

    result = service.generate_digest("2026-10-19", force=True)

    assert result["status"] == "succeeded"
    assert result["input_count"] == 1
    assert result["coverage_start_day"] == "2026-10-17"
    assert result["weekend_input_count"] == 0
    assert result["weekend_review"] == []
    service.shutdown()


def test_weekend_review_rejects_points_that_include_non_weekend_citations():
    sections, _ = InvestmentNewsService._validated_sections(
        {
            "market_themes": [{"text": "周一市场要点", "item_ids": ["monday"]}],
            "weekend_review": [{"text": "周末要点", "item_ids": ["saturday", "monday"]}],
        },
        {"saturday", "sunday", "monday"},
        set(),
        {"saturday", "sunday"},
    )

    assert sections["weekend_review"] == []
    assert sections["market_themes"][0]["item_ids"] == ["monday"]


def test_digest_retries_once_when_model_output_is_truncated(tmp_path):
    class TruncatedOnceAI(RecordingAIClient):
        def chat(self, messages, **kwargs):
            if not self.calls:
                self.calls.append({"messages": messages, "kwargs": kwargs})
                return {"content": '{"market_themes": [{"text": "cut off', "finish_reason": "length"}
            return super().chat(messages, **kwargs)

    ai = TruncatedOnceAI()
    service, store = make_service(
        tmp_path,
        source=FakeNewsSource(market_items=[make_item(1)]),
        ai_client=ai,
    )
    service.collect_now("manual")

    result = service.generate_digest("2026-10-14", force=True)

    assert result["status"] == "succeeded"
    assert len(ai.calls) == 3  # truncated batch, retried batch, then daily combine
    assert all(call["kwargs"]["max_tokens"] == 8192 for call in ai.calls)
    service.shutdown()


def test_digest_prompt_includes_watchlist_scope_and_symbols(tmp_path):
    market_item = make_item(101)
    stock_item = make_item(102, symbol="600900")
    source = FakeNewsSource(
        market_items=[market_item],
        stock_items_by_code={"600900": [stock_item]},
    )
    ai = RecordingAIClient()
    service, store = make_service(tmp_path, source=source, ai_client=ai)
    store.add_watchlist("600900", "长江电力")
    service.collect_now("manual")

    result = service.generate_digest("2026-10-14", force=True)

    prompt_items = [
        item
        for call in ai.calls
        for item in json.loads(call["messages"][0]["content"]).get("items", [])
    ]
    assert result["status"] == "succeeded"
    assert any(item["kinds"] == ["watchlist"] and item["symbols"] == ["600900"] for item in prompt_items)
    assert "不得把转载或同题报道声称为独立佐证" in ai.calls[0]["kwargs"]["system"]
    service.shutdown()


def test_market_items_cannot_be_classified_as_watchlist_highlights(tmp_path):
    market_item = make_item(801)

    class MisclassifyingAI:
        def chat(self, messages, **kwargs):
            content = json.loads(messages[-1]["content"])
            ids = content.get("allowed_item_ids") or [item["id"] for item in content.get("items", [])]
            return {
                "content": json.dumps({
                    "market_themes": [{"text": "市场快讯主题", "item_ids": ids[:1]}] if ids else [],
                    "policy_regulatory": [],
                    "watchlist": [{"text": "宁德时代（误分类）", "item_ids": ids[:1]}] if ids else [],
                    "risks": [],
                }, ensure_ascii=False),
                "model": "misclassification-test",
            }

    service, store = make_service(
        tmp_path,
        source=FakeNewsSource(market_items=[market_item]),
        ai_client=MisclassifyingAI(),
    )
    store.add_watchlist("600900", "长江电力")
    service.collect_now("manual")

    result = service.generate_digest("2026-10-14", force=True)

    assert result["status"] == "succeeded"
    assert result["watchlist"] == []
    assert result["market_themes"][0]["text"] == "市场快讯主题"
    service.shutdown()


def test_automatic_digest_skips_unchanged_input_but_manual_force_regenerates(tmp_path):
    items = [make_item(index) for index in range(3)]
    service, store = make_service(tmp_path, source=FakeNewsSource(market_items=items))
    service.collect_now("manual")

    first = service.generate_digest("2026-10-14", force=True)
    calls_after_first = len(service.ai_client.calls)
    automatic = service.generate_digest("2026-10-14", force=False)
    calls_after_skip = len(service.ai_client.calls)
    manual = service.generate_digest("2026-10-14", force=True)

    assert first["status"] == "succeeded"
    assert automatic["status"] == "skipped"
    assert calls_after_skip == calls_after_first
    assert manual["status"] == "succeeded"
    assert len(service.ai_client.calls) > calls_after_skip
    service.shutdown()


def test_invalid_citations_fail_generation_and_keep_previous_digest(tmp_path):
    service, store = make_service(
        tmp_path,
        source=FakeNewsSource(market_items=[make_item(1)]),
        ai_client=RecordingAIClient(),
    )
    service.collect_now("manual")
    previous = service.generate_digest("2026-10-14", force=True)
    stored_before = store.get_digest("2026-10-14")

    class InvalidCitationAI:
        def chat(self, messages, **kwargs):
            import json
            return {
                "content": json.dumps({
                    "market_themes": [{"text": "无依据结论", "item_ids": ["outside-item"]}],
                    "policy_regulatory": [], "watchlist": [], "risks": [],
                }),
                "model": "invalid-test-model",
            }

    service.ai_client = InvalidCitationAI()
    failed = service.generate_digest("2026-10-14", force=True)

    assert previous["status"] == "succeeded"
    assert failed["status"] == "failed"
    assert store.get_digest("2026-10-14")["payload"] == stored_before["payload"]
    service.shutdown()
