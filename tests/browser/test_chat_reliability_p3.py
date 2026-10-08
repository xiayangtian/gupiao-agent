"""P3 cancellation UX against an isolated, deterministic FastAPI app."""
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


def test_stop_cancels_inflight_source_without_followup_calls():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(LAUNCHER), str(port)], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}"
    session = "p3-stop-" + uuid.uuid4().hex
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("P3 fixture exited before startup")
            try:
                _get_json(url + "/api/health")
                break
            except OSError:
                time.sleep(.2)
        else:
            pytest.fail("P3 fixture did not become healthy")

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
        time.sleep(.2)  # allow the explicit cancel request and stream disconnect to reach the app
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
        _browser(session, "close")
        process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
