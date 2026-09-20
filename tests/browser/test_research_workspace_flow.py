"""M4 research-workspace browser acceptance against only deterministic local fixtures.

The real FastAPI app is launched through ``visual_test_app``.  That launcher owns
all fixture records and uses no AI, MCP, RAG ingestion, web, or production data.
"""

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
from webapp.browser_preflight import find_usable_agent_browser  # noqa: E402

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
    """Each test launches the actual app with isolated deterministic fixture state."""
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
    session = f"research-workspace-{uuid.uuid4().hex}"
    _run_browser(session, "open", actual_app_url + "/#/chat")
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


def _data(session: str, command: str, *args: str) -> dict:
    return _run_browser(session, command, *args).get("data") or {}


def _intercept_native_download(session: str) -> None:
    """Prevent fixture exports from reaching the browser's real download directory."""
    _eval(session, """
(() => {
  const originalClick = HTMLAnchorElement.prototype.click;
  window.nativeDownloads = [];
  HTMLAnchorElement.prototype.click = function () {
    window.nativeDownloads.push({ href: this.href, download: this.download });
  };
  window.restoreNativeDownload = function () {
    HTMLAnchorElement.prototype.click = originalClick;
    delete window.restoreNativeDownload;
  };
  return JSON.stringify(true);
})()
""")


def _restore_native_download(session: str) -> list[dict]:
    return _eval(session, """
(() => {
  const downloads = window.nativeDownloads || [];
  if (window.restoreNativeDownload) window.restoreNativeDownload();
  delete window.nativeDownloads;
  return JSON.stringify(downloads);
})()
""")


def _start_observing(session: str) -> None:
    _run_browser(session, "console", "--clear")
    _run_browser(session, "errors", "--clear")
    _run_browser(session, "network", "requests", "--clear")


def _console_errors(session: str) -> list:
    return [
        message.get("text") for message in _data(session, "console").get("messages") or []
        if message.get("type") == "error"
    ]


def _page_errors(session: str) -> list:
    return _data(session, "errors").get("errors") or []


def _failed_requests(session: str, origin: str) -> list:
    return [
        {"url": request.get("url"), "status": request.get("status")}
        for request in _data(session, "network", "requests").get("requests") or []
        if str(request.get("url") or "").startswith(origin)
        and isinstance(request.get("status"), int)
        and not str(request.get("url")).endswith("/favicon.ico")
        and not 200 <= request["status"] < 400
    ]


def _workspace_script(company: str = "", status: str = "", favorite_only: bool = False, *, delay_initial: bool = False) -> str:
    return f"""
(async () => {{
  const originalFetch = window.fetch.bind(window);
  let delayedInitial = false;
  window.fetch = async (...args) => {{
    const response = await originalFetch(...args);
    const url = String(args[0] || '');
    if ({str(delay_initial).lower()} && !delayedInitial && url.includes('/api/research/workspace?') && !url.includes('status=')) {{
      delayedInitial = true;
      await new Promise(resolve => setTimeout(resolve, 400));
    }}
    return response;
  }};
  const toggle = document.querySelector('#research-workspace-toggle');
  if (document.querySelector('#research-workspace').classList.contains('hidden')) toggle.click();
  document.querySelector('#research-filter-company').value = {json.dumps(company)};
  document.querySelector('#research-filter-status').value = {json.dumps(status)};
  document.querySelector('#research-filter-favorite').checked = {str(favorite_only).lower()};
  document.querySelector('#research-filter-apply').click();
  const deadline = Date.now() + 10000;
  while (!document.querySelector('#research-workspace-status').textContent.includes('已显示')
      && Date.now() < deadline) {{
    await new Promise(resolve => setTimeout(resolve, 100));
  }}
  if ({str(delay_initial).lower()}) await new Promise(resolve => setTimeout(resolve, 450));
  window.fetch = originalFetch;
  return JSON.stringify({{
    visible: !document.querySelector('#research-workspace').classList.contains('hidden'),
    status: document.querySelector('#research-workspace-status').textContent,
    items: [...document.querySelectorAll('.research-workspace-item')].map(item => ({{
      runId: item.dataset.researchRunId, text: item.textContent,
      favorite: item.querySelector('[data-research-action="favorite"]').getAttribute('aria-pressed')
    }}))
  }});
}})()
"""


