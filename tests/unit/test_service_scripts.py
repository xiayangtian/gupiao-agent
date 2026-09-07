"""服务脚本的静态安全契约。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = ROOT / "start.sh"
WINDOWS_SCRIPT = ROOT / "service.bat"


def test_start_script_leaves_ai_configuration_to_application() -> None:
    """启动脚本不解析、覆盖或回显 AI API Key。"""
    script = START_SCRIPT.read_text(encoding="utf-8")

    assert "CFG_AI_KEY" not in script
    assert "AI_API_KEY:0:" not in script
    assert "AI_API_KEY: -" not in script
    assert 'export AI_API_KEY="$CFG_AI_KEY"' not in script


def test_windows_service_script_has_safe_lifecycle_commands() -> None:
    """Windows 控制脚本覆盖完整生命周期，并在停止前校验进程命令行。"""
    script = WINDOWS_SCRIPT.read_text(encoding="utf-8")

    for command in ("start", "stop", "restart", "status", "logs"):
        assert f'if /i "%~1"=="{command}"' in script
    assert "Get-CimInstance Win32_Process" in script
    assert "webapp\\.server:app" in script
    assert "taskkill /pid" in script
    assert "AI_API_KEY" not in script
