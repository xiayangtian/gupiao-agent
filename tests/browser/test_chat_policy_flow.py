"""M2 policy/source-boundary browser acceptance against the real fixture app."""
import json
import pytest

from tests.browser.test_chat_trust_flow import (
    AGENT_BROWSER, _chat_stream, _console_errors, _eval, _page_errors,
    _run_browser, actual_app_url, browser_session,
)

pytestmark = pytest.mark.skipif(AGENT_BROWSER is None, reason="agent-browser CLI 不可用，无法执行真实应用浏览器回归")


def test_m2_report_policy_and_safe_numeric_degradation(browser_session, actual_app_url):
    session_id, events = _chat_stream(actual_app_url, {
        "question": "农业银行半年报营收是多少？",
        "focus_report": {"code": "601288", "period": "2026-06-30"},
        "use_mcp": False,
    })
    assert next(event for event in events if event["event"] == "policy_resolved")["data"]["intent"] == "report_fact"
    done = next(event for event in events if event["event"] == "done")["data"]["run"]
    assert done["verification_report"]["status"] == "blocked"
    _run_browser(browser_session, "open", actual_app_url + "/#/chat")
    script = f"""(async () => {{
      await openChatSession({json.dumps(session_id)});
      await new Promise(resolve => setTimeout(resolve, 300));
      return JSON.stringify({{text: document.querySelector('#chat-history').textContent,
        policy: document.querySelector('.chat-policy') && document.querySelector('.chat-policy').textContent,
        overflow: document.documentElement.scrollWidth > window.innerWidth}});
    }})()"""
    rendered = _eval(browser_session, script)
    assert "本地财报查证" in rendered["policy"]
    assert "未找到可核验的披露" in rendered["text"]
    assert rendered["overflow"] is False
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
