"""浏览器测试的运行隔离约束。"""

import os
import re
import subprocess
import sys
from pathlib import Path

from scripts.run_chat_evaluation import main as quality_command_main
from webapp.browser_preflight import find_usable_agent_browser


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "chat_eval_cases.json"
BROWSER_TEST = ROOT / "tests" / "browser" / "test_analysis_dialog_layout.py"
STRUCTURE_BROWSER_TEST = ROOT / "tests" / "browser" / "test_financial_structure_visuals.py"
CHAT_TRUST_BROWSER_TEST = ROOT / "tests" / "browser" / "test_chat_trust_flow.py"
WORKSPACE_BROWSER_TEST = ROOT / "tests" / "browser" / "test_research_workspace_flow.py"
STRUCTURE_LAUNCHER = ROOT / "tests" / "browser" / "visual_test_app.py"


def test_quality_command_writes_only_requested_temp_output(tmp_path):
    """Explicit quality replay must not create or replace repository sidecars."""
    output = tmp_path / "research_quality_summary.json"
    repository_output = ROOT / "data" / "research_quality_summary.json"
    before = repository_output.read_bytes() if repository_output.exists() else None

    assert quality_command_main(["--fixture", str(FIXTURE), "--output", str(output)]) == 0

    assert output.exists()
    if before is None:
        assert not repository_output.exists()
    else:
        assert repository_output.read_bytes() == before


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


def test_acceptance_launcher_keeps_task_database_out_of_the_repository():
    """浏览器验收的应用不得打开仓库的 data/tasks.sqlite3。

    启动器在独立进程里真实执行 ``build_app()``：在单元测试进程内导入它会用验收
    fixture 覆盖模块级 server 组件。断言任务库落在启动器临时目录，且仓库任务库
    文件未被创建或修改。
    """
    source = STRUCTURE_LAUNCHER.read_text(encoding="utf-8")
    assert "TASK_DB_PATH" in source
    assert 'os.environ["TASK_DB_PATH"]' in source

    repository_db = ROOT / "data" / "tasks.sqlite3"
    before = repository_db.stat().st_mtime_ns if repository_db.exists() else None
    script = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(STRUCTURE_LAUNCHER.parent)!r})\n"
        "import visual_test_app, webapp.server as server\n"
        "visual_test_app.build_app()\n"
        "server._startup_task_manager()\n"
        "print(server.task_manager._db_path)\n"
        "server._shutdown_task_manager()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    db_path = result.stdout.strip().splitlines()[-1]
    assert "trusted-chat-browser-" in db_path, db_path
    assert not os.path.abspath(db_path).startswith(str(ROOT / "data")), db_path
    after = repository_db.stat().st_mtime_ns if repository_db.exists() else None
    assert after == before, "浏览器验收应用改动了仓库任务库"


def test_workspace_browser_flow_uses_isolated_deterministic_m4_fixtures():
    """M4 workbench acceptance uses only temporary persisted contracts and localhost."""
    source = WORKSPACE_BROWSER_TEST.read_text(encoding="utf-8")
    launcher = STRUCTURE_LAUNCHER.read_text(encoding="utf-8")

    assert "actual_app_url" in source
    assert "browser_session" in source
    assert "visual_test_app" in source
    assert "fixture-completed-run" in source
    assert "fixture-completed-run" in launcher
    assert "_seed_workspace_fixtures" in launcher
    assert "ResearchWorkspaceStore(" in launcher
    assert "ResearchMemoryStore(" in launcher
    assert "tempfile.mkdtemp" in launcher
    assert "server.research_workspace = ResearchWorkspaceStore" in launcher
    assert "server.research_memory = ResearchMemoryStore" in launcher
    assert "IngestionService" not in launcher
    assert "ReportDownloader" not in launcher
    assert "CNINFODatasource" not in launcher
    assert "requests.get" not in launcher
    assert "requests.post" not in launcher
    assert "urlopen" not in launcher
    assert "httpx." not in launcher
    assert "(1280, 900), (768, 1000), (390, 844)" in source
    assert "scrollWidth" in source
    assert "_console_errors" in source
    assert "_page_errors" in source
    assert "_failed_requests" in source


def test_workspace_export_browser_test_intercepts_native_downloads():
    """浏览器验收可验证导出动作，但不得将 fixture 导出文件写入用户下载目录。"""
    source = WORKSPACE_BROWSER_TEST.read_text(encoding="utf-8")

    assert "def _intercept_native_download" in source
    assert "HTMLAnchorElement.prototype.click" in source
    assert "restoreNativeDownload" in source
    assert "nativeDownloads" in source


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


def test_m2_policy_browser_flow_uses_real_fixture_app_and_agent_browser():
    source = (ROOT / "tests" / "browser" / "test_chat_policy_flow.py").read_text(encoding="utf-8")
    assert "actual_app_url" in source
    assert "browser_session" in source
    assert "_chat_stream" in source
    assert "overflow" in source
    assert "policy_resolved" in source
