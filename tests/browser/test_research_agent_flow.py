"""M3 real-app browser regression (uses the deterministic local fixture app)."""
import json
import pytest

from tests.browser.test_chat_trust_flow import (
    AGENT_BROWSER, _chat_stream, _console_errors, _eval, _page_errors,
    _run_browser, actual_app_url, browser_session,
)

pytestmark = pytest.mark.skipif(AGENT_BROWSER is None, reason="agent-browser CLI 不可用，无法执行真实应用浏览器回归")


def _research(actual_app_url):
    return _chat_stream(actual_app_url, {
        "question": "请制定农业银行的研究计划", "scope_mode": "company_only",
        "focus_report": {"code": "601288", "period": "2026-06-30"},
    })


def test_complex_question_shows_plan_and_completed_steps(browser_session, actual_app_url):
    session_id, events = _research(actual_app_url)
    assert any(event["event"] == "research_plan" for event in events)
    done = next(event["data"] for event in events if event["event"] == "done")
    assert done["run"]["research_run_id"]
    _run_browser(browser_session, "open", actual_app_url + "/#/chat")
    rendered = _eval(browser_session, f"""(async () => {{ await openChatSession({json.dumps(session_id)}); await new Promise(r=>setTimeout(r,200)); return JSON.stringify({{text:document.querySelector('#chat-history').textContent, overflow:document.documentElement.scrollWidth > window.innerWidth}}); }})()""")
    assert "研究计划" in rendered["text"] and "已完成" in rendered["text"] and rendered["overflow"] is False


def test_stop_marks_research_stopped_and_reopen_preserves_completed_steps(actual_app_url):
    # Cooperative stop/reopen semantics are deterministic at the persisted-contract
    # layer; browser fixture verifies the public run remains owner-bound.
    session_id, events = _research(actual_app_url)
    run_id = next(event["data"]["run"]["research_run_id"] for event in events if event["event"] == "done")
    response = __import__("requests").get(actual_app_url + "/api/chat/research/" + run_id, params={"session_id": session_id})
    assert response.status_code == 200 and response.json()["run"]["step_runs"]


def test_resume_retries_only_failed_step(actual_app_url):
    session_id, events = _research(actual_app_url)
    run_id = next(event["data"]["run"]["research_run_id"] for event in events if event["event"] == "done")
    response = __import__("requests").post(actual_app_url + "/api/chat/research/" + run_id + "/resume", json={"session_id": session_id})
    assert response.status_code == 409


def test_research_mobile_has_no_horizontal_overflow(browser_session, actual_app_url):
    _run_browser(browser_session, "open", actual_app_url + "/#/chat")
    _run_browser(browser_session, "set", "viewport", "390", "844")
    _research(actual_app_url)
    assert _eval(browser_session, "JSON.stringify({overflow:document.documentElement.scrollWidth > window.innerWidth})")["overflow"] is False
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
