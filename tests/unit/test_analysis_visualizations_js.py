"""财务结构可视化纯前端组件契约。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VISUALIZATIONS_JS = ROOT / "webapp" / "static" / "analysis_visualizations.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="财务结构可视化测试需要 Node.js")


def _run_node(source: str) -> dict:
    completed = subprocess.run(
        [NODE, "-e", source], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return json.loads(completed.stdout)


def test_structure_visualization_renders_slots_tables_and_pdf_pages_only():
    result = _run_node(
        f"""
        const visuals = require({json.dumps(str(VISUALIZATIONS_JS))});
        const html = visuals.renderSlots({{ version: 1, cards: [{{
          id: 'cash_flow_structure', topic_id: 'cash', title: '现金流结构', kind: 'cash_flow', status: 'complete', rows: [
            {{ metric_id: 'operating_cash_flow', label: '经营活动现金流净额', value: 12, unit: '亿元', direction: 'inflow', evidence_ids: ['pdf12', 'ocr13'] }},
            {{ metric_id: 'investing_cash_flow', label: '投资活动现金流净额', value: -3, unit: '亿元', direction: 'outflow', evidence_ids: ['pdf12'] }}
          ]
        }}] }}, [{{ section_id: 'cash' }}], {{
          pdf12: {{ source_type: 'pdf_text', source_locator: {{ page: 12 }} }},
          ocr13: {{ source_type: 'ocr_text', source_locator: {{ page: 13 }} }}
        }});
        console.log(JSON.stringify({{
          slot: html.includes('analysis-visualization-slot'), table: html.includes('<table'),
          page: html.includes('data-evidence-page="12"'), ocr: html.includes('data-evidence-page="13"'),
          direction: html.includes('流入') && html.includes('流出'), canvas: html.includes('<canvas')
        }}));
        """
    )
    assert result == {
        "slot": True, "table": True, "page": True, "ocr": False,
        "direction": True, "canvas": True,
    }


def test_structure_visualization_configs_and_safe_fallbacks():
    result = _run_node(
        f"""
        const visuals = require({json.dumps(str(VISUALIZATIONS_JS))});
        const profit = {{ id: 'profit_structure', kind: 'profit', rows: [{{ label: '收入', value: 10, unit: '亿元' }}, {{ label: '成本', value: 6, unit: '亿元' }}] }};
        const balance = {{ id: 'balance_sheet_structure', kind: 'balance_sheet', rows: [{{ metric_id: 'total_assets', label: '资产', value: 10, unit: '亿元' }}, {{ metric_id: 'total_liabilities', label: '负债', value: 6, unit: '亿元' }}, {{ metric_id: 'total_equity', label: '权益', value: 4, unit: '亿元' }}] }};
        const cash = {{ id: 'cash_flow_structure', topic_id: 'cash', title: '现金流结构', kind: 'cash_flow', status: 'partial', rows: [{{ label: '经营', value: 10, unit: '亿元', direction: 'inflow' }}, {{ label: '投资', value: -6, unit: '亿元', direction: 'outflow' }}] }};
        const partial = visuals.renderSlots({{ version: 1, cards: [cash] }}, [{{ section_id: 'cash' }}], {{}});
        const unavailable = visuals.renderSlots({{ version: 1, cards: [{{ id: 'cash_flow_structure', topic_id: 'cash', title: '现金流结构', kind: 'cash_flow', status: 'unavailable', unavailable_reason: '披露不足', rows: [] }}] }}, [{{ section_id: 'cash' }}], {{}});
        const legacy = visuals.renderSlots(null, [{{ section_id: 'cash' }}], {{}});
        console.log(JSON.stringify({{
          profit: visuals.chartConfig(profit).options.indexAxis,
          balance: visuals.chartConfig(balance).data.datasets.length,
          cashZero: visuals.chartConfig(cash).options.scales.x.beginAtZero,
          partial: partial.includes('数据不完整，仅展示已核验披露项'),
          unavailableCanvas: unavailable.includes('<canvas'), unavailableReason: unavailable.includes('披露不足'),
          legacyPrompt: legacy.includes('重新分析后可生成结构图')
        }}));
        """
    )
    assert result == {
        "profit": "y", "balance": 3, "cashZero": True, "partial": True,
        "unavailableCanvas": False, "unavailableReason": True, "legacyPrompt": True,
    }


def test_structure_visualization_without_chartjs_removes_canvas_but_keeps_evidence_binding():
    result = _run_node(
        f"""
        const visuals = require({json.dumps(str(VISUALIZATIONS_JS))});
        delete global.Chart;
        const chartBox = {{ innerHTML: '<canvas></canvas>' }};
        const canvas = {{ parentNode: chartBox, remove: () => {{ throw new Error('should replace parent HTML'); }} }};
        const evidenceButton = {{ dataset: {{ evidencePage: '12' }}, addEventListener: (_name, listener) => {{ evidenceButton.listener = listener; }} }};
        const slot = {{ getAttribute: () => 'cash_flow_structure', querySelector: () => canvas }};
        const container = {{ querySelectorAll: selector => selector.indexOf('slot') >= 0 ? [slot] : [evidenceButton] }};
        let page = null;
        const charts = visuals.mount(container, {{ version: 1, cards: [{{
          id: 'cash_flow_structure', kind: 'cash_flow', status: 'partial', rows: [
            {{ label: '经营', value: 1, unit: '亿元', direction: 'inflow' }},
            {{ label: '投资', value: -1, unit: '亿元', direction: 'outflow' }}
          ]
        }}] }}, {{ onEvidencePage: value => page = value }});
        evidenceButton.listener({{ stopPropagation: () => {{}} }});
        console.log(JSON.stringify({{ charts: charts.size, chartBox: chartBox.innerHTML, page }}));
        """
    )
    assert result == {
        "charts": 0,
        "chartBox": '<p class="analysis-visualization-chart-unavailable" role="status">图表组件不可用，已展示数据表</p>',
        "page": 12,
    }


def test_structure_visualization_mounts_charts_and_binds_pdf_buttons():
    result = _run_node(
        f"""
        const visuals = require({json.dumps(str(VISUALIZATIONS_JS))});
        let destroyed = 0;
        global.Chart = function(canvas, config) {{ this.canvas = canvas; this.config = config; this.destroy = () => destroyed++; }};
        const canvas = {{}};
        const evidenceButton = {{ dataset: {{ evidencePage: '18' }}, addEventListener: (_name, listener) => {{ evidenceButton.listener = listener; }} }};
        const slot = {{ getAttribute: () => 'cash_flow_structure', querySelector: () => canvas }};
        const container = {{ querySelectorAll: selector => selector.indexOf('slot') >= 0 ? [slot] : [evidenceButton] }};
        let page = null;
        const charts = visuals.mount(container, {{ version: 1, cards: [{{
          id: 'cash_flow_structure', kind: 'cash_flow', status: 'partial', rows: [
            {{ label: '经营', value: 1, unit: '亿元', direction: 'inflow' }},
            {{ label: '投资', value: -1, unit: '亿元', direction: 'outflow' }}
          ]
        }}] }}, {{ onEvidencePage: value => page = value }});
        let propagationStopped = false;
        evidenceButton.listener({{ stopPropagation: () => propagationStopped = true }});
        const created = charts.size;
        visuals.destroy(charts);
        console.log(JSON.stringify({{ created, cleared: charts.size, page, destroyed, propagationStopped }}));
        """
    )
    assert result == {"created": 1, "cleared": 0, "page": 18, "destroyed": 1, "propagationStopped": True}
