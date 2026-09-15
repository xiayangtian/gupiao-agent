"""Contracts for the report-supplement consent card and resume stream."""
import subprocess
from pathlib import Path


APP_JS = Path(__file__).parents[2] / "webapp/static/app.js"
STYLE_CSS = Path(__file__).parents[2] / "webapp/static/style.css"


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
