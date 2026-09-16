import json
from pathlib import Path


CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "chat_policy_eval_cases.json").read_text())
REQUIRED = {"question", "scope", "intent", "allowed_sources", "expected_fact_ids", "forbidden_report_ids", "expected_status"}


def test_m2_policy_evaluation_cases_are_complete_and_fail_closed():
    assert len(CASES) >= 8
    intents = {case["intent"] for case in CASES}
    assert {"report_fact", "company_trend", "industry_benchmark", "realtime_market", "event_attribution"} <= intents
    for case in CASES:
        assert REQUIRED <= set(case)
        assert case["question"] and case["scope"] and case["expected_status"]
        assert isinstance(case["allowed_sources"], list)
        assert case["expected_fact_ids"] or case.get("no_fact_outcome") is True
