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
    ("context-budget-long-history", "completed"),
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


def test_relevance_window_recovers_the_gold_fact_clipped_by_legacy_prefix(tmp_path):
    from webapp import server
    from webapp.chat_runtime_eval_report import score

    case = _case("report-number-after-300")
    legacy = OfflineChatHarness(case, workspace=tmp_path / "legacy", context_window_strategy="legacy")
    candidate = OfflineChatHarness(case, workspace=tmp_path / "candidate", context_window_strategy="relevance")

    legacy_observation = legacy.run(server.app)
    candidate_observation = candidate.run(server.app)

    legacy_score = score(case, legacy_observation)
    candidate_score = score(case, candidate_observation)
    evidence_id = "601288:2026-06-30:semi_annual#p40"
    assert legacy_score.citation_support_found == 0
    assert candidate_score.citation_support_found == 1
    assert legacy_observation.retrieval_diagnostics[evidence_id] == "window_clipped"
    assert candidate_observation.retrieval_diagnostics[evidence_id] == "supported"
    assert candidate_observation.call_attempts == legacy_observation.call_attempts


def test_cli_revision_fingerprints_dirty_working_tree(tmp_path):
    import importlib.util
    import os
    import subprocess
    from pathlib import Path

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    source = repo / "tracked.py"
    source.write_text("value = 1\n", encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
    subprocess.run(["git", "add", "tracked.py"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, env=env, check=True)

    script = Path(__file__).resolve().parents[2] / "scripts/run_chat_runtime_evaluation.py"
    spec = importlib.util.spec_from_file_location("runtime_eval_cli", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.PROJECT_ROOT = repo
    clean_revision = module._code_revision()
    source.write_text("value = 2\n", encoding="utf-8")
    dirty_revision = module._code_revision()

    assert "+dirty:" not in clean_revision
    assert dirty_revision.startswith(clean_revision + "+dirty:")
    assert len(dirty_revision.rsplit(":", 1)[-1]) == 12


def test_context_budget_reduces_long_followup_prompt_without_extra_source_calls(tmp_path):
    from webapp import server

    case = _case("context-budget-long-history")
    unbounded = OfflineChatHarness(case, workspace=tmp_path / "unbounded",
                                   context_budget_enabled=False)
    bounded = OfflineChatHarness(case, workspace=tmp_path / "bounded",
                                 context_budget_enabled=True)

    unbounded_observation = unbounded.run(server.app)
    bounded_observation = bounded.run(server.app)

    assert unbounded_observation.persisted_status == bounded_observation.persisted_status == "completed"
    assert bounded_observation.prompt_characters < unbounded_observation.prompt_characters
    assert bounded_observation.call_attempts == unbounded_observation.call_attempts


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
