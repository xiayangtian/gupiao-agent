from pathlib import Path

import pytest

from webapp.chat_runtime_eval_cases import RuntimeSuite
from webapp.chat_runtime_eval_runner import (
    OfflineChatHarness,
    ProviderFixture,
    _network_guard,
    _parse_sse,
    run_case,
)

SUITE = Path(__file__).resolve().parents[1] / "fixtures/chat_runtime_eval_cases.json"


def _case(case_id: str):
    return next(case for case in RuntimeSuite.load(SUITE).cases if case.id == case_id)


@pytest.mark.parametrize("case_id, expected_status", [
    ("general-knowledge", "completed"),
    ("report-number-after-300", "completed"),
    ("market-kline", "completed"),
    ("market-recap-window", "completed"),
    ("explicit-research", "completed"),
    ("followup-scope", "completed"),
    ("source-failure", "partial"),
    ("research-recovery", "completed"),
])
def test_offline_harness_runs_real_chat_sse(tmp_path, case_id, expected_status):
    from webapp import server

    case = _case(case_id)
    harness = OfflineChatHarness(case, workspace=tmp_path / case_id)

    observation = harness.run(server.app)

    assert observation.events[:3] == ("session", "run_started", "reasoning_stage")
    assert observation.persisted_status == expected_status
    assert observation.session_id and observation.run_id
    assert harness.fixture.calls, f"{case_id} must exercise at least one frozen provider"
    assert observation.first_frame_seconds is None


def test_report_case_never_reaches_cross_company_evidence(tmp_path):
    from webapp import server

    case = _case("report-number-after-300")
    harness = OfflineChatHarness(case, workspace=tmp_path / case.id)

    observation = harness.run(server.app)

    artifacts = observation.terminal.get("run", {}).get("artifacts", [])
    assert artifacts, "report case must persist evidence artifacts"
    assert all(artifact.get("report_id") == "601288:2026-06-30:semi_annual" for artifact in artifacts)
    assert all("600900" not in str(artifact.get("report_id")) for artifact in artifacts)


def test_sse_parser_preserves_order_and_rejects_invalid_payload():
    parsed = _parse_sse('event: session\ndata: {"session_id":"s1"}\n\nevent: done\ndata: {"answer":"ok"}\n\n')
    assert [name for name, _ in parsed] == ["session", "done"]
    with pytest.raises(ValueError, match="invalid SSE payload"):
        _parse_sse('event: done\ndata: not-json\n\n')


def test_network_guard_blocks_tcp_transport():
    from contextlib import ExitStack

    import requests

    with ExitStack() as stack:
        _network_guard(stack)
        with pytest.raises(AssertionError, match="outbound network"):
            requests.get("http://127.0.0.1:1/")


def test_provider_fixture_enforces_declared_call_limits():
    fixture = ProviderFixture(version="fixed-v1", case_id="x", limits={"model": 1})
    fixture.record_attempt("model")
    with pytest.raises(AssertionError, match="call limit exceeded"):
        fixture.record_attempt("model")


def test_cli_help_and_missing_manifest_do_not_require_the_server(tmp_path, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_chat_runtime_evaluation",
        Path(__file__).resolve().parents[2] / "scripts/run_chat_runtime_evaluation.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(SystemExit) as excinfo:
        module.main(["--help"])
    assert excinfo.value.code == 0
    assert not hasattr(module, "server")

    assert module.main(["--cases", str(tmp_path / "missing.json"), "--output", str(tmp_path / "out.json")]) == 2
    assert "not found" in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()


def test_cli_refuses_to_overwrite_the_quality_sidecar(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_chat_runtime_evaluation_guard",
        Path(__file__).resolve().parents[2] / "scripts/run_chat_runtime_evaluation.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    suite_path = Path(__file__).resolve().parents[1] / "fixtures/chat_runtime_eval_cases.json"
    target = tmp_path / "research_quality_summary.json"
    assert module.main(["--cases", str(suite_path), "--output", str(target)]) == 2
    assert not target.exists()


def test_run_case_entrypoint_evaluates_one_case(tmp_path):
    from webapp import server

    observation = run_case(_case("general-knowledge"), server.app, workspace=tmp_path / "entry")

    assert observation.persisted_status == "completed"
    assert observation.terminal.get("run", {}).get("id") == observation.run_id
