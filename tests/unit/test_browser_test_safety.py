"""浏览器测试的运行隔离约束。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BROWSER_TEST = ROOT / "tests" / "browser" / "test_analysis_dialog_layout.py"


def test_dialog_layout_browser_probe_is_not_collected_as_a_unit_test():
    """真实 Chrome 检查必须从单元测试目录隔离出去。"""
    assert BROWSER_TEST.is_file()
    assert not (ROOT / "tests" / "unit" / "test_analysis_dialog_layout.py").exists()
    source = BROWSER_TEST.read_text(encoding="utf-8")
    assert "start_new_session=True" in source
    assert "communicate(timeout=15)" in source
    assert "os.killpg(process.pid, signal.SIGKILL)" in source
    assert "assert match, stdout" in source