def test_workspace_filters_by_company_and_status(browser_session, actual_app_url):
    """The workbench filters persisted scope/status metadata, not fixture titles."""
    _start_observing(browser_session)
    rendered = _eval(browser_session, _workspace_script(company="601288", status="completed"))

    assert rendered["visible"] is True
    assert len(rendered["items"]) == 1
    assert rendered["items"][0]["runId"] == "fixture-completed-run"
    assert "状态：completed" in rendered["items"][0]["text"]
    assert "证据：可用" in rendered["items"][0]["text"]
    favorite = _eval(browser_session, """
(async () => {
  const card = document.querySelector('[data-research-run-id="fixture-completed-run"]');
  card.querySelector('[data-research-action="favorite"]').click();
  const deadline = Date.now() + 10000;
  while (document.querySelector('[data-research-run-id="fixture-completed-run"] [data-research-action="favorite"]').getAttribute('aria-pressed') !== 'true' && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  document.querySelector('#research-filter-favorite').checked = true;
  document.querySelector('#research-filter-apply').click();
  while (document.querySelectorAll('.research-workspace-item').length !== 1 && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return JSON.stringify([...document.querySelectorAll('.research-workspace-item')].map(item => item.dataset.researchRunId));
})()
""")
    assert favorite == ["fixture-completed-run"]
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
    assert _failed_requests(browser_session, actual_app_url) == []


def test_workspace_filter_ignores_stale_unfiltered_response(browser_session, actual_app_url):
    """A late unfiltered request must not overwrite the user's latest filter result."""
    _start_observing(browser_session)
    rendered = _eval(
        browser_session,
        _workspace_script(company="601288", status="completed", delay_initial=True),
    )

    assert [item["runId"] for item in rendered["items"]] == ["fixture-completed-run"]
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
    assert _failed_requests(browser_session, actual_app_url) == []


def test_workspace_has_no_removed_memory_or_decision_entry(browser_session, actual_app_url):
    """The live workbench exposes none of the removed persistence entry points."""
    _start_observing(browser_session)
    _eval(browser_session, _workspace_script())
    rendered = _eval(browser_session, """
(() => {
  const removedMemory = 'research' + '-memory';
  const removedDecision = 'save' + '-decision';
  return JSON.stringify({
    panel: !!document.querySelector('.' + removedMemory + '-panel'),
    factAction: !!document.querySelector('[data-' + removedMemory + '-kind]'),
    decisionAction: !!document.querySelector('[data-research-action="' + removedDecision + '"]'),
  });
})()
""")

    assert rendered == {"panel": False, "factAction": False, "decisionAction": False}
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
    assert _failed_requests(browser_session, actual_app_url) == []


def test_delete_keeps_fixture_pdf_available(browser_session, actual_app_url):
    """Deleting a session keeps its source PDF available without a persistence sidecar."""
    _start_observing(browser_session)
    rendered = _eval(browser_session, """
(async () => {
  const workspace = await fetch('/api/research/workspace');
  const completed = (await workspace.json()).items.find(item => item.run_id === 'fixture-completed-run');
  await openChatSession(completed.session_id);
  window.confirm = () => true;
  await deleteChatSession(completed.session_id);
  const deadline = Date.now() + 10000;
  while (document.body.textContent.indexOf('会话已删除') < 0 && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const pdf = await fetch('/api/history-pdf/' + encodeURIComponent('农业银行_601288_半年报_2026.pdf'));
  return JSON.stringify({ text: document.body.textContent, pdfStatus: pdf.status });
})()
""")

    assert "会话已删除，原始 PDF 不会被删除。" in rendered["text"]
    assert rendered["pdfStatus"] == 200
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
    assert _failed_requests(browser_session, actual_app_url) == []


def test_export_contains_scope_status_and_pdf_page_link(browser_session, actual_app_url):
    """The browser action exports the immutable run and keeps its review contract."""
    _start_observing(browser_session)
    rendered = _eval(browser_session, _workspace_script())
    assert any(item["runId"] == "fixture-completed-run" for item in rendered["items"])
    _intercept_native_download(browser_session)
    exported = _eval(browser_session, """
(async () => {
  const card = document.querySelector('[data-research-run-id="fixture-completed-run"]');
  card.querySelector('[data-research-format="markdown"]').click();
  const deadline = Date.now() + 10000;
  while (!document.querySelector('.research-export-state') && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const response = await fetch('/api/research/runs/fixture-completed-run/export?session_id=' + encodeURIComponent(card.dataset.researchSessionId) + '&format=markdown');
  return JSON.stringify({ status: document.querySelector('#research-workspace-status').textContent, text: await response.text() });
})()
""")

    native_downloads = _restore_native_download(browser_session)
    assert len(native_downloads) == 1
    assert native_downloads[0]["download"] == "research-fixture-completed-run.md"
    assert native_downloads[0]["href"].startswith("blob:")
    assert "导出完成" in exported["status"]
    assert "## 范围" in exported["text"]
    assert "## 运行状态" in exported["text"]
    assert "PDF 第 40 页" in exported["text"]
    assert "数据截至" in exported["text"]
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
    assert _failed_requests(browser_session, actual_app_url) == []


