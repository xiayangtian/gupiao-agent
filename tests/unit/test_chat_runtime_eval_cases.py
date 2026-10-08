import json
from pathlib import Path

import pytest

from webapp.chat_runtime_eval_cases import RuntimeSuite


def _valid_suite():
    return {
        "schema_version": 1,
        "suite_id": "p4-smoke-v1",
        "corpus_version": "fixture-v1",
        "cases": [{
            "id": "market-smoke",
            "category": "market",
            "turns": ["600519 今天行情如何？"],
            "scope": {"mode": "company_only", "companies": ["600519"]},
            "expected_intent": "market_data",
            "expected_status": "completed",
            "allowed_sources": ["tencent"],
            "expected_claims": [{
                "subject": "600519", "period": "2026-10-08", "unit": "CNY",
                "relation": "equals", "value": 10.5, "evidence_id": "quote:600519:2026-10-08",
            }],
            "forbidden_claims": [],
            "forbidden_report_ids": [],
            "provider_fixture": {"market": [{"symbol": "600519", "price": 10.5}]},
            "max_calls": {"market": 1},
        }],
    }


def test_runtime_suite_rejects_network_endpoint_in_fixture(tmp_path):
    raw = _valid_suite()
    raw["cases"][0]["provider_fixture"]["host"] = "api.example.org"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="provider_fixture"):
        RuntimeSuite.load(path)


def test_runtime_suite_rejects_unknown_case_fields(tmp_path):
    raw = _valid_suite()
    raw["cases"][0]["unexpected"] = "silently ignored fields make cases ambiguous"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected"):
        RuntimeSuite.load(path)


def test_runtime_suite_rejects_duplicate_ids_and_empty_claim_evidence(tmp_path):
    raw = _valid_suite()
    raw["cases"].append(dict(raw["cases"][0]))
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        RuntimeSuite.load(path)

    raw = _valid_suite()
    raw["cases"][0]["expected_claims"][0]["evidence_id"] = ""
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="evidence_id"):
        RuntimeSuite.load(path)


def test_repository_runtime_suite_is_versioned_and_offline():
    root = Path(__file__).resolve().parents[1]
    suite = RuntimeSuite.load(root / "fixtures/chat_runtime_eval_cases.json")
    assert suite.suite_id == "chat-runtime-offline-v1"
    assert {case.category for case in suite.cases} >= {
        "report", "market", "recap", "knowledge", "research", "followup", "failure", "recovery",
    }
    for case in suite.cases:
        assert all(not (isinstance(value, str) and ("http://" in value or "https://" in value))
                   for fixture in case.provider_fixture.values()
                   for value in (fixture if isinstance(fixture, tuple) else (fixture,)))


def test_runtime_suite_loads_immutable_cases(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(_valid_suite()), encoding="utf-8")

    suite = RuntimeSuite.load(path)

    assert suite.schema_version == 1
    assert suite.suite_id == "p4-smoke-v1"
    assert len(suite.cases) == 1
    assert suite.cases[0].id == "market-smoke"
    assert suite.cases[0].expected_claims[0].evidence_id == "quote:600519:2026-10-08"
    with pytest.raises((AttributeError, TypeError)):
        suite.cases[0].id = "mutated"
