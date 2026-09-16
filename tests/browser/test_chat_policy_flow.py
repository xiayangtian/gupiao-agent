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


def test_m2_policy_fixture_covers_industry_realtime_event_conflict_tool_failure_and_scope(browser_session, actual_app_url):
    def stream(question, *, scope_mode="company_only"):
        return _chat_stream(actual_app_url, {
            "question": question,
            "scope_mode": scope_mode,
            "focus_report": {"code": "601288", "period": "2026-06-30"},
            "use_mcp": True,
        })

    industry_session, industry_events = stream("农业银行行业验收", scope_mode="auto")
    industry_run = next(event for event in industry_events if event["event"] == "done")["data"]["run"]
    assert industry_run["scope"]["mode"] == "company_industry"

    realtime_session, realtime_events = stream("农业银行实时验收")
    realtime_run = next(event for event in realtime_events if event["event"] == "done")["data"]["run"]
    assert realtime_run["facts"][0]["verification"] == "reference"
    assert realtime_run["facts"][0]["as_of"]

    event_session, event_events = stream("农业银行公告事件验收")
    event_run = next(event for event in event_events if event["event"] == "done")["data"]["run"]
    assert event_run["artifacts"][0]["source"] == "web"

    conflict_session, conflict_events = stream("农业银行冲突验收")
    conflict_run = next(event for event in conflict_events if event["event"] == "done")["data"]["run"]
    assert conflict_run["verification_report"]["status"] == "partial"
    assert conflict_run["conflicts"]

    _, failed_events = stream("农业银行工具失败验收")
    failed_run = next(event for event in failed_events if event["event"] == "done")["data"]["run"]
    assert failed_run["status"] == "partial"

    _, scope_events = stream("农业银行范围越界验收")
    scope_run = next(event for event in scope_events if event["event"] == "done")["data"]["run"]
    assert scope_run["facts"] == []

    for session_id, expected_text in (
        (industry_session, "本地可检索同业样本"),
        (realtime_session, "外部参考"),
        (event_session, "公告"),
        (conflict_session, "存在口径/时间差异"),
    ):
        rendered = _eval(browser_session, f"""(async () => {{
          await openChatSession({json.dumps(session_id)});
          await new Promise(resolve => setTimeout(resolve, 200));
          return JSON.stringify({{text: document.querySelector('#chat-history').textContent,
            overflow: document.documentElement.scrollWidth > window.innerWidth}});
        }})()""")
        assert expected_text in rendered["text"]
        assert rendered["overflow"] is False
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
