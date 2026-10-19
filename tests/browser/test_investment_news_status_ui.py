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


def test_monday_digest_help_explains_collection_precondition(app_url):
    session = "investment-news-collection-help-" + uuid.uuid4().hex
    _browser(session, "open", app_url + "/#/investment-news")
    try:
        help_text = _browser(session, "eval", "document.querySelector('#news-digest-input-help')?.textContent || ''")
        assert "不会自动采集" in help_text["data"]["result"]
        assert "先点击“采集资讯”" in help_text["data"]["result"]
        assert "采集完成后再点击页面上方的综述生成按钮" in help_text["data"]["result"]
    finally:
        _browser(session, "close", allow_failure=True)


def test_digest_selector_only_lists_available_dates_and_renders_weekend_module(app_url):
    session = "investment-news-digest-days-" + uuid.uuid4().hex
    _browser(session, "open", app_url + "/#/investment-news")
    try:
        result = _browser(session, "eval", "JSON.stringify({tag:document.querySelector('#news-digest-day')?.tagName,"
                  "options:[...document.querySelector('#news-digest-day')?.options||[]].map(option=>option.value),"
                  "summary:document.querySelector('#news-digest')?.textContent})")
        state = json.loads(result["data"]["result"])
        available = _browser(session, "eval", "fetch('/api/investment-news/digest/days').then(r=>r.json())")
        assert state["tag"] == "SELECT"
        assert state["options"] == available["data"]["result"]["days"]
        assert "周末模块 fixture" in state["summary"]
        assert "测试市场来源" in state["summary"]
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _browser(session, "close", allow_failure=True)


def test_monday_today_shortcut_includes_saturday_through_monday(app_url):
    session = "investment-news-monday-window-" + uuid.uuid4().hex
    _browser(session, "open", app_url + "/#/investment-news")
    try:
        _browser(session, "eval", "(()=>{const RealDate=window.Date;window.Date=new Proxy(RealDate,{"
                  "construct(target,args){return Reflect.construct(target,args.length?args:['2026-10-19T04:00:00Z']);}});"
                  "const realFetch=window.fetch.bind(window);window.__newsRequest='';window.fetch=(url,options)=>{"
                  "if(String(url).includes('/api/investment-news?'))window.__newsRequest=String(url);return realFetch(url,options);};"
                  "document.querySelector('[data-news-window=\\\"market-today\\\"]').click();})()")
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 300))")
        request = _browser(session, "eval", "(()=>{const url=new URL(window.__newsRequest,location.origin);"
                  "return JSON.stringify({start:url.searchParams.get('start_day'),end:url.searchParams.get('end_day'),"
                  "day:url.searchParams.get('day'),label:document.querySelector('[data-news-window=\\\"market-today\\\"]').textContent});})()")
        state = json.loads(request["data"]["result"])
        assert state["start"] == "2026-10-17"
        assert state["end"] == "2026-10-19"
        assert state["day"] is None
        assert "周末" in state["label"]

        _browser(session, "click", '[data-news-kind="watchlist"]')
        _browser(session, "click", '[data-news-window="watchlist-today"]')
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 300))")
        watchlist_request = _browser(session, "eval", "(()=>{const url=new URL(window.__newsRequest,location.origin);"
                  "return JSON.stringify({kind:url.searchParams.get('kind'),start:url.searchParams.get('start_day'),"
                  "end:url.searchParams.get('end_day')});})()")
        watchlist_state = json.loads(watchlist_request["data"]["result"])
        assert watchlist_state == {"kind": "watchlist", "start": "2026-10-17", "end": "2026-10-19"}
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _browser(session, "close", allow_failure=True)


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

        _browser(session, "eval", "(()=>{const input=document.querySelector('#news-digest-day');input.value=input.options[1].value;"
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
        _browser(session, "eval", "(()=>{const input=document.querySelector('#news-digest-day');input.value=input.options[1].value;"
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


def test_scheduled_digest_switches_the_open_page_to_the_new_summary_date(app_url):
    session = "news-scheduled-digest-" + uuid.uuid4().hex[:12]
    _browser(session, "open", app_url + "/#/investment-news")
    try:
        dates = _browser(session, "eval", "(()=>{const input=document.querySelector('#news-digest-day');"
               "const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai'}).format(new Date()).split('-').map(Number);"
               "const today=parts.map((n,i)=>String(n).padStart(i===0?4:2,'0')).join('-');"
               "input.value=input.options[1].value;input.dispatchEvent(new Event('change',{bubbles:true}));"
               "return JSON.stringify({today,selected:input.value});})()")
        selected = json.loads(dates["data"]["result"])
        assert selected["today"] != selected["selected"]
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 250))")

        scheduled = _browser(session, "eval", "fetch('/__test__/scheduled-digest',{method:'POST'}).then(r=>r.json())")
        assert scheduled["data"]["result"]["status"] == "succeeded"
        assert scheduled["data"]["result"]["result"]["day"] == selected["today"]
        _browser(session, "eval", "document.dispatchEvent(new Event('visibilitychange'))")
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 500))")
        refreshed = _browser(session, "eval", "JSON.stringify({day:document.querySelector('#news-digest-day')?.value,"
                   "title:document.querySelector('#news-digest-title')?.textContent,"
                   "summary:document.querySelector('#news-digest')?.textContent})")
        result = json.loads(refreshed["data"]["result"])
        assert result["day"] == selected["today"]
        assert result["title"].startswith("今日 AI 综述")
        assert "仅由本地 fixture 生成的综述" in result["summary"]
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _browser(session, "close", allow_failure=True)


