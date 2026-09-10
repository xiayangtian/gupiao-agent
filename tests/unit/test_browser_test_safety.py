"""浏览器测试的运行隔离约束。"""

from pathlib import Path

from webapp.browser_preflight import find_usable_agent_browser


ROOT = Path(__file__).resolve().parents[2]
BROWSER_TEST = ROOT / "tests" / "browser" / "test_analysis_dialog_layout.py"
STRUCTURE_BROWSER_TEST = ROOT / "tests" / "browser" / "test_financial_structure_visuals.py"
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


def test_acceptance_app_disables_rag_ingest_and_external_datasource():
    """验收应用不触发 RAG 摄取与外部行情请求，避免结果依赖本地数据与网络。"""
    launcher = STRUCTURE_LAUNCHER.read_text(encoding="utf-8")

    assert "webapp.server" in launcher
    assert "rag_service = None" in launcher
    assert "rag_store = None" in launcher
    assert "rag_qa = None" in launcher
    assert "fetch_reports" in launcher
    assert "uvicorn" in launcher


def test_acceptance_teardown_only_fails_when_the_process_survives_kill():
    """慢关闭不是失败；只有 SIGKILL 之后进程仍存活才允许判为失败。"""
    source = STRUCTURE_BROWSER_TEST.read_text(encoding="utf-8")

    assert "真实应用服务在终止后仍未退出" not in source
    assert "TEARDOWN_GRACE_SECONDS" in source
    assert "SIGKILL 后仍未退出" in source
    assert source.index("process.kill()") < source.index("SIGKILL 后仍未退出")
