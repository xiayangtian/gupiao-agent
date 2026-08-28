"""服务脚本的静态安全契约。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = ROOT / "start.sh"


def test_start_script_leaves_ai_configuration_to_application() -> None:
    """启动脚本不解析、覆盖或回显 AI API Key。"""
    script = START_SCRIPT.read_text(encoding="utf-8")

    assert "CFG_AI_KEY" not in script
    assert "AI_API_KEY:0:" not in script
    assert "AI_API_KEY: -" not in script
    assert 'export AI_API_KEY="$CFG_AI_KEY"' not in script