def test_scheduled_collection_refreshes_the_open_page_on_visibility_check(app_url):
    session = "news-scheduled-refresh-" + uuid.uuid4().hex[:12]
    _browser(session, "open", app_url + "/#/investment-news")
    try:
        before = _browser(session, "eval", "document.querySelector('#news-list')?.textContent")
        scheduled = _browser(session, "eval", "fetch('/__test__/scheduled-collection',{method:'POST'}).then(r=>r.json())")
        title = scheduled["data"]["result"]["title"]
        assert title not in before["data"]["result"]
        _browser(session, "eval", "document.dispatchEvent(new Event('visibilitychange'))")
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 500))")
        after = _browser(session, "eval", "JSON.stringify({list:document.querySelector('#news-list')?.textContent,"
                  "time:document.querySelector('#news-last-collection-success')?.textContent})")
        result = json.loads(after["data"]["result"])
        assert title in result["list"]
        assert result["time"] != "暂无成功记录"
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _browser(session, "close", allow_failure=True)


def test_symbol_filter_availability_is_explained_for_each_news_kind(app_url):
    session = "news-filter-availability-" + uuid.uuid4().hex[:12]
    _browser(session, "open", app_url + "/#/investment-news")
    try:
        _browser(session, "eval", "new Promise(resolve => setTimeout(resolve, 300))")
        market = _browser(session, "eval", "JSON.stringify({disabled:document.querySelector('#news-symbol-filter').disabled,"
                            "label:document.querySelector('#news-symbol-filter-label').textContent,"
                            "help:document.querySelector('#news-symbol-filter-help').textContent})")
        market_state = json.loads(market["data"]["result"])
        assert market_state["disabled"] is True
        assert "不可用" in market_state["label"]
        assert "市场资讯没有个股关联" in market_state["help"]
        assert "自选股资讯" in market_state["help"]

        _browser(session, "click", '[data-news-kind="watchlist"]')
        watchlist = _browser(session, "eval", "JSON.stringify({disabled:document.querySelector('#news-symbol-filter').disabled,"
                               "label:document.querySelector('#news-symbol-filter-label').textContent,"
                               "help:document.querySelector('#news-symbol-filter-help').textContent})")
        watchlist_state = json.loads(watchlist["data"]["result"])
        assert watchlist_state["disabled"] is False
        assert watchlist_state["label"] == "股票筛选"
        assert "当前共享自选股" in watchlist_state["help"]
        assert _browser(session, "errors")["data"].get("errors", []) == []
    finally:
        _browser(session, "close", allow_failure=True)


def test_watchlist_filter_and_sort_controls_have_deliberate_rows(app_url):
    session = "news-sort-layout-" + uuid.uuid4().hex[:12]
    _browser(session, "open", app_url + "/#/investment-news")
    _browser(session, "set", "viewport", "1280", "900")
    try:
        _browser(session, "click", '[data-news-kind="watchlist"]')
        layout = _browser(session, "eval", "(()=>{const rect=s=>{const r=document.querySelector(s).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}};"
                  "return JSON.stringify({primary:rect('.news-primary-control-row'),secondary:rect('.news-secondary-control-row'),"
                  "symbol:rect('#news-symbol-filter'),sort:rect('#news-sort')})})()")
        metrics = json.loads(layout["data"]["result"])
        assert metrics["primary"]["y"] < metrics["secondary"]["y"]
        assert abs(metrics["symbol"]["y"] - metrics["sort"]["y"]) < 2
        _browser(session, "set", "viewport", "390", "844")
        mobile = _browser(session, "eval", "JSON.stringify({viewport:innerWidth,document:document.documentElement.scrollWidth})")
        mobile_width = json.loads(mobile["data"]["result"])
        assert mobile_width["document"] == mobile_width["viewport"]
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
