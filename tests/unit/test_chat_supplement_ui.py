"""Contracts for the report-supplement consent card and resume stream."""
import json
import shutil
import subprocess
from pathlib import Path


APP_JS = Path(__file__).parents[2] / "webapp/static/app.js"
STYLE_CSS = Path(__file__).parents[2] / "webapp/static/style.css"
CHAT_RENDERING_JS = Path(__file__).parents[2] / "webapp/static/chat_rendering.js"
NODE = shutil.which("node")


def _run_node(source: str):
    completed = subprocess.run(
        [NODE, "-e", source], cwd=CHAT_RENDERING_JS.parents[2],
        check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout)


def _source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _function_source(source: str, name: str) -> str:
    """Extract one plain JS function so its streaming EOF branch can run in Node."""
    marker = f"function {name}("
    start = source.index(marker)
    if start >= 6 and source[start - 6:start] == "async ":
        start -= 6
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated function {name}")


def test_consent_card_is_explicitly_opt_in_and_accessible():
    source = _source()

    assert "function renderSupplementConsent(container, payload, handlers)" in source
    assert "checkbox.checked = false" in source
    assert "selected.size === 0" in source
    assert "selected.size >= payload.limit" in source
    assert "aria-live', 'polite'" in source
    assert "checkbox.setAttribute('aria-label'" in source


def test_selection_count_is_visible_from_zero_and_tracks_every_change():
    source = _source()

    assert "function updateSelectionState()" in source
    assert "'确认补充并继续（' + selected.size + '/' + payload.limit + '）'" in source
    assert "updateSelectionState();" in source
    # A rejected sixth selection restores its checkbox and still refreshes 5/5.
    assert "checkbox.checked = false;\n          updateSelectionState();\n          return;" in source


def test_consent_actions_preserve_authorization_boundary():
    source = _source()

    assert "action: 'decline'" in source
    assert "Array.from(selected)" in source
    assert "candidate_ids: candidateIds" in source
    assert "action: 'approve'" in source
    assert "supplements/" in source
    assert "/resolve" in source
    assert "card.setAttribute('aria-busy'" in source
    assert "checkboxes[boxIndex].disabled = busy" in source
    assert "decline.disabled = busy" in source
    assert "approve.disabled = busy || selected.size === 0" in source


def test_supplement_stream_renders_progress_then_existing_run_renderer():
    source = _source()

    assert "function consumeSupplementStream(" in source
    assert "supplement_download_started" in source
    assert "supplement_downloaded" in source
    assert "supplement_ingested" in source
    assert "supplement_failed" in source
    assert "appendAssistantRun('#chat-history'" in source
    assert "本次未补充成功，已基于现有信息回答" in source
    assert "parsed.event === 'supplement_needed'" in source


def test_resolve_stream_eof_without_done_is_a_safe_existing_evidence_fallback():
    source = _source()
    script = "\n".join([
        _function_source(source, "parseSseFrame"),
        _function_source(source, "consumeSupplementStream"),
        """
consumeSupplementStream({
  ok: true,
  body: { getReader: function () { return { read: async function () { return { done: true }; } }; } },
}, {}).then(
  function () { console.error('EOF unexpectedly succeeded'); process.exit(1); },
  function (error) {
    if (error.message !== '补充流未完成') process.exit(2);
    console.log('safe EOF rejection');
  },
);
""",
    ])
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "safe EOF rejection"
    # The action catch clears busy controls before it renders this safe fallback.
    assert "setBusy(false, '本次未补充成功，已基于现有信息回答')" in source


def test_consent_card_styles_keep_controls_keyboard_and_mobile_accessible():
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert ".chat-supplement-consent" in css
    assert "min-height: 44px" in css
    assert ".chat-supplement-consent button:focus-visible" in css
    assert "@media (max-width: 640px)" in css
    assert "flex-direction: column-reverse" not in css
    assert "flex-direction: column;" in css
def _supplement_view(run: dict):
    """Run the persisted-summary mapper in Node against real chat_rendering.js."""
    return _run_node(
        "const rendering = require(" + json.dumps(str(CHAT_RENDERING_JS)) + ");\n"
        "const run = " + json.dumps(run, ensure_ascii=False) + ";\n"
        "console.log(JSON.stringify(rendering.supplementSummaryView(run)));\n"
    )


def _waiting_consent_status_html(status: str) -> str:
    return _run_node(
        "const rendering = require(" + json.dumps(str(CHAT_RENDERING_JS)) + ");\n"
        "console.log(JSON.stringify(rendering.renderRunStatus({status: "
        + json.dumps(status) + "})));\n"
    )


