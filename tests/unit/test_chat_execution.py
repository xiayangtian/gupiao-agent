import pytest


def _status(**overrides):
    from webapp.chat_execution import resolve_answer_status
    values = dict(stopped=False, failed=False, waiting_consent=False, required_missing=False,
                  had_source_failure=False, retrieval_degraded=False, verification="passed")
    values.update(overrides)
    return resolve_answer_status(**values)


@pytest.mark.parametrize("verification", ["blocked", "partial"])
def test_failed_verification_never_marks_run_completed(verification):
    assert _status(verification=verification) == "partial"


def test_status_prioritizes_stopped_then_failure_then_consent():
    assert _status(stopped=True, failed=True, waiting_consent=True) == "stopped"
    assert _status(failed=True, waiting_consent=True) == "failed"
    assert _status(waiting_consent=True) == "waiting_consent"


def test_source_failure_and_missing_verification_are_partial():
    assert _status(had_source_failure=True) == "partial"
    assert _status(verification=None) == "partial"


def test_unknown_time_source_is_partial_and_keeps_acquisition_time():
    from webapp.chat_execution import project_sources
    from webapp.chat_models import Scope
    from webapp.source_runtime import SourceResult

    result = SourceResult("s1", "fixture", "quote", "market", "success",
                          content="bounded", fetched_at="2026-09-24T10:00:00+08:00")
    projection = project_sources([result], Scope.whole_corpus())
    assert len(projection.tool_artifacts) == 1
    artifact = projection.tool_artifacts[0]
    assert artifact.status == "partial"
    assert artifact.as_of == ""
    assert artifact.fetched_at == "2026-09-24T10:00:00+08:00"
    assert artifact.source_id == "s1"
    assert projection.facts == ()


def test_local_retrieval_is_not_fabricated_as_external_tool_artifact():
    from webapp.chat_execution import project_sources
    from webapp.chat_models import Scope
    from webapp.source_runtime import SourceResult

    projection = project_sources([SourceResult("local-1", "local", "retrieve", "local", "success",
        retrieval_hits=({"source": "pdf", "snippet": "evidence"},))], Scope.whole_corpus())
    assert projection.tool_artifacts == ()
    assert projection.source_summary["local_pdf"] == "已使用"


def test_same_source_id_is_projected_once_but_distinct_sources_do_not_overwrite():
    from webapp.chat_execution import project_sources
    from webapp.chat_models import Scope
    from webapp.source_runtime import SourceResult

    results = [
        SourceResult("s1", "fixture", "quote", "market", "success", "one", "2026-09-24"),
        SourceResult("s1", "fixture", "quote", "market", "failed", error_code="timeout"),
        SourceResult("s2", "fixture", "fund_flow", "market", "failed", error_code="timeout"),
    ]
    projection = project_sources(results, Scope.whole_corpus())
    assert [item.source_id for item in projection.tool_artifacts] == ["s1", "s2"]
    assert projection.had_failure


def test_old_tool_artifact_fields_round_trip_and_partial_can_omit_as_of():
    from webapp.chat_models import ToolArtifact

    old = ToolArtifact.from_dict({"provider": "fixture", "tool_name": "quote",
                                  "status": "failed", "as_of": ""})
    assert old.fetched_at == old.source_id == ""
    assert ToolArtifact.from_dict(old.to_dict()) == old
    partial = ToolArtifact("fixture", "quote", status="partial", fetched_at="2026-09-24T10:00:00+08:00")
    assert partial.as_of == ""
