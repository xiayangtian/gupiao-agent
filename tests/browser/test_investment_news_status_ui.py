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


def test_collection_selects_the_latest_available_market_day(app_url):
    session = "investment-news-latest-day-" + uuid.uuid4().hex
    _browser(session, "open", app_url + "/#/investment-news")
    try:
        _browser(session, "click", '[data-news-window="market-yesterday"]')
        _browser(session, "click", "#news-collect")
        _browser(session, "wait", "#news-collection-status.success")
        selected_day = _browser(session, "eval", "(async()=>{const status=await fetch('/api/investment-news/status').then(r=>r.json());"
                  "return JSON.stringify({selected:document.querySelector('[data-news-window^=\\\"market-\\\"][aria-pressed=\\\"true\\\"]')?.dataset.newsWindow,"
                  "latest:status.latest_news_day,today:new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai'}).format(new Date())});})()")
        selected = json.loads(selected_day["data"]["result"])
        assert selected["selected"] == ("market-today" if selected["latest"] == selected["today"] else "market-yesterday")
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _browser(session, "close", allow_failure=True)


def test_watchlist_date_shortcuts_and_historical_digest_browsing(app_url):
    session = "investment-news-date-window-" + uuid.uuid4().hex
    _browser(session, "open", app_url + "/#/investment-news")
    try:
        _browser(session, "click", '[data-news-kind="watchlist"]')
        _browser(session, "click", '[data-news-window="watchlist-7"]')
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 300))")
        seven_days = _browser(session, "eval", "document.querySelector('#news-list')?.textContent")
        assert "长江电力三十天内资讯" not in seven_days["data"]["result"]

        _browser(session, "click", '[data-news-window="watchlist-30"]')
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 300))")
        thirty_days = _browser(session, "eval", "document.querySelector('#news-list')?.textContent")
        assert "长江电力七天内资讯" in thirty_days["data"]["result"]

        _browser(session, "click", '[data-news-window="watchlist-today"]')
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 300))")
        today_watchlist = _browser(session, "eval", "document.querySelector('#news-list')?.textContent")
        assert "长江电力测试资讯" in today_watchlist["data"]["result"]
        assert "长江电力七天内资讯" not in today_watchlist["data"]["result"]

        _browser(session, "eval", "(()=>{const input=document.querySelector('#news-digest-day');const d=new Date(input.value+'T00:00:00Z');"
                  "d.setUTCDate(d.getUTCDate()-1);input.value=d.toISOString().slice(0,10);"
                  "input.dispatchEvent(new Event('change',{bubbles:true}));})()")
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 300))")
        historical = _browser(session, "eval", "JSON.stringify({day:document.querySelector('#news-digest-day')?.value,"
                    "title:document.querySelector('#news-digest-title')?.textContent,"
                    "summary:document.querySelector('#news-digest')?.textContent})")
        historical_dom = json.loads(historical["data"]["result"])
        assert historical_dom["title"] == historical_dom["day"] + " AI 综述"
        assert "历史日期综述 fixture" in historical_dom["summary"]
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _browser(session, "close", allow_failure=True)


def test_summary_date_and_news_day_shortcuts_are_separate(app_url):
    session = "news-controls-" + uuid.uuid4().hex[:12]
    _browser(session, "open", app_url + "/#/investment-news")
    try:
        top_date = _browser(session, "eval", "Boolean(document.querySelector('#news-digest-day')?.closest('.news-digest-card'))")
        assert top_date["data"]["result"] is True

        _browser(session, "click", '[data-news-window="market-yesterday"]')
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 300))")
        yesterday_news = _browser(session, "eval", "document.querySelector('#news-list')?.textContent")
        assert "昨日市场资讯 fixture" in yesterday_news["data"]["result"]
        _browser(session, "click", '[data-news-window="market-today"]')
        _browser(session, "eval", "(()=>{const input=document.querySelector('#news-digest-day');const d=new Date(input.value+'T00:00:00Z');"
                  "d.setUTCDate(d.getUTCDate()-1);input.value=d.toISOString().slice(0,10);"
                  "input.dispatchEvent(new Event('change',{bubbles:true}));})()")
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 300))")
        state = _browser(session, "eval", "JSON.stringify({summary:document.querySelector('#news-digest')?.textContent,"
                  "news:document.querySelector('#news-list')?.textContent,todayPressed:document.querySelector('[data-news-window=\\\"market-today\\\"]')?.getAttribute('aria-pressed')})")
        current = json.loads(state["data"]["result"])
        assert "历史日期综述 fixture" in current["summary"]
        assert "昨日市场资讯 fixture" not in current["news"]
        assert current["todayPressed"] == "true"

        _browser(session, "click", '[data-news-kind="watchlist"]')
        _browser(session, "click", '[data-news-window="watchlist-today"]')
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 300))")
        today_watchlist = _browser(session, "eval", "document.querySelector('#news-list')?.textContent")
        assert "长江电力测试资讯" in today_watchlist["data"]["result"]
        assert "长江电力七天内资讯" not in today_watchlist["data"]["result"]
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _browser(session, "close", allow_failure=True)


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