@pytest.mark.parametrize("viewport", [(1280, 900), (768, 1000), (390, 844)])
def test_quality_status_preserves_workspace_filters(browser_session, actual_app_url, viewport):
    """Quality status remains safe while the filtered workbench fits every required viewport."""
    _run_browser(browser_session, "set", "viewport", str(viewport[0]), str(viewport[1]))
    _start_observing(browser_session)
    rendered = _eval(browser_session, r"""
(async () => {
  const originalFetch = window.fetch.bind(window);
  const safeQuality = {
    available: true,
    health: {passed: true, case_name: 'scope-leak-case'},
    probe: {passed: true, failure_prose: 'scope-leak-case'}
  };
  window.fetch = async (...args) => {
    if (String(args[0] || '').includes('/api/research/quality')) {
      return new Response(JSON.stringify(safeQuality), {
        status: 200, headers: {'Content-Type': 'application/json'}
      });
    }
    return originalFetch(...args);
  };
  const deadline = () => Date.now() + 10000;
  const toggle = document.querySelector('#research-workspace-toggle');
  if (document.querySelector('#research-workspace').classList.contains('hidden')) toggle.click();
  document.querySelector('#research-filter-company').value = '601288';
  document.querySelector('#research-filter-status').value = 'completed';
  document.querySelector('#research-filter-apply').click();
  const until = deadline();
  while ((!document.querySelector('#research-workspace-status').textContent.includes('已显示')
      || !document.querySelector('#research-quality-status').textContent.includes('健康评测'))
      && Date.now() < until) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  window.fetch = originalFetch;
  return JSON.stringify({
    company: document.querySelector('#research-filter-company').value,
    status: document.querySelector('#research-filter-status').value,
    quality: document.querySelector('#research-quality-status').textContent,
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  });
})()
""")

    assert rendered["company"] == "601288"
    assert rendered["status"] == "completed"
    assert "健康评测：通过" in rendered["quality"]
    assert "负向探针：通过" in rendered["quality"]
    assert "scope-leak-case" not in rendered["quality"]
    assert rendered["scrollWidth"] <= rendered["innerWidth"]
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
    assert _failed_requests(browser_session, actual_app_url) == []


@pytest.mark.parametrize("viewport", [(1280, 900), (768, 1000), (390, 844)])
def test_workspace_mobile_has_no_horizontal_overflow(browser_session, actual_app_url, viewport):
    """Desktop, tablet, and the required 390px mobile workbench have no overflow."""
    _run_browser(browser_session, "set", "viewport", str(viewport[0]), str(viewport[1]))
    _start_observing(browser_session)
    rendered = _eval(browser_session, _workspace_script())
    geometry = _eval(browser_session, "JSON.stringify({scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth})")

    assert rendered["visible"] is True
    assert geometry["scrollWidth"] <= geometry["innerWidth"], (
        f"{viewport} 工作台横向溢出：{geometry['scrollWidth']} > {geometry['innerWidth']}"
    )
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
    assert _failed_requests(browser_session, actual_app_url) == []


def test_run_reuse_actions_keep_history_and_branch_into_a_new_session(browser_session, actual_app_url):
    """编辑重问只回填问题；分支追问用既有端点新建会话，不复制证据、不改写历史。"""
    _start_observing(browser_session)
    rendered = _eval(browser_session, """
(async () => {
  const deadline = () => Date.now() + 10000;
  const items = (await (await fetch('/api/research/workspace')).json()).items;
  const completed = items.find(item => item.run_id === 'fixture-completed-run');
  await openChatSession(completed.session_id);
  let until = deadline();
  while (!document.querySelector('[data-chat-action="edit-reask"]') && Date.now() < until) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const question = document.querySelector('#chat-history .chat-msg.user').textContent.trim();
  document.querySelector('[data-chat-action="edit-reask"]').click();
  const inputAfterEdit = document.querySelector('#chat-input').value;
  const editNotice = document.body.textContent.indexOf('已回填该轮原始问题') >= 0;
  document.querySelector('[data-chat-action="branch-followup"]').click();
  until = deadline();
  while (chatSessionId === completed.session_id && Date.now() < until) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const branchSessionId = chatSessionId;
  while (document.querySelector('#chat-input').value !== question && Date.now() < until) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const branchNotice = document.body.textContent.indexOf('已在新会话中分支追问') >= 0;
  const oldDetail = await (await fetch('/api/chat/sessions/' + encodeURIComponent(completed.session_id))).json();
  const newDetail = await (await fetch('/api/chat/sessions/' + encodeURIComponent(branchSessionId))).json();
  return JSON.stringify({
    question, inputAfterEdit, editNotice, branchNotice,
    branched: branchSessionId !== completed.session_id,
    branchInput: document.querySelector('#chat-input').value,
    sourceMessages: oldDetail.messages.length,
    branchMessages: newDetail.messages.length
  });
})()
""")

    assert rendered["inputAfterEdit"] == rendered["question"]
    assert rendered["editNotice"] is True
    assert rendered["branched"] is True
    assert rendered["branchInput"] == rendered["question"]
    assert rendered["branchNotice"] is True
    # 历史轮次不变，新会话不复制任何证据包（只预填问题，等待用户发送）。
    assert rendered["sourceMessages"] == 2
    assert rendered["branchMessages"] == 0
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
    assert _failed_requests(browser_session, actual_app_url) == []
