"""真实 FastAPI + agent-browser 的补报授权流验收，所有协作方均为本地 fake。"""

import base64
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
LAUNCHER = Path(__file__).resolve().parent / "visual_test_app.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from webapp.browser_preflight import find_usable_agent_browser  # noqa: E402

AGENT_BROWSER = find_usable_agent_browser((
    os.environ.get("AGENT_BROWSER"), shutil.which("agent-browser"),
    str(Path.home() / ".pi/agent/npm/node_modules/.bin/agent-browser"),
))
pytestmark = pytest.mark.skipif(AGENT_BROWSER is None, reason="agent-browser CLI 不可用，无法执行真实应用浏览器验收")


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_health(url, process):
    import urllib.request
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("真实应用服务在浏览器验收前退出")
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(.2)
    pytest.fail("真实应用服务在 15 秒内未就绪")


@pytest.fixture
def actual_app_url(tmp_path):
    """真实应用进程；协作方日志只写入 pytest 临时目录。"""
    port, request_log = _free_port(), tmp_path / "fixture-requests.json"
    process = subprocess.Popen(
        [sys.executable, str(LAUNCHER), str(port)], cwd=ROOT,
        env=dict(os.environ, BROWSER_FIXTURE_LOG=str(request_log)),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(url, process)
        yield url, request_log
    finally:
        process.terminate()
        try:
            process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=10)


def _run_browser(session, command, *args):
    result = subprocess.run([AGENT_BROWSER, "--session", session, "--json", command, *args], cwd=ROOT, capture_output=True, text=True, timeout=20)
    if result.returncode:
        pytest.fail(f"agent-browser {command} 失败：{result.stderr or result.stdout}")
    payload = json.loads(result.stdout)
    if not payload.get("success", False):
        pytest.fail(f"agent-browser {command} 失败：{payload.get('error')}")
    return payload


def _eval(session, script):
    encoded = base64.b64encode(script.encode()).decode()
    return json.loads(_run_browser(session, "eval", "--base64", encoded)["data"]["result"])


def _data(session, command, *args):
    return _run_browser(session, command, *args).get("data") or {}


def _start_observing(session):
    _run_browser(session, "console", "--clear")
    _run_browser(session, "errors", "--clear")


def _assert_clean_browser(session):
    messages = _data(session, "console").get("messages") or []
    assert [message.get("text") for message in messages if message.get("type") == "error"] == []
    assert (_data(session, "errors").get("errors") or []) == []


def _assert_fixture_only(request_log, *, downloaded):
    entries = json.loads(request_log.read_text(encoding="utf-8"))
    assert entries, "fixture must record injected collaborator calls"
    assert all(str(entry.get("url", "")).startswith("fixture://") for entry in entries)
    assert not any("cninfo" in str(entry).lower() or "http" in str(entry).lower() for entry in entries)
    kinds = [entry["kind"] for entry in entries]
    assert ("download" in kinds) is downloaded
    assert ("ingest" in kinds) is downloaded


@pytest.fixture
def browser_session(actual_app_url):
    session = f"chat-supplement-{uuid.uuid4().hex}"
    _run_browser(session, "open", actual_app_url[0] + "/#/chat")
    try:
        yield session
    finally:
        subprocess.run([AGENT_BROWSER, "--session", session, "--json", "close"], cwd=ROOT, capture_output=True, text=True, timeout=15)


def _open_supplement_card(session):
    return _eval(session, """
(async () => {
  await submitQuestion('601288 补报验收：请核对 2025 半年报经营现金流', chatStreamKey());
  const deadline = Date.now() + 10000;
  while (!document.querySelector('.chat-supplement-consent') && Date.now() < deadline) await new Promise(r => setTimeout(r, 50));
  const card = document.querySelector('.chat-supplement-consent'), boxes = card ? [...card.querySelectorAll('input[type=checkbox]')] : [], approve = card && card.querySelector('.chat-supplement-approve');
  return JSON.stringify({exists: !!card, count: boxes.length, checked: boxes.filter(box => box.checked).length, approveDisabled: approve ? approve.disabled : null, approveText: approve ? approve.textContent : '', live: card ? card.getAttribute('aria-live') : '', label: card ? card.getAttribute('aria-label') : ''});
})()
""")


def test_supplement_needed_renders_default_unchecked_max_five(browser_session, actual_app_url):
    """真实 supplement_needed 默认全未选、至多五项，且不触发下载。"""
    _start_observing(browser_session)
    rendered = _open_supplement_card(browser_session)
    assert rendered["exists"] is True
    assert 1 <= rendered["count"] <= 5
    assert rendered["checked"] == 0
    assert rendered["approveDisabled"] is True
    assert "0/5" in rendered["approveText"]
    assert rendered["live"] == "polite" and rendered["label"] == "补充财报授权"
    _assert_fixture_only(actual_app_url[1], downloaded=False)
    _assert_clean_browser(browser_session)


