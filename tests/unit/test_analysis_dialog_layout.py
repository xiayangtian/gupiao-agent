"""分析维度弹窗布局的浏览器回归测试。"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
STYLE_CSS = ROOT / "webapp" / "static" / "style.css"
CHROME = next(
    (
        path
        for path in (
            shutil.which("google-chrome"),
            shutil.which("chromium"),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        if path and Path(path).exists()
    ),
    None,
)

pytestmark = pytest.mark.skipif(CHROME is None, reason="弹窗布局回归测试需要 Chrome/Chromium")


def test_analysis_dialog_is_centered_in_the_viewport(tmp_path):
    """打开维度选择弹窗后，其中心应与视口中心重合，不能停在左上角。"""
    html_path = tmp_path / "dialog-layout.html"
    html_path.write_text(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="{STYLE_CSS.as_uri()}">
  <style>.analysis-dialog {{ animation: none !important; }}</style>
</head>
<body>
  <dialog id="dialog" class="analysis-dialog">
    <div class="analysis-dialog-head"><h2>选择分析维度</h2></div>
    <div class="analysis-dialog-actions"><button>开始分析</button></div>
  </dialog>
  <script>
    const dialog = document.querySelector('#dialog');
    dialog.showModal();
    const rect = dialog.getBoundingClientRect();
    document.body.textContent = JSON.stringify({{
      dialogCenterX: rect.left + rect.width / 2,
      dialogCenterY: rect.top + rect.height / 2,
      viewportCenterX: innerWidth / 2,
      viewportCenterY: innerHeight / 2
    }});
  </script>
</body>
</html>""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--allow-file-access-from-files",
            "--window-size=800,600",
            "--virtual-time-budget=1000",
            "--dump-dom",
            html_path.as_uri(),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    match = re.search(r"<body>(\{.*?\})\s*</body>", completed.stdout, re.DOTALL)
    assert match, completed.stdout
    layout = json.loads(match.group(1))

    assert layout["dialogCenterX"] == pytest.approx(layout["viewportCenterX"], abs=1)
    assert layout["dialogCenterY"] == pytest.approx(layout["viewportCenterY"], abs=1)
