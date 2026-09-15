"""浏览器测试的运行隔离约束。"""

import re
from pathlib import Path

from webapp.browser_preflight import find_usable_agent_browser


ROOT = Path(__file__).resolve().parents[2]
BROWSER_TEST = ROOT / "tests" / "browser" / "test_analysis_dialog_layout.py"
STRUCTURE_BROWSER_TEST = ROOT / "tests" / "browser" / "test_financial_structure_visuals.py"
CHAT_TRUST_BROWSER_TEST = ROOT / "tests" / "browser" / "test_chat_trust_flow.py"
STRUCTURE_LAUNCHER = ROOT / "tests" / "browser" / "visual_test_app.py"


def test_dialog_layout_browser_probe_is_not_collected_as_a_unit_test():
    """真实 Chrome 检查必须从单元测试目录隔离出去。"""
    assert BROWSER_TEST.is_file()
    assert not (ROOT / "tests" / "unit" / "test_analysis_dialog_layout.py").exists()
    source = BROWSER_TEST.read_text(encoding="utf-8")
    assert "start_new_session=True" in source
    assert "communicate(timeout=15)" in source
    assert "os.killpg(process.pid, signal.SIGKILL)" in source
    assert "assert match, stdout" in source


def test_agent_browser_preflight_rejects_non_executable_environment_candidate(tmp_path):
    wrapper = tmp_path / "agent-browser"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    assert find_usable_agent_browser([str(wrapper)]) is None


def test_agent_browser_preflight_rejects_wrapper_without_browser_backend(tmp_path):
    wrapper = tmp_path / "agent-browser"
    wrapper.write_text(
        "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then exit 0; fi\nprintf '{\"success\":false}\\n'\nexit 1\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    assert find_usable_agent_browser([str(wrapper)]) is None


def test_financial_structure_browser_tests_use_agent_browser_without_opt_in_skip():
    """默认入口在可用 agent-browser 下运行真实 URL，只有 CLI 缺失才允许跳过。"""
    source = STRUCTURE_BROWSER_TEST.read_text(encoding="utf-8")

    assert "AGENT_BROWSER" in source
    assert "RUN_BROWSER_INTEGRATION" not in source
    assert "agent-browser CLI 不可用" in source
    assert "find_usable_agent_browser" in source
    assert "os.X_OK" in (ROOT / "webapp" / "browser_preflight.py").read_text(encoding="utf-8")
    assert '"open", "about:blank"' in (ROOT / "webapp" / "browser_preflight.py").read_text(encoding="utf-8")
    assert "visual_test_app" in source
    assert "actual_app_url" in source
    assert "file://" in source  # 文档明确说明该测试不是 file:// fixture。
    assert "timeout=15" in source
    assert "真实应用服务在 15 秒内未就绪" in source


def test_acceptance_app_disables_real_rag_ingest_and_injects_local_fakes():
    """验收应用不构造真实摄取、外部数据源或生产下载器，全部协作为本地 fake。

    旧契约锚定「字面量 `rag_service = None`」这一实现手段；补报授权验收需要在
    批准路径上真实反映摄取结果，因此改为锚定真正的意图：注入本地 fake 摄取
    （``_SupplementIngestion``），既不导入也不构造真实 ``IngestionService``，
    且不出现 CNINFO/外部 URL 与生产下载器默认实现。``rag_store``/``rag_qa``
    仍为 fake，让可信问答浏览器回归经真实 SSE 产出确定性证据与停止运行。
    """
    launcher = STRUCTURE_LAUNCHER.read_text(encoding="utf-8")
    lowered = launcher.lower()

    assert "webapp.server" in launcher
    # 真实摄取必须被替换为本地 fake，既没有导入也没有构造真实服务。
    assert "_SupplementIngestion" in launcher
    assert "IngestionService" not in launcher
    assert "server.rag_service = _SupplementIngestion" in launcher
    # 生产下载器默认实现与真实数据源同样不得出现在验收启动器中。
    assert "ReportDownloader" not in launcher
    assert "CNINFODatasource" not in launcher
    assert "server.downloader = _SupplementDownloader" in launcher
    assert "server.datasource = _SupplementDatasource" in launcher
    # 补充协作方的外部身份只允许 fixture://；启动器不得包含出站调用原语。
    assert "fixture://" in launcher
    download_urls = re.findall(r'download_url="([^"]*)"', launcher)
    assert download_urls, "验收启动器必须提供确定性补报元数据"
    assert all(url.startswith("fixture://") for url in download_urls), download_urls
    assert "cninfo.com.cn" not in lowered
    for outbound in ("requests.get", "requests.post", "urlopen", "httpx."):
        assert outbound not in launcher, outbound
    assert "_FakeRagStore" in launcher
    assert "list_report_ids" in launcher
    assert "_FakeRagQA" in launcher
    assert "answer_stream" in launcher
    assert "chat_store = ChatStore" in launcher
    assert "fetch_reports" in launcher
    assert "uvicorn" in launcher


def test_acceptance_teardown_only_fails_when_the_process_survives_kill():
    """慢关闭不是失败；只有 SIGKILL 之后进程仍存活才允许判为失败。"""
    source = STRUCTURE_BROWSER_TEST.read_text(encoding="utf-8")

    assert "真实应用服务在终止后仍未退出" not in source
    assert "TEARDOWN_GRACE_SECONDS" in source
    assert "SIGKILL 后仍未退出" in source
    assert source.index("process.kill()") < source.index("SIGKILL 后仍未退出")


def test_chat_trust_browser_tests_use_agent_browser_without_opt_in_skip():
    """可信问答浏览器回归在可用 agent-browser 下访问真实 URL，只有 CLI 缺失才跳过。"""
    source = CHAT_TRUST_BROWSER_TEST.read_text(encoding="utf-8")

    assert "AGENT_BROWSER" in source
    assert "RUN_BROWSER_INTEGRATION" not in source
    assert "agent-browser CLI 不可用" in source
    assert "find_usable_agent_browser" in source
    assert "visual_test_app" in source
    assert "actual_app_url" in source
    assert "file://" in source  # 文档明确说明该测试不是 file:// fixture。
    assert "/api/chat/stream" in source  # 通过真实 SSE 端点生成会话，而非 mock 页面。
    assert "TEARDOWN_GRACE_SECONDS" in source
    assert "SIGKILL 后仍未退出" in source