def test_supplement_selection_cap_shows_count_and_blocks_sixth(browser_session, actual_app_url):
    """客户端即使接到六项也最多选择五项，并暴露 N/5。"""
    payload = _eval(browser_session, """
(() => {
  const host = document.querySelector('#chat-history'), candidates = Array.from({length: 6}, (_, i) => ({id: 'candidate-' + i, label: '验收报告 ' + (i + 1)}));
  const consent = renderSupplementConsent(host, {limit: 5, candidates}, {approve: () => {}, decline: () => {}}), boxes = [...consent.card.querySelectorAll('input[type=checkbox]')];
  boxes.forEach(box => box.click());
  const approve = consent.card.querySelector('.chat-supplement-approve');
  return JSON.stringify({checked: boxes.filter(box => box.checked).length, sixthChecked: boxes[5].checked, approveText: approve.textContent, approveDisabled: approve.disabled});
})()
""")
    assert payload["checked"] == 5 and payload["sixthChecked"] is False
    assert payload["approveDisabled"] is False and "5/5" in payload["approveText"]


def test_supplement_keyboard_order_reaches_candidates_decline_then_approve(browser_session, actual_app_url):
    """真实键盘 Tab 顺序与候选、拒绝、批准的可见顺序一致。"""
    _open_supplement_card(browser_session)
    first = _eval(browser_session, """(() => { const box = document.querySelector('.chat-supplement-consent input[type=checkbox]'); box.focus(); return JSON.stringify({tag: document.activeElement.tagName, label: document.activeElement.getAttribute('aria-label')}); })()""")
    assert first["tag"] == "INPUT" and first["label"].startswith("选择 ")
    _eval(browser_session, """(() => { const boxes = [...document.querySelectorAll('.chat-supplement-consent input[type=checkbox]')]; boxes[0].click(); boxes[boxes.length - 1].focus(); return JSON.stringify(true); })()""")
    _run_browser(browser_session, "press", "Tab")
    decline = _eval(browser_session, "JSON.stringify(document.activeElement.className)")
    _run_browser(browser_session, "press", "Tab")
    approve = _eval(browser_session, "JSON.stringify(document.activeElement.className)")
    assert "chat-supplement-decline" in decline
    assert "chat-supplement-approve" in approve


def test_decline_uses_no_downloader_and_renders_honest_existing_evidence_answer(browser_session, actual_app_url):
    """拒绝零下载，且恢复回答如实表明只使用现有信息。"""
    _start_observing(browser_session)
    _open_supplement_card(browser_session)
    rendered = _eval(browser_session, """
(async () => {
  document.querySelector('.chat-supplement-decline').click(); const deadline = Date.now() + 10000;
  while (document.querySelector('.chat-supplement-consent') && Date.now() < deadline) await new Promise(r => setTimeout(r, 50));
  return JSON.stringify({cardGone: !document.querySelector('.chat-supplement-consent'), text: document.querySelector('#chat-history').textContent});
})()
""")
    assert rendered["cardGone"] is True
    assert "未补充财报，已基于现有信息回答" in rendered["text"]
    _assert_fixture_only(actual_app_url[1], downloaded=False)
    _assert_clean_browser(browser_session)


def test_approve_shows_safe_progress_and_resumed_pdf_source(browser_session, actual_app_url):
    """批准后显示下载/索引进度，最终显示恢复回答及 PDF 来源。"""
    _start_observing(browser_session)
    _open_supplement_card(browser_session)
    rendered = _eval(browser_session, """
(async () => {
  const card = document.querySelector('.chat-supplement-consent'), seen = [], status = card.querySelector('.chat-supplement-status');
  const observer = new MutationObserver(() => { if (status.textContent) seen.push(status.textContent); }); observer.observe(status, {childList: true, characterData: true, subtree: true});
  card.querySelector('input[type=checkbox]').click(); card.querySelector('.chat-supplement-approve').click(); const deadline = Date.now() + 10000;
  while (document.querySelector('.chat-supplement-consent') && Date.now() < deadline) await new Promise(r => setTimeout(r, 50));
  observer.disconnect();
  return JSON.stringify({cardGone: !document.querySelector('.chat-supplement-consent'), progress: seen, text: document.querySelector('#chat-history').textContent, pdfText: [...document.querySelectorAll('#chat-history .chat-pdf-page')].map(node => node.textContent)});
})()
""")
    assert rendered["cardGone"] is True
    assert any("下载" in message for message in rendered["progress"])
    assert any("索引" in message for message in rendered["progress"])
    assert "已基于补充财报原文恢复回答" in rendered["text"]
    assert "PDF · 第 40 页" in rendered["pdfText"]
    _assert_fixture_only(actual_app_url[1], downloaded=True)
    _assert_clean_browser(browser_session)


@pytest.mark.parametrize("viewport", [(1280, 900), (768, 1000), (390, 844)])
def test_supplement_consent_has_no_horizontal_overflow_or_browser_errors(browser_session, actual_app_url, viewport):
    """桌面、平板、390x844 移动视口没有横向溢出、console 或 page error。"""
    _run_browser(browser_session, "set", "viewport", str(viewport[0]), str(viewport[1]))
    _start_observing(browser_session)
    _open_supplement_card(browser_session)
    layout = _eval(browser_session, """(() => JSON.stringify({scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth, card: !!document.querySelector('.chat-supplement-consent')}))()""")
    assert layout["card"] is True
    assert layout["scrollWidth"] <= layout["innerWidth"]
    _assert_clean_browser(browser_session)
