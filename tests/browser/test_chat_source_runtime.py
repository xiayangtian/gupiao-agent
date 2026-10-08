"""Browser-level run/source state contract using a real app and local providers only."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = Path(__file__).resolve().parent / "source_runtime_app.py"
AGENT_BROWSER = (os.environ.get("AGENT_BROWSER") or shutil.which("agent-browser")
                 or str(Path.home() / ".pi/agent/npm/node_modules/.bin/agent-browser"))
pytestmark = pytest.mark.skipif(not Path(AGENT_BROWSER).is_file(), reason="agent-browser CLI unavailable")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _browser(session, *args, allow_failure=False):
    result = subprocess.run([AGENT_BROWSER, "--session", session, "--json", *args],
                            cwd=ROOT, capture_output=True, text=True, timeout=25)
    if result.returncode and not allow_failure:
        pytest.fail(result.stderr or result.stdout)
    if not result.stdout.strip():
        return {}
    payload = json.loads(result.stdout)
    if not payload.get("success", False) and not allow_failure:
        pytest.fail(str(payload.get("error")))
    return payload


@pytest.fixture
def app_url():
    port = _free_port()
    process = subprocess.Popen([sys.executable, str(LAUNCHER), str(port)], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("fixture application exited before startup")
            try:
                import urllib.request
                with urllib.request.urlopen(url + "/api/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail("fixture application did not become healthy")
        yield url
    finally:
        process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.parametrize(("question", "expected_status", "expected_label"), [
    ("2026-09-24 A股来源验收成功复盘", "completed", "数据截至 2026-09-24"),
    ("2026-09-24 A股来源验收网页失败复盘", "partial", "获取失败"),
    ("2026-09-24 A股来源验收全部失败复盘", "partial", "获取失败"),
])
def test_chat_source_status_and_time_are_visible_in_browser(app_url, question, expected_status, expected_label):
    session = "source-runtime-" + uuid.uuid4().hex
    _browser(session, "open", app_url + "/#/chat")
    try:
        _browser(session, "click", "#chat-new-btn")
        _browser(session, "fill", "#chat-input", question)
        _browser(session, "click", "#chat-send-btn")
        _browser(session, "wait", "#chat-history .chat-run")
        payload = _browser(session, "eval", "JSON.stringify((() => {const runs=document.querySelectorAll('#chat-history .chat-run');const run=runs[runs.length-1];"
                  "return {text:run?.textContent||'',width:document.documentElement.scrollWidth,"
                  "inner:window.innerWidth,status:run?.querySelector('.chat-run-status-label')?.textContent||''};})())")
        dom = json.loads(payload["data"]["result"])
        assert expected_label in dom["text"]
        assert ({'completed':'已完成','partial':'部分完成'}[expected_status]) in dom["status"]
        if "网页失败" in question:
            assert "数据截至 2026-09-24" in dom["text"]
            assert dom["text"].count("实时数据 · tencent") == 4
        assert dom["width"] <= dom["inner"]
        assert _browser(session, "errors")["data"].get("errors", []) == []
        assert not [item for item in _browser(session, "console")["data"].get("messages", [])
                    if item.get("type") == "error"]
    finally:
        _browser(session, "close", allow_failure=True)
