"""智能问答可信范围与证据闭环的真实应用浏览器回归测试。

验证 M1 的可信回答底座在真实浏览器中按契约渲染：

- company_only Scope 首部 + 第 40 页 PDF 证据 + 网页来源在历史重开后完整保留；
- company_industry 范围标注「本地可检索同业样本」，绝不承诺「全行业排名」；
- stopped 运行显示「已停止」，绝不显示「已完成」误导文案；
- 390x844 移动视口下页面无横向溢出。

会话由可控 fake ``rag_qa.answer_stream`` 通过真实 ``/api/chat/stream`` SSE 端点生成，
因此本测试不消耗模型配额、不访问真实网络；仅当 ``agent-browser`` CLI 不可执行时跳过。
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = Path(__file__).resolve().parent / "visual_test_app.py"
# 关闭服务时的宽限：后台任务仍需收尾，慢关闭不算失败。
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
    """每项浏览器测试都访问实际 FastAPI 应用，而非 file:// 或 mock 页面。"""
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
            # 后台任务拖延关闭时强制结束；只有 SIGKILL 后仍存活才算失败。
            process.kill()
            try:
                process.communicate(timeout=TEARDOWN_KILL_SECONDS)
            except subprocess.TimeoutExpired:
                pytest.fail("真实应用服务在 SIGKILL 后仍未退出")


@pytest.fixture
def browser_session(actual_app_url):
    session = f"chat-trust-{uuid.uuid4().hex}"
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
    result = payload["data"]["result"]
    return json.loads(result)


def _data(session: str, command: str, *args: str) -> dict:
    return (_run_browser(session, command, *args).get("data") or {})


def _start_observing(session: str) -> None:
    """清空缓冲，只观察本用例产生的 console/错误/网络事件。"""
    _run_browser(session, "console", "--clear")
    _run_browser(session, "errors", "--clear")
    _run_browser(session, "network", "requests", "--clear")


def _console_errors(session: str) -> list:
    messages = _data(session, "console").get("messages") or []
    return [message.get("text") for message in messages if message.get("type") == "error"]


def _page_errors(session: str) -> list:
    return _data(session, "errors").get("errors") or []


def _failed_requests(session: str, origin: str) -> list:
    requests = _data(session, "network", "requests").get("requests") or []
    failed = []
    for request in requests:
        url = request.get("url") or ""
        status = request.get("status")
        if not url.startswith(origin) or not isinstance(status, int):
            continue
        if url.endswith("/favicon.ico"):
            continue  # 应用未提供 favicon，与可信问答回归无关
        if not 200 <= status < 400:
            failed.append({"url": url, "status": status})
    return failed


def _parse_sse_frame(frame: str):
    """把单帧 SSE 文本解析为 (event, data)；非 data 帧返回 None。"""
    event = None
    data_lines = []
    for line in frame.splitlines():
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    if not data_lines:
        return None
    try:
        data = json.loads("".join(data_lines))
    except json.JSONDecodeError:
        return None
    return {"event": event, "data": data}


