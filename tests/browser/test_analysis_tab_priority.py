"""动态分析主题 Tab 优先级的真实应用浏览器回归。"""

import base64
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
LAUNCHER = Path(__file__).resolve().parent / "visual_test_app.py"
TEARDOWN_GRACE_SECONDS = 20
TEARDOWN_KILL_SECONDS = 10
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from webapp.browser_preflight import find_usable_agent_browser

_AGENT_BROWSER_CANDIDATES = (
    os.environ.get("AGENT_BROWSER"),
    shutil.which("agent-browser"),
    str(Path.home() / ".pi/agent/npm/node_modules/.bin/agent-browser"),
)
AGENT_BROWSER = find_usable_agent_browser(_AGENT_BROWSER_CANDIDATES)
pytestmark = pytest.mark.skipif(
    AGENT_BROWSER is None,
    reason="agent-browser CLI 不可用，无法执行真实应用浏览器回归",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_health(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("真实应用服务在浏览器验收前退出")
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    pytest.fail("真实应用服务在 15 秒内未就绪")


@pytest.fixture
def actual_app_url():
    """运行关闭 RAG 和外部数据源的实际 FastAPI 应用。"""
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(LAUNCHER), str(port)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(url, process)
        yield url
    finally:
        process.terminate()
        try:
            process.communicate(timeout=TEARDOWN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.communicate(timeout=TEARDOWN_KILL_SECONDS)
            except subprocess.TimeoutExpired:
                pytest.fail("真实应用服务在 SIGKILL 后仍未退出")


@pytest.fixture
def browser_session(actual_app_url):
    session = f"analysis-tab-priority-{uuid.uuid4().hex}"
    _run_browser(session, "open", actual_app_url + "/#/analysis")
    try:
        yield session
    finally:
        _run_browser(session, "close", allow_failure=True)


def _run_browser(session: str, command: str, *args: str, allow_failure: bool = False) -> dict:
    result = subprocess.run(
        [AGENT_BROWSER, "--session", session, "--json", command, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode and not allow_failure:
        pytest.fail(f"agent-browser {command} 失败：{result.stderr or result.stdout}")
    if not result.stdout.strip():
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        if allow_failure:
            return {}
        pytest.fail(f"agent-browser {command} 返回非 JSON：{result.stdout}")
    if not payload.get("success", False) and not allow_failure:
        pytest.fail(f"agent-browser {command} 失败：{payload.get('error')}")
    return payload


def _eval(session: str, script: str):
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    payload = _run_browser(session, "eval", "--base64", encoded)
    return json.loads(payload["data"]["result"])


def _v3_fixture() -> dict:
    """历史 v3 报告刻意缺失 tab_label，禁止测试依赖新报告或 AI。"""
    return {
        "schema_version": 3,
        "stage": "completed",
        "activeTab": "cash",
        "quick": {"conclusions": []},
        "sections": [
            {
                "section_id": "cash",
                "title": "经营现金流净流入显著改善，偿债能力增强",
                "summary": "经营活动现金流持续改善。",
                "findings": [{"claim": "经营现金流为正。"}],
                "score": {
                    "evidence_sufficiency": 5,
                    "source_reliability": 5,
                    "materiality": 5,
                    "clarity": 5,
                    "interest_relevance": 5,
                },
            },
            {
                "section_id": "profit",
                "title": "盈利质量保持稳健，利润结构持续优化",
                "summary": "利润结构改善。",
                "findings": [{"claim": "净利润稳健。"}, {"claim": "毛利率改善。"}],
                "score": {
                    "evidence_sufficiency": 4,
                    "source_reliability": 4,
                    "materiality": 4,
                    "clarity": 3,
                    "interest_relevance": 3,
                },
            },
            {
                "section_id": "risk",
                "title": "风险事项仍需持续跟踪，减值压力存在",
                "summary": "减值风险需关注。",
                "findings": [{"claim": "应收款减值压力上升。"}],
                "score": {
                    "evidence_sufficiency": 2,
                    "source_reliability": 2,
                    "materiality": 2,
                    "clarity": 2,
                    "interest_relevance": 2,
                },
            },
        ],
        "evidence_catalog": {},
    }


def _render_fixture_script() -> str:
    fixture = _v3_fixture()
    assert all("tab_label" not in section for section in fixture["sections"])
    return f"""
(() => {{
  const fixture = {json.dumps(fixture, ensure_ascii=False)};
  STATE.selected = {{code: '600900', name: '离线测试公司'}};
  STATE.selectedReport = {{period: '2026-06-30'}};
  STATE.analysisCache['600900:2026-06-30'] = {{status: 'done', data: fixture}};
  renderAnalysisPanel('600900:2026-06-30');
  const root = document.querySelector('#analyze-result');
  const tabs = Array.from(root.querySelectorAll('[data-analysis-tab]'));
  return JSON.stringify({{
    routeVisible: !document.querySelector('#page-analysis').classList.contains('hidden'),
    tabIds: tabs.map(tab => tab.dataset.analysisTab),
    labels: tabs.slice(1).map(tab => tab.childNodes[0].textContent.trim()),
    importance: tabs.slice(1).map(tab => tab.querySelector('.analysis-tab-importance').textContent),
    ariaLabels: tabs.slice(1).map(tab => tab.getAttribute('aria-label')),
    titleAttributes: tabs.slice(1).map(tab => tab.getAttribute('title')),
    activeTab: root.querySelector('[aria-selected="true"]').dataset.analysisTab,
    heading: root.querySelector('.analysis-progressive-body h3').textContent,
    summary: root.querySelector('.analysis-section-summary').textContent,
    overflow: document.documentElement.scrollWidth > window.innerWidth
  }});
}})()
"""


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_v3_analysis_tabs_keep_priority_and_accessibility_in_actual_application(
    browser_session, actual_app_url, viewport
):
    """真实页面须兼容缺失短标签的 v3 fixture，并在桌面和手机宽度保持可用。"""
    _run_browser(browser_session, "set", "viewport", str(viewport[0]), str(viewport[1]))
    _run_browser(browser_session, "open", f"{actual_app_url}/#/analysis")
    rendered = _eval(browser_session, _render_fixture_script())

    assert rendered["routeVisible"] is True
    assert rendered["tabIds"] == ["quick", "cash", "profit", "risk"]
    assert rendered["labels"] == ["02 现金流（1）", "03 盈利结构（2）", "04 风险事项（1）"]
    assert rendered["importance"] == ["高", "中", "常"]
    assert rendered["ariaLabels"] == [
        "经营现金流净流入显著改善，偿债能力增强，重要程度：高，发现 1 条",
        "盈利质量保持稳健，利润结构持续优化，重要程度：中，发现 2 条",
        "风险事项仍需持续跟踪，减值压力存在，重要程度：一般，发现 1 条",
    ]
    assert rendered["titleAttributes"] == [
        "经营现金流净流入显著改善，偿债能力增强",
        "盈利质量保持稳健，利润结构持续优化",
        "风险事项仍需持续跟踪，减值压力存在",
    ]
    assert rendered["activeTab"] == "cash"
    assert rendered["heading"] == "经营现金流净流入显著改善，偿债能力增强"
    assert rendered["summary"] == "经营活动现金流持续改善。"
    assert rendered["overflow"] is False
