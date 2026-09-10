"""财务结构可视化的真实应用浏览器回归测试。"""

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
_AGENT_BROWSER_CANDIDATES = (
    os.environ.get("AGENT_BROWSER"),
    shutil.which("agent-browser"),
    str(Path.home() / ".pi/agent/npm/node_modules/.bin/agent-browser"),
)
AGENT_BROWSER = next(
    (candidate for candidate in _AGENT_BROWSER_CANDIDATES if candidate and Path(candidate).is_file()),
    None,
)
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
    """每项浏览器测试都访问实际 FastAPI 应用，而非 file:// 或 mock 页面。"""
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "webapp.server:app", "--host", "127.0.0.1", "--port", str(port)],
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
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            pytest.fail("真实应用服务在终止后仍未退出")


@pytest.fixture
def browser_session(actual_app_url):
    session = f"financial-visuals-{uuid.uuid4().hex}"
    _run_browser(session, "open", actual_app_url)
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
    result = payload["data"]["result"]
    return json.loads(result)


def _visualization_script() -> str:
    return """
(() => {
  const catalog = {pdf: {source_type: 'pdf_text', source_locator: {page: 12}}};
  const cards = [
    {id: 'profit_structure', topic_id: 'profit', title: '利润结构', kind: 'profit', status: 'complete', rows: [
      {metric_id: 'revenue', label: '营业收入', value: 120, unit: '亿元', direction: 'neutral', evidence_ids: ['pdf']},
      {metric_id: 'net_profit', label: '净利润', value: 12, unit: '亿元', direction: 'neutral', evidence_ids: ['pdf']}
    ]},
    {id: 'balance_sheet_structure', topic_id: 'balance', title: '资产负债结构', kind: 'balance_sheet', status: 'complete', rows: [
      {metric_id: 'total_assets', label: '总资产', value: 500, unit: '亿元', direction: 'neutral', evidence_ids: ['pdf']},
      {metric_id: 'total_liabilities', label: '总负债', value: 360, unit: '亿元', direction: 'neutral', evidence_ids: ['pdf']},
      {metric_id: 'total_equity', label: '所有者权益', value: 140, unit: '亿元', direction: 'neutral', evidence_ids: ['pdf']}
    ]},
    {id: 'cash_flow_structure', topic_id: 'cash', title: '现金流结构', kind: 'cash_flow', status: 'partial', rows: [
      {metric_id: 'operating_cash_flow', label: '经营活动现金流净额', value: 22, unit: '亿元', direction: 'inflow', evidence_ids: ['pdf']},
      {metric_id: 'investing_cash_flow', label: '投资活动现金流净额', value: -8, unit: '亿元', direction: 'outflow', evidence_ids: ['pdf']}
    ]},
    {id: 'unavailable_structure', topic_id: 'unavailable', title: '暂不可用结构', kind: 'profit', status: 'unavailable', unavailable_reason: '披露不足', rows: []}
  ];
  const root = document.querySelector('#analyze-result');
  root.innerHTML = AnalysisVisualizations.renderSlots({version: 1, cards}, [
    {section_id: 'profit'}, {section_id: 'balance'}, {section_id: 'cash'}, {section_id: 'unavailable'}
  ], catalog);
  let evidencePage = null;
  AnalysisVisualizations.mount(root, {version: 1, cards}, {onEvidencePage(page) { evidencePage = page; }});
  root.querySelector('.analysis-visualization-evidence').click();
  return JSON.stringify({
    headings: Array.from(root.querySelectorAll('h3')).map(node => node.textContent),
    tables: root.querySelectorAll('table').length,
    canvases: root.querySelectorAll('canvas').length,
    unavailableCanvas: root.querySelector('[aria-label="暂不可用结构"]').querySelectorAll('canvas').length,
    evidencePage,
    overflow: document.documentElement.scrollWidth > window.innerWidth
  });
})()
"""


@pytest.mark.parametrize("viewport", [(1280, 800), (768, 900), (390, 844)])
def test_v4_visualizations_render_in_actual_application(browser_session, viewport):
    """默认 pytest 在 agent-browser 可用时真实访问应用，并验证桌面到手机布局。"""
    _run_browser(browser_session, "set", "viewport", str(viewport[0]), str(viewport[1]))
    rendered = _eval(browser_session, _visualization_script())

    assert rendered["headings"] == ["利润结构", "资产负债结构", "现金流结构", "暂不可用结构"]
    assert rendered["tables"] == 3
    assert rendered["canvases"] == 3
    assert rendered["unavailableCanvas"] == 0
    assert rendered["evidencePage"] == 12
    assert not rendered["overflow"]


def test_actual_app_lifecycle_mounts_v4_routes_pdf_and_cleans_up_for_v3(browser_session):
    """真实 app.js 主/历史路径必须支持 v4，并在切回 v3 时回收图表。"""
    rendered = _eval(browser_session, """
(() => {
  const catalog = {pdf: {source_type: 'pdf_text', source_locator: {page: 12}}};
  const section = {section_id: 'cash', title: '现金流', findings: [{content: '正文', evidence_ids: []}]};
  const v4 = {schema_version: 4, stage: 'completed', activeTab: 'cash', quick: {conclusions: []}, sections: [section], evidence_catalog: catalog, visualizations: {version: 1, cards: [{
    id: 'cash_flow_structure', topic_id: 'cash', title: '现金流结构', kind: 'cash_flow', status: 'partial', rows: [
      {metric_id: 'operating_cash_flow', label: '经营活动现金流净额', value: 22, unit: '亿元', direction: 'inflow', evidence_ids: ['pdf']},
      {metric_id: 'investing_cash_flow', label: '投资活动现金流净额', value: -8, unit: '亿元', direction: 'outflow', evidence_ids: ['pdf']}
    ]
  }]}};
  const v3 = {schema_version: 3, stage: 'completed', activeTab: 'cash', quick: {conclusions: []}, sections: [section], evidence_catalog: catalog};
  STATE.selected = {code: '600900'};
  STATE.selectedReport = {period: '2026-06-30'};
  STATE.analysisCache['600900:2026-06-30'] = {status: 'done', data: v4};
  let mainPage = null;
  document.querySelector('#analyze-result').addEventListener('analysis:evidence-page', event => { mainPage = event.detail.page; });
  renderAnalysisPanel('600900:2026-06-30');
  document.querySelector('#analyze-result .analysis-visualization-evidence').click();
  STATE.historySelected = {code: '600900', period: '2026-06-30', pdf_filename: 'report.pdf'};
  renderAnalysisInDetail('示例公司', '600900', '2026-06-30', '2026', v4, 'fixture');
  document.querySelector('#history-detail .analysis-visualization-evidence').click();
  const historyPdf = document.querySelector('#history-pdf-frame').src;
  renderAnalysisInDetail('示例公司', '600900', '2026-06-30', '2026', v3, 'fixture');
  return JSON.stringify({
    mainPage, historyPdf,
    legacy: document.querySelector('#history-detail').textContent.includes('重新分析后可生成结构图'),
    oldCanvas: document.querySelector('#history-detail canvas') !== null
  });
})()
""")

    assert rendered["mainPage"] == 12
    assert rendered["historyPdf"].endswith("#page=12")
    assert "?jump=" in rendered["historyPdf"]
    assert rendered["legacy"] is True
    assert rendered["oldCanvas"] is False
