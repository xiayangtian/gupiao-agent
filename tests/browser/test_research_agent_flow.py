"""M3 real-app browser regression (uses the deterministic local fixture app)."""
import json
import time

import pytest

from tests.browser.test_chat_trust_flow import (
    AGENT_BROWSER, _chat_stream, _console_errors, _eval, _page_errors,
    _run_browser, _start_observing, actual_app_url, browser_session,
)

pytestmark = pytest.mark.skipif(AGENT_BROWSER is None, reason="agent-browser CLI 不可用，无法执行真实应用浏览器回归")


def _chat_text(browser_session):
    return _eval(browser_session, "JSON.stringify({text:(document.querySelector('#chat-history')||{}).textContent})")["text"]


def _wait_for_chat_text(browser_session, needle, timeout=12.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = _chat_text(browser_session)
        if needle in text:
            return text
        time.sleep(0.25)
    return _chat_text(browser_session)


def _wait_for_resume_button_count(browser_session, expected, timeout=12.0):
    """轮询「继续研究」按钮数量：完成/不可恢复的运行不得留下死按钮。"""
    deadline = time.monotonic() + timeout
    count = None
    while time.monotonic() < deadline:
        count = _eval(browser_session, "JSON.stringify({count:document.querySelectorAll('[data-chat-action=\"resume-research\"]').length})")["count"]
        if count == expected:
            return count
        time.sleep(0.25)
    return count


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
    rendered = _eval(browser_session, f"""(async () => {{ await openChatSession({json.dumps(session_id)}); await new Promise(r=>setTimeout(r,200)); return JSON.stringify({{text:document.querySelector('#chat-history').textContent, overflow:document.documentElement.scrollWidth > window.innerWidth, resume:document.querySelectorAll('[data-chat-action="resume-research"]').length}}); }})()""")
    assert "研究计划" in rendered["text"] and "已完成" in rendered["text"] and rendered["overflow"] is False
    # 已完成的研究运行不得留下点击后必然失败的「继续研究」死按钮。
    assert rendered["resume"] == 0


def test_stop_marks_research_stopped_and_reopen_preserves_steps(browser_session, actual_app_url):
    """真实停止点击：运行未完成前停止，历史重开仍保留计划、步骤与恢复入口。"""
    time.sleep(0.5)
    _eval(browser_session, "submitQuestion('请制定农业银行研究计划（停止验收）', chatStreamKey()); JSON.stringify({started:true})")
    assert "检索已授权披露：进行中" in _wait_for_chat_text(browser_session, "检索已授权披露：进行中")
    _eval(browser_session, "stopChatStream(); JSON.stringify({stopped:true})")
    text = _wait_for_chat_text(browser_session, "已停止")
    assert "已停止" in text
    sid = _eval(browser_session, "JSON.stringify({sid:chatSessionId})")["sid"]
    _eval(browser_session, f"(async () => {{ await openChatSession({json.dumps(sid)}); return JSON.stringify({{ok:true}}); }})()")
    reopened = _wait_for_chat_text(browser_session, "检索已授权披露")
    assert "研究计划" in reopened
    assert "已停止" in reopened
    resume_button = _eval(browser_session, "JSON.stringify({exists:!!document.querySelector('[data-chat-action=\"resume-research\"]')})")
    assert resume_button["exists"], reopened


def test_resume_retries_failed_research_step_through_browser_action(browser_session, actual_app_url):
    """失败步骤只通过恢复入口重试，成功后展示服务端返回的回答。"""
    time.sleep(0.5)
    _eval(browser_session, "submitQuestion('请制定农业银行研究计划（失败验收）', chatStreamKey()); JSON.stringify({started:true})")
    failed = _wait_for_chat_text(browser_session, "研究未完成")
    target = _eval(browser_session, "JSON.stringify({failed:!!document.querySelector('.chat-run-status-failed'), exists:!!document.querySelector('[data-chat-action=\"resume-research\"]')})")
    assert target["failed"], failed
    assert target["exists"], failed
    _eval(browser_session, "document.querySelector('[data-chat-action=\"resume-research\"]').click(); JSON.stringify({clicked:true})")
    after = _wait_for_chat_text(browser_session, "已基于范围内披露恢复研究。")
    assert "已基于范围内披露恢复研究。" in after
    # 恢复成功后该运行已完成：旧按钮必须消失，不得留下禁用/失效的恢复入口。
    assert _wait_for_resume_button_count(browser_session, 0) == 0


def test_research_layout_renders_without_overflow_at_three_viewports(browser_session, actual_app_url):
    """1280x900 / 768x1000 / 390x844 三档真实浏览器 QA：无横向溢出与控制台错误。"""
    session_id, events = _research(actual_app_url)
    assert any(event["event"] == "research_plan" for event in events)
    _start_observing(browser_session)
    _run_browser(browser_session, "open", actual_app_url + "/#/chat")
    for width, height in ((1280, 900), (768, 1000), (390, 844)):
        _run_browser(browser_session, "set", "viewport", str(width), str(height))
        rendered = _eval(browser_session, f"""(async () => {{ await openChatSession({json.dumps(session_id)}); await new Promise(r=>setTimeout(r,200)); return JSON.stringify({{text:document.querySelector('#chat-history').textContent, overflow:document.documentElement.scrollWidth > window.innerWidth}}); }})()""")
        assert "研究计划" in rendered["text"], (width, rendered["text"][:200])
        assert rendered["overflow"] is False, width
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
