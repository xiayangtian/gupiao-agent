"""P2 real-browser checks against a local fake app; never call external sources."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests.browser.test_chat_source_runtime import AGENT_BROWSER, _browser

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = Path(__file__).with_name("chat_reliability_p2_app.py")
pytestmark = pytest.mark.skipif(not Path(AGENT_BROWSER).is_file(), reason="agent-browser CLI unavailable")


@pytest.fixture(scope="module")
def app_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(LAUNCHER), str(port)], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("P2 fixture exited before becoming healthy")
            try:
                with urllib.request.urlopen(url + "/api/health", timeout=1):
                    break
            except OSError:
                time.sleep(.2)
        else:
            pytest.fail("P2 fixture did not become healthy")
        yield url
    finally:
        process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def _send(session, question):
    _browser(session, "click", "#chat-new-btn")
    _browser(session, "fill", "#chat-input", question)
    _browser(session, "click", "#chat-send-btn")
    _browser(session, "wait", "#chat-history .chat-run")
    payload = _browser(session, "eval", "JSON.stringify((() => {const run=[...document.querySelectorAll('#chat-history .chat-run')].at(-1);"
                       "return {text:run?.textContent||'',note:run?.querySelector('.chat-run-status-note')?.textContent||'',"
                       "overflow:document.documentElement.scrollWidth>window.innerWidth};})())")
    return json.loads(payload["data"]["result"])


def _calls(url):
    with urllib.request.urlopen(url + "/_fixture/p2-calls", timeout=2) as response:
        return json.load(response)


def test_clarification_avoids_planner_and_sources(app_url):
    session = "p2-clarify-" + uuid.uuid4().hex
    _browser(session, "open", app_url + "/#/chat")
    try:
        before = _calls(app_url)
        view = _send(session, "股价走势")
        assert "请明确公司" in view["text"]
        assert _calls(app_url) == before
        assert not view["overflow"]
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _browser(session, "close", allow_failure=True)


def test_company_without_report_market_and_recap_show_actual_coverage(app_url):
    session = "p2-coverage-" + uuid.uuid4().hex
    _browser(session, "open", app_url + "/#/chat")
    try:
        company = _send(session, "600519 2026-09-24 股价走势")
        assert "贵州茅台" in company["text"]
        assert "2026-09-24" in company["text"]
        recap = _send(session, "2026-09-24 A股复盘")
        assert "返回上限为 50 条" in recap["text"]
        assert "总量未知" in recap["text"]
        assert "行业资金流" in recap["text"]
        assert not recap["overflow"]
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _browser(session, "close", allow_failure=True)


def test_knowledge_label_keyboard_and_mobile_overflow(app_url):
    session = "p2-knowledge-" + uuid.uuid4().hex
    _browser(session, "open", app_url + "/#/chat")
    try:
        _browser(session, "set", "viewport", "390", "844")
        before = _calls(app_url)
        view = _send(session, "什么是市盈率？")
        assert "依据模型常识，未检索外部来源" in view["note"]
        assert "市盈率" in view["text"]
        assert _calls(app_url) == before
        assert not view["overflow"]
        _browser(session, "click", "#chat-input")
        _browser(session, "press", "Tab")
        _browser(session, "press", "Shift+Tab")
        focus = _browser(session, "eval", "JSON.stringify((() => {const input=document.querySelector('#chat-input');"
                         "return {tag:document.activeElement?.tagName,aria:input?.getAttribute('aria-label'),"
                         "focus:getComputedStyle(input).outlineStyle};})())")
        focus_state = json.loads(focus["data"]["result"])
        assert focus_state["tag"] == "INPUT"
        assert focus_state["aria"] or _browser(session, "eval", "document.querySelector('#chat-input')?.placeholder")["data"]["result"]
        assert _browser(session, "errors")["data"].get("errors", []) == []
        assert not [item for item in _browser(session, "console")["data"].get("messages", [])
                    if item.get("type") == "error"]
    finally:
        _browser(session, "close", allow_failure=True)