def _chat_stream(url: str, body: dict):
    """POST /api/chat/stream 并读取完整 SSE；返回 (session_id, events)。"""
    req = urllib.request.Request(
        f"{url}/api/chat/stream",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    events = []
    session_id = None
    with urllib.request.urlopen(req, timeout=30) as response:
        buffer = ""
        while True:
            chunk = response.read(1024)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                parsed = _parse_sse_frame(frame)
                if not parsed:
                    continue
                events.append(parsed)
                if parsed["event"] == "session":
                    session_id = parsed["data"].get("session_id")
    return session_id, events


def _session_detail(url: str, session_id: str) -> dict:
    with urllib.request.urlopen(f"{url}/api/chat/sessions/{session_id}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _create_completed_chat(url: str, question: str) -> tuple[str, dict]:
    """通过真实 SSE 生成一条 company_only + PDF(40 页)/网页证据的 completed 运行。"""
    body = {
        "question": question,
        "scope_mode": "company_only",
        "use_mcp": False,
        "focus_report": {"code": "601288", "period": "2026-06-30"},
    }
    session_id, events = _chat_stream(url, body)
    assert session_id, "SSE 未返回 session 事件"
    done = next((event for event in events if event["event"] == "done"), None)
    assert done, "SSE 未返回 done 事件"
    return session_id, done["data"]["run"]


def _create_stopped_chat(url: str, question: str) -> tuple[str, dict]:
    """通过真实 SSE 生成一条 stopped 运行（fake RAG 产出部分内容后停止，无 done）。"""
    body = {
        "question": question,
        "scope_mode": "company_only",
        "use_mcp": False,
        "focus_report": {"code": "601288", "period": "2026-06-30"},
    }
    session_id, events = _chat_stream(url, body)
    assert session_id, "SSE 未返回 session 事件"
    assert not any(event["event"] == "done" for event in events), "停止运行不应出现 done 事件"
    run = _session_detail(url, session_id)["messages"][1]["run"]
    assert run["status"] == "stopped"
    return session_id, run


def _reopen_session_script(session_id: str) -> str:
    """重开会话并返回范围/证据/状态的真实 DOM 摘要。"""
    return f"""
(async () => {{
  const deadline = Date.now() + 10000;
  await openChatSession({json.dumps(session_id)});
  while (!document.querySelector('#chat-history .chat-run') && Date.now() < deadline) {{
    await new Promise(r => setTimeout(r, 100));
  }}
  const scopeLine = document.querySelector('#chat-history .chat-scope-line');
  const scopeNote = document.querySelector('#chat-history .chat-scope-note');
  const pdfPage = document.querySelector('#chat-history .chat-pdf-page');
  const webLink = document.querySelector('#chat-history .chat-web-link');
  const statusLabel = document.querySelector('#chat-history .chat-run-status-label');
  return JSON.stringify({{
    routeVisible: !document.querySelector('#page-chat').classList.contains('hidden'),
    scopeText: scopeLine ? scopeLine.textContent : '',
    scopeNote: scopeNote ? scopeNote.textContent : '',
    pdfHref: pdfPage ? pdfPage.getAttribute('href') : '',
    pdfPageAttr: pdfPage ? pdfPage.getAttribute('data-chat-pdf-page') : '',
    pdfText: pdfPage ? pdfPage.textContent : '',
    webHref: webLink ? webLink.getAttribute('href') : '',
    webText: webLink ? webLink.textContent : '',
    statusText: statusLabel ? statusLabel.textContent : '',
    hasArtifacts: !!document.querySelector('#chat-history .chat-artifacts'),
    runCount: document.querySelectorAll('#chat-history .chat-run').length
  }});
}})()
"""


def _overflow_script(session_id: str) -> str:
    """在移动视口下重开会话并量测横向溢出。"""
    return f"""
(async () => {{
  const deadline = Date.now() + 10000;
  await openChatSession({json.dumps(session_id)});
  while (!document.querySelector('#chat-history .chat-run') && Date.now() < deadline) {{
    await new Promise(r => setTimeout(r, 100));
  }}
  return JSON.stringify({{
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
    overflow: document.documentElement.scrollWidth > window.innerWidth,
    hasRun: !!document.querySelector('#chat-history .chat-run')
  }});
}})()
"""


def test_chat_history_reopens_with_scope_and_pdf_page_link(browser_session, actual_app_url):
    """company_only Scope 与第 40 页 PDF/网页证据在历史重开后完整保留且 URL 安全。"""
    session_id, run = _create_completed_chat(actual_app_url, question="农业银行经营现金流如何？")

    assert run["status"] == "completed"
    assert run["scope"]["mode"] == "company_only"
    assert run["scope"]["companies"][0]["code"] == "601288"
    pdf = next(artifact for artifact in run["artifacts"] if artifact["source"] == "pdf")
    assert pdf["page"] == 40
    assert pdf["pdf_url"].startswith("/api/history-pdf/")
    assert "#page=40" in pdf["pdf_url"]
    web = next(artifact for artifact in run["artifacts"] if artifact["source"] == "web")
    assert web["url"].startswith("https://")

    _run_browser(browser_session, "open", f"{actual_app_url}/#/chat")
    _start_observing(browser_session)
    rendered = _eval(browser_session, _reopen_session_script(session_id))

    assert rendered["routeVisible"] is True, "必须路由到真实智能问答页"
    assert "本公司" in rendered["scopeText"]
    assert "601288" in rendered["scopeText"]
    assert "2026 半年报" in rendered["scopeText"]
    assert rendered["pdfPageAttr"] == "40"
    assert "#page=40" in rendered["pdfHref"]
    assert rendered["pdfHref"].startswith("/api/history-pdf/")
    assert "javascript:" not in rendered["pdfHref"]
    assert "打开 PDF 原文" in rendered["pdfText"]
    assert rendered["webHref"] == web["url"]
    assert web["title"] in rendered["webText"]
    assert rendered["hasArtifacts"] is True
    assert rendered["statusText"] == "✅ 已完成"
    assert rendered["runCount"] == 1
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
    assert _failed_requests(browser_session, actual_app_url) == []


def test_chat_industry_scope_labels_local_indexed_sample(browser_session, actual_app_url):
    """行业扩展必须标注「本地可检索同业样本」，绝不出现「全行业排名」。"""
    body = {
        "question": "农业银行与同业现金流对比如何？",
        "scope_mode": "company_industry",
        "use_mcp": False,
        "focus_report": {"code": "601288", "period": "2026-06-30"},
    }
    session_id, events = _chat_stream(actual_app_url, body)
    assert session_id, "SSE 未返回 session 事件"
    done = next((event for event in events if event["event"] == "done"), None)
    assert done, "SSE 未返回 done 事件"
    assert done["data"]["run"]["scope"]["mode"] == "company_industry"
    assert done["data"]["run"]["scope"]["industry"]["sample_kind"] == "local_indexed"

    _run_browser(browser_session, "open", f"{actual_app_url}/#/chat")
    _start_observing(browser_session)
    rendered = _eval(browser_session, _reopen_session_script(session_id))

    assert rendered["routeVisible"] is True
    assert "本地可检索同业样本" in rendered["scopeNote"]
    assert "银行业（数据源分类）" in rendered["scopeText"]
    assert "全行业排名" not in rendered["scopeText"]
    assert "全行业排名" not in rendered["scopeNote"]
    body_text = _eval(browser_session, "JSON.stringify(document.body.textContent)")
    assert "全行业排名" not in body_text
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []


def test_stopped_run_is_not_rendered_as_completed(browser_session, actual_app_url):
    """stopped 运行显示「已停止」，绝不显示「已完成」误导文案。"""
    session_id, run = _create_stopped_chat(actual_app_url, question="请分析农业银行现金流并停止")
    assert run["status"] == "stopped"

    _run_browser(browser_session, "open", f"{actual_app_url}/#/chat")
    _start_observing(browser_session)
    rendered = _eval(browser_session, _reopen_session_script(session_id))

    assert rendered["statusText"] == "⏹ 已停止"
    body_text = _eval(browser_session, "JSON.stringify(document.querySelector('#chat-history').textContent)")
    assert "已停止" in body_text
    assert "已完成" not in body_text
    assert "重新生成" in body_text
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []


@pytest.mark.parametrize("viewport", [(1280, 900), (768, 1000), (390, 844)])
def test_chat_layout_has_no_horizontal_overflow_across_viewports(browser_session, actual_app_url, viewport):
    """桌面/平板/移动（390x844）视口下智能问答页均无横向溢出且无 console/error。"""
    session_id, _ = _create_completed_chat(actual_app_url, question="农业银行经营现金流如何？")

    _run_browser(browser_session, "set", "viewport", str(viewport[0]), str(viewport[1]))
    _run_browser(browser_session, "open", f"{actual_app_url}/#/chat")
    _start_observing(browser_session)
    rendered = _eval(browser_session, _overflow_script(session_id))

    assert rendered["hasRun"] is True, "必须先渲染出可信回答块，量测才有意义"
    assert rendered["scrollWidth"] <= rendered["innerWidth"], (
        f"{viewport} 视口横向溢出：scrollWidth={rendered['scrollWidth']} > innerWidth={rendered['innerWidth']}"
    )
    assert rendered["overflow"] is False
    assert _console_errors(browser_session) == []
    assert _page_errors(browser_session) == []
