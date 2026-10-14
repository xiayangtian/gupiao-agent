"""Investment-news status feedback browser regression using local fake sources/AI only."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from tests.browser.test_chat_source_runtime import AGENT_BROWSER, _browser

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = Path(__file__).with_name("investment_news_test_app.py")
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
        import urllib.request
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("investment-news fixture exited before becoming healthy")
            try:
                with urllib.request.urlopen(url + "/api/health", timeout=1):
                    break
            except OSError:
                time.sleep(.2)
        else:
            pytest.fail("investment-news fixture did not become healthy")
        yield url
    finally:
        process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_success_times_stay_visible_while_operation_notices_auto_hide(app_url):
    session = "investment-news-status-" + uuid.uuid4().hex
    _browser(session, "open", app_url + "/#/investment-news")
    try:
        _browser(session, "click", "#news-collect")
        _browser(session, "wait", "#news-collection-status.success")
        collect_time = _browser(session, "eval", "document.querySelector('#news-last-collection-success')?.textContent")
        assert collect_time["data"]["result"] != "暂无成功记录"
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 5200))")
        hidden = _browser(session, "eval", "JSON.stringify({notice:document.querySelector('#news-collection-status')?.textContent,"
                  "time:document.querySelector('#news-last-collection-success')?.textContent})")
        hidden_dom = json.loads(hidden["data"]["result"])
        assert hidden_dom["notice"] == ""
        assert hidden_dom["time"] == collect_time["data"]["result"]

        _browser(session, "click", "#news-generate-digest")
        _browser(session, "wait", "#news-digest-run-status.success")
        digest_time = _browser(session, "eval", "document.querySelector('#news-last-digest-success')?.textContent")
        assert digest_time["data"]["result"] != "暂无成功记录"

        _browser(session, "eval", "window.__realFetch=window.fetch.bind(window);window.fetch=(url,options)=>"
                  "String(url).endsWith('/api/investment-news/collect')?Promise.reject(new Error('fixture failure')):window.__realFetch(url,options)")
        _browser(session, "click", "#news-collect")
        _browser(session, "wait", "#news-collection-status.error")
        failure = _browser(session, "eval", "document.querySelector('#news-collection-status')?.textContent")
        assert "fixture failure" in failure["data"]["result"]
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 5200))")
        cleared = _browser(session, "eval", "document.querySelector('#news-collection-status')?.textContent")
        assert cleared["data"]["result"] == ""
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _browser(session, "close", allow_failure=True)
