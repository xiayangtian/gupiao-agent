"""浏览器测试的运行隔离约束。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BROWSER_TEST = ROOT / "tests" / "browser" / "test_analysis_dialog_layout.py"
STRUCTURE_BROWSER_TEST = ROOT / "tests" / "browser" / "test_financial_structure_visuals.py"


def test_dialog_layout_browser_probe_is_not_collected_as_a_unit_test():
    """真实 Chrome 检查必须从单元测试目录隔离出去。"""
    assert BROWSER_TEST.is_file()
    assert not (ROOT / "tests" / "unit" / "test_analysis_dialog_layout.py").exists()
    source = BROWSER_TEST.read_text(encoding="utf-8")
    assert "start_new_session=True" in source
    assert "communicate(timeout=15)" in source
    assert "os.killpg(process.pid, signal.SIGKILL)" in source
    assert "assert match, stdout" in source


def test_financial_structure_browser_timeout_fails_after_process_group_cleanup():
    """Chrome 超时是测试失败，不得伪装成宿主 skip；清理必须确认退出。"""
    source = STRUCTURE_BROWSER_TEST.read_text(encoding="utf-8")

    assert "def _terminate_timed_out_process" in source
    assert source.count("_terminate_timed_out_process(process)") == 2
    assert source.count('pytest.fail("Chrome --dump-dom 在 15 秒内未退出")') == 2
    assert "当前宿主的 Chrome --dump-dom 在 15 秒内未退出" not in source
    assert 'pytest.fail("Chrome 进程组在 SIGKILL 后仍未退出")' in source
