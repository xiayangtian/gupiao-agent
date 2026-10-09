"""P3 cancellation, capacity admission and research-resume UX in isolated browsers."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = Path(__file__).with_name("chat_reliability_p3_app.py")
AGENT_BROWSER = (os.environ.get("AGENT_BROWSER") or shutil.which("agent-browser")
                 or str(Path.home() / ".pi/agent/npm/node_modules/.bin/agent-browser"))
pytestmark = pytest.mark.skipif(not Path(AGENT_BROWSER).is_file(), reason="agent-browser CLI unavailable")


def _browser(session, *args):
    result = subprocess.run([AGENT_BROWSER, "--session", session, "--json", *args],
                            cwd=ROOT, capture_output=True, text=True, timeout=25)
    if result.returncode:
        pytest.fail(result.stderr or result.stdout)
    payload = json.loads(result.stdout)
    if not payload.get("success", False):
        pytest.fail(str(payload.get("error")))
    return payload


def _get_json(url):
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.load(response)


def _post_json(url):
    request = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


def _start_fixture(scenario):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(LAUNCHER), str(port), scenario], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if process.poll() is not None:
            pytest.fail("P3 fixture exited before startup")
        try:
            _get_json(url + "/api/health")
            return process, url
        except OSError:
            time.sleep(.1)
    process.terminate()
    pytest.fail("P3 fixture did not become healthy")


def _close_fixture(process, sessions):
    for session in sessions:
        try:
            _browser(session, "close")
        except Exception:
            pass
    process.terminate()
    try:
        process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)


def test_stop_cancels_inflight_source_without_followup_calls():
    process, url = _start_fixture("lifecycle")
    session = "p3-stop-" + uuid.uuid4().hex
    try:
        _browser(session, "open", url + "/#/chat")
        _browser(session, "fill", "#chat-input", "600519 2026-09-24 股价走势")
        _browser(session, "click", "#chat-send-btn")
        deadline = time.monotonic() + 10
        state = {}
        while time.monotonic() < deadline:
            state = _get_json(url + "/_fixture/p3-state")
            if state["started"]:
                break
            time.sleep(.05)
        assert state["started"], "fake source call was not reached"
        progress = _browser(session, "eval", "JSON.stringify([...document.querySelectorAll('#chat-history .chat-tool-step.running')].map(n => n.textContent))")
        assert "market_kline" in json.loads(progress["data"]["result"])[0]

        _browser(session, "click", "#chat-send-btn")
        time.sleep(.2)
        _post_json(url + "/_fixture/p3-release")
        deadline = time.monotonic() + 10
        visible = ""
        while time.monotonic() < deadline:
            payload = _browser(session, "eval", "document.querySelector('#chat-history')?.innerText || ''")
            visible = payload.get("data", {}).get("result", "")
            if "已停止" in visible or "stopped" in visible:
                break
            time.sleep(.1)
        state = _get_json(url + "/_fixture/p3-state")
        assert len(state["calls"]) == 1, "cancelled execution must not start its next source step"
        assert "已停止" in visible or "stopped" in visible
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "stopped" not in state["saved_statuses"]:
            time.sleep(.05)
            state = _get_json(url + "/_fixture/p3-state")
        assert "stopped" in state["saved_statuses"], "cancelled AnswerRun must be persisted as stopped"
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _close_fixture(process, [session])


def test_four_workers_reject_fifth_request_then_recover_capacity():
    process, url = _start_fixture("capacity")
    sessions = []
    try:
        for index in range(4):
            session = f"p3-cap-{index}-" + uuid.uuid4().hex
            sessions.append(session)
            _browser(session, "open", url + "/#/chat")
            _browser(session, "fill", "#chat-input", "600519 2026-09-24 股价走势")
            _browser(session, "click", "#chat-send-btn")
        deadline = time.monotonic() + 15
        state = {}
        while time.monotonic() < deadline:
            state = _get_json(url + "/_fixture/p3-state")
            if state.get("active_runs") == 4 and len(state.get("calls", [])) == 4:
                break
            time.sleep(.05)
        assert state.get("active_runs") == 4, state
        assert len(state.get("calls", [])) == 4, state

        fifth = "p3-cap-fifth-" + uuid.uuid4().hex
        sessions.append(fifth)
        _browser(fifth, "open", url + "/#/chat")
        _browser(fifth, "fill", "#chat-input", "600519 2026-09-24 股价走势")
        _browser(fifth, "click", "#chat-send-btn")
        deadline = time.monotonic() + 8
        visible = ""
        while time.monotonic() < deadline:
            payload = _browser(fifth, "eval", "document.querySelector('#chat-history')?.innerText || ''")
            visible = payload.get("data", {}).get("result", "")
            if "当前问答任务较多" in visible:
                break
            time.sleep(.1)
        assert "当前问答任务较多" in visible
        assert _get_json(url + "/_fixture/p3-state")["active_runs"] == 4

        _post_json(url + "/_fixture/p3-release")
        deadline = time.monotonic() + 12
        state = {}
        while time.monotonic() < deadline:
            state = _get_json(url + "/_fixture/p3-state")
            if state.get("active_runs") == 0:
                break
            time.sleep(.05)
        assert state.get("active_runs") == 0, state
        assert len(state.get("calls", [])) == 4

        recovered = "p3-cap-recovered-" + uuid.uuid4().hex
        sessions.append(recovered)
        _browser(recovered, "open", url + "/#/chat")
        _browser(recovered, "fill", "#chat-input", "600519 2026-09-24 股价走势")
        _browser(recovered, "click", "#chat-send-btn")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = _get_json(url + "/_fixture/p3-state")
            if len(state.get("calls", [])) == 5:
                break
            time.sleep(.05)
        assert len(state.get("calls", [])) == 5, state
        assert state.get("active_runs", 0) <= 1
        assert _browser(recovered, "errors")["data"].get("errors", []) == []
    finally:
        _post_json(url + "/_fixture/p3-release")
        _close_fixture(process, sessions)


def test_stopped_research_resume_does_not_repeat_completed_retrieval():
    process, url = _start_fixture("research-resume")
    session = "p3-research-resume-" + uuid.uuid4().hex
    try:
        _browser(session, "open", url + "/#/chat")
        _browser(session, "fill", "#chat-input", "研究计划停止验收 601288 2026年半年报财报")
        _browser(session, "click", "#chat-send-btn")
        deadline = time.monotonic() + 12
        state = {}
        while time.monotonic() < deadline:
            state = _get_json(url + "/_fixture/p3-state")
            if state.get("normalize_started"):
                break
            time.sleep(.05)
        if not state.get("normalize_started"):
            state["dom"] = _browser(session, "eval", "document.querySelector('#chat-history')?.innerText || ''").get("data", {}).get("result", "")
            state["browser_errors"] = _browser(session, "errors")["data"].get("errors", [])
        assert state.get("normalize_started"), json.dumps(state, ensure_ascii=False)
        assert state.get("retrieval_calls") == 1, state

        _browser(session, "click", "#chat-send-btn")
        _post_json(url + "/_fixture/p3-release")
        deadline = time.monotonic() + 12
        state = {}
        visible = ""
        while time.monotonic() < deadline:
            state = _get_json(url + "/_fixture/p3-state")
            payload = _browser(session, "eval", "document.querySelector('#chat-history')?.innerText || ''")
            visible = payload.get("data", {}).get("result", "")
            if state.get("research_status") == "stopped":
                break
            time.sleep(.1)
        assert state.get("research_status") == "stopped", state
        completed_before_resume = state.get("completed_step_ids", [])
        assert completed_before_resume in (["retrieve"], ["retrieve", "normalize"]), state
        # The in-flight DOM only has the pre-stop run snapshot; reload the persisted
        # history so the recovery action uses the server's terminal research state.
        _browser(session, "reload")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            payload = _browser(session, "eval", "document.querySelector('#chat-history')?.innerText || ''")
            visible = payload.get("data", {}).get("result", "")
            if "继续研究" in visible:
                break
            time.sleep(.1)
        assert "继续研究" in visible, json.dumps(state, ensure_ascii=False)
        assert state.get("retrieval_calls") == 1, state

        _browser(session, "click", '[data-chat-action="resume-research"]')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = _get_json(url + "/_fixture/p3-state")
            if state.get("research_status") == "completed":
                break
            time.sleep(.1)
        assert state.get("research_status") == "completed", state
        assert state.get("completed_step_ids") == ["retrieve", "normalize", "compare", "verify", "answer"]
        assert state.get("retrieval_calls") == 1, "resuming must reuse persisted completed retrieval"
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _post_json(url + "/_fixture/p3-release")
        _close_fixture(process, [session])
