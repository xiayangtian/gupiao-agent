"""Static contracts for the report-supplement consent card and resume stream."""
from pathlib import Path


APP_JS = Path(__file__).parents[2] / "webapp/static/app.js"
STYLE_CSS = Path(__file__).parents[2] / "webapp/static/style.css"


def _source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_consent_card_is_explicitly_opt_in_and_accessible():
    source = _source()

    assert "function renderSupplementConsent(container, payload, handlers)" in source
    assert "checkbox.checked = false" in source
    assert "selected.size === 0" in source
    assert "selected.size >= payload.limit" in source
    assert "aria-live', 'polite'" in source
    assert "checkbox.setAttribute('aria-label'" in source


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


def test_consent_card_styles_keep_controls_keyboard_and_mobile_accessible():
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert ".chat-supplement-consent" in css
    assert "min-height: 44px" in css
    assert ".chat-supplement-consent button:focus-visible" in css
    assert "@media (max-width: 640px)" in css