def _candidate(candidate_id: str, period: str, label: str, report_type: str = "semi_annual") -> dict:
    return {
        "id": candidate_id, "company": "农业银行", "code": "601288", "period": period,
        "report_type": report_type, "label": label, "source": "cninfo",
    }


def _supplement_run(status: str, **overrides) -> dict:
    supplement = {
        "status": status, "limit": 5, "candidates": [], "ingested_report_ids": [],
        "skipped_report_ids": [], "failed": [], "resumed_at": "",
    }
    supplement.update(overrides)
    return {"status": "partial", "supplement": supplement}


def test_persisted_supplement_summary_reports_what_was_authorized_and_ingested():
    view = _supplement_view(_supplement_run(
        "completed",
        candidates=[
            _candidate("candidate-a", "2025-06-30", "2025 半年报"),
            _candidate("candidate-b", "2024-06-30", "2024 半年报"),
        ],
        ingested_report_ids=["601288:2025-06-30:semi_annual"],
        failed=[{"candidate_id": "candidate-b", "reason": "download_failed"}],
        resumed_at="2026-09-15T10:00:00",
    ))

    assert view["headline"] == "本次经授权补充 1 份财报"
    assert view["periods"] == ["2025 半年报"]
    assert view["waiting"] is False
    # 失败项从受控候选身份推导，不泄露候选 id、报告 id 或下载地址。
    assert view["failures"] == [
        {"reason": "下载失败", "company": "农业银行", "period": "2024 半年报"},
    ]
    serialized = json.dumps(view, ensure_ascii=False)
    assert "candidate-b" not in serialized
    assert "601288:" not in serialized


def test_persisted_supplement_summary_states_declined_and_failed_plainly():
    declined = _supplement_view(_supplement_run(
        "declined", candidates=[_candidate("candidate-a", "2025-06-30", "2025 半年报")],
    ))
    failed = _supplement_view(_supplement_run(
        "failed", candidates=[_candidate("candidate-a", "2025-06-30", "2025 半年报")],
        failed=[{"candidate_id": "candidate-a", "reason": "ingest_failed"}],
    ))

    assert "未补充财报" in declined["headline"]
    assert declined["periods"] == []
    assert "补充未成功" in failed["headline"]
    assert failed["failures"] == [
        {"reason": "索引失败", "company": "农业银行", "period": "2025 半年报"},
    ]


def test_unknown_supplement_failure_reason_stays_human_readable():
    view = _supplement_view(_supplement_run(
        "failed", failed=[{"candidate_id": "candidate-x", "reason": "upstream_timeout"}],
    ))

    assert view["failures"] == [{"reason": "补充未成功"}]
    assert "upstream_timeout" not in json.dumps(view, ensure_ascii=False)


def test_ingested_periods_are_derived_from_report_ids_when_candidates_are_missing():
    view = _supplement_view(_supplement_run(
        "completed", ingested_report_ids=["601288:2025-06-30:annual"],
    ))

    assert view["periods"] == ["2025 年报"]
    assert "601288" not in json.dumps(view, ensure_ascii=False)


def test_waiting_consent_run_shows_honest_non_actionable_supplement_state():
    view = _supplement_view(_supplement_run(
        "proposed", candidates=[_candidate("candidate-a", "2025-06-30", "2025 半年报")],
    ))
    status_html = _waiting_consent_status_html("waiting_consent")

    assert view["waiting"] is True
    assert view["periods"] == ["2025 半年报"]
    assert "等待" in view["headline"]
    assert "等待授权补充财报" in status_html
    assert "chat-run-status-waiting_consent" in status_html
    # 重载后无法再确认：不得渲染任何失效的确认/重生成按钮。
    assert "button" not in status_html


def test_reloaded_history_renders_supplement_summary_through_text_content():
    source = _source()

    assert "function appendSupplementSummary(box, run)" in source
    assert "rendering.supplementSummaryView(run)" in source
    assert "appendSupplementSummary(wrapper, run);" in source
    assert "head.textContent = view.headline" in source
    assert "aria-label', '未能补充的财报'" in source
    # 摘要节点只用 textContent 写入持久化数据，不得拼 innerHTML。
    assert "section.innerHTML" not in source
    assert "chat-supplement-summary-failures" in source


def test_supplement_summary_styles_stay_mobile_safe():
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert ".chat-supplement-summary" in css
    assert ".chat-supplement-summary-failures" in css
    assert "overflow-wrap: anywhere" in css
