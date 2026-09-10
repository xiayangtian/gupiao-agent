"""财务结构可视化的浏览器回归测试。"""

import json
import os
import re
import signal
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
VISUALIZATIONS_JS = ROOT / "webapp" / "static" / "analysis_visualizations.js"
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

pytestmark = pytest.mark.skipif(CHROME is None, reason="财务结构可视化浏览器回归测试需要 Chrome/Chromium")


def _dump_fixture(tmp_path: Path, viewport: tuple[int, int]) -> dict:
    """在 15 秒超时与进程组清理保护下执行真实渲染脚本。"""
    html_path = tmp_path / "financial-structure-visuals.html"
    html_path.write_text(
        f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
body {{ margin: 0; max-width: 100%; overflow-x: hidden; }}
.analysis-visualization-card {{ max-width: 100%; }}
.analysis-visualization-table-wrap {{ max-width: 100%; overflow-wrap: anywhere; }}
canvas {{ max-width: 100%; }}
</style></head><body><main id="analysis"></main><iframe id="pdf"></iframe>
<script src="{VISUALIZATIONS_JS.as_uri()}"></script>
<script>
window.Chart = function(canvas, config) {{ this.canvas = canvas; this.config = config; this.destroy = function() {{}}; }};
const catalog = {{pdf: {{source_type: 'pdf_text', source_locator: {{page: 12}}}}}};
const cards = [
  {{id: 'profit_structure', topic_id: 'profit', title: '利润结构', kind: 'profit', status: 'complete', rows: [
    {{metric_id: 'revenue', label: '营业收入', value: 120, unit: '亿元', direction: 'neutral', evidence_ids: ['pdf']}},
    {{metric_id: 'net_profit', label: '净利润', value: 12, unit: '亿元', direction: 'neutral', evidence_ids: ['pdf']}}
  ]}},
  {{id: 'balance_sheet_structure', topic_id: 'balance', title: '资产负债结构', kind: 'balance_sheet', status: 'complete', rows: [
    {{metric_id: 'total_assets', label: '总资产', value: 500, unit: '亿元', direction: 'neutral', evidence_ids: ['pdf']}},
    {{metric_id: 'total_liabilities', label: '总负债', value: 360, unit: '亿元', direction: 'neutral', evidence_ids: ['pdf']}},
    {{metric_id: 'total_equity', label: '所有者权益', value: 140, unit: '亿元', direction: 'neutral', evidence_ids: ['pdf']}}
  ]}},
  {{id: 'cash_flow_structure', topic_id: 'cash', title: '现金流结构', kind: 'cash_flow', status: 'partial', rows: [
    {{metric_id: 'operating_cash_flow', label: '经营活动现金流净额', value: 22, unit: '亿元', direction: 'inflow', evidence_ids: ['pdf']}},
    {{metric_id: 'investing_cash_flow', label: '投资活动现金流净额', value: -8, unit: '亿元', direction: 'outflow', evidence_ids: ['pdf']}}
  ]}},
  {{id: 'unavailable_structure', topic_id: 'unavailable', title: '暂不可用结构', kind: 'profit', status: 'unavailable', unavailable_reason: '披露不足', rows: []}}
];
const root = document.querySelector('#analysis');
root.innerHTML = AnalysisVisualizations.renderSlots({{version: 1, cards}}, [
  {{section_id: 'profit'}}, {{section_id: 'balance'}}, {{section_id: 'cash'}}, {{section_id: 'unavailable'}}
], catalog);
AnalysisVisualizations.mount(root, {{version: 1, cards}}, {{onEvidencePage(page) {{
  document.querySelector('#pdf').src = 'https://example.test/report.pdf?jump=1#page=' + page;
}}}});
root.querySelector('.analysis-visualization-evidence').click();
document.body.dataset.result = JSON.stringify({{
  headings: Array.from(root.querySelectorAll('h3')).map(node => node.textContent),
  tables: root.querySelectorAll('table').length,
  canvases: root.querySelectorAll('canvas').length,
  unavailableCanvas: root.querySelector('[aria-label="暂不可用结构"]').querySelectorAll('canvas').length,
  pdfUrl: document.querySelector('#pdf').src,
  overflow: document.documentElement.scrollWidth > window.innerWidth
}});
</script></body></html>""",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-breakpad",
            "--disable-crash-reporter",
            "--allow-file-access-from-files",
            f"--user-data-dir={tmp_path / 'chrome-profile'}",
            f"--window-size={viewport[0]},{viewport[1]}",
            "--virtual-time-budget=1000",
            "--dump-dom",
            html_path.as_uri(),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                pytest.fail("Chrome 进程组在 SIGKILL 后仍未退出")
        pytest.skip("当前宿主的 Chrome --dump-dom 在 15 秒内未退出")
    if process.returncode == -signal.SIGABRT:
        pytest.skip("当前宿主的 Chrome headless 进程不可用")
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, process.args, stdout, stderr)
    result = re.search(r'data-result="([^"]+)"', stdout)
    assert result, stdout
    return json.loads(result.group(1).replace("&quot;", '"'))


@pytest.mark.parametrize("viewport", [(1280, 800), (768, 900), (390, 844)])
def test_v4_financial_structure_visuals_render_semantic_tables_and_pdf_pages(tmp_path, viewport):
    """三类 v4 图表有表格替代，未提供卡片不画空 canvas，窄屏不溢出。"""
    rendered = _dump_fixture(tmp_path, viewport)

    assert rendered["headings"] == ["利润结构", "资产负债结构", "现金流结构", "暂不可用结构"]
    assert rendered["tables"] == 3
    assert rendered["canvases"] == 3
    assert rendered["unavailableCanvas"] == 0
    assert rendered["pdfUrl"].endswith("?jump=1#page=12")
    assert not rendered["overflow"]
