"""Chat multi-index Chart.js configuration and lifecycle regression tests."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CHARTS_JS = ROOT / "webapp" / "static" / "chat_market_charts.js"
CHAT_RENDERING_JS = ROOT / "webapp" / "static" / "chat_rendering.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="前端图表测试需要 Node.js")


def _run_node(source: str) -> dict:
    completed = subprocess.run(
        [NODE, "-e", source], cwd=ROOT, check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout)


def test_market_chart_config_is_line_chart_with_date_raw_point_and_change_tooltips():
    result = _run_node(
        f"""
        let charts;
        try {{ charts = require({json.dumps(str(CHARTS_JS))}); }} catch (_) {{}}
        const available = !!charts && typeof charts.chartConfig === 'function';
        if (!available) {{ console.log(JSON.stringify({{available}})); }}
        else {{
          const config = charts.chartConfig({{
            periods:['2026-01-05','2026-01-06'], periodKind:'daily',
            series:[
              {{code:'sh000001', label:'上证指数', closeValues:[3000,3030], changePctValues:[0,1]}},
              {{code:'sz399001', label:'深证成指', closeValues:[10000,10100], changePctValues:[0,1]}}
            ]
          }});
          const item = {{label:'2026-01-06', dataset:config.data.datasets[0], dataIndex:1, parsed:{{y:1}}}};
          const tooltip = config.options.plugins.tooltip.callbacks;
          console.log(JSON.stringify({{
            available, type:config.type, labels:config.data.labels,
            yTitle:config.options.scales.y.title.text,
            dataset:config.data.datasets[0].data, seriesCount:config.data.datasets.length,
            firstPointStyle:config.data.datasets[0].pointStyle,
            secondPointStyle:config.data.datasets[1].pointStyle,
            secondDash:config.data.datasets[1].borderDash,
            tooltipTitle:tooltip.title([item]), tooltipLabel:tooltip.label(item)
          }}));
        }}
        """
    )
    assert result["available"] is True
    assert result["type"] == "line"
    assert result["labels"] == ["2026-01-05", "2026-01-06"]
    assert "涨跌幅" in result["yTitle"]
    assert result["dataset"] == [0, 1]
    assert result["seriesCount"] == 2
    assert result["firstPointStyle"] == "circle"
    assert result["secondPointStyle"] == "rect"
    assert result["secondDash"] == [6, 3]
    assert result["tooltipTitle"] == "2026-01-06"
    assert "上证指数" in result["tooltipLabel"]
    assert "3,030" in result["tooltipLabel"]
    assert "+1.00%" in result["tooltipLabel"]


def test_stock_chart_uses_raw_prices_on_axis_and_relative_change_in_tooltip():
    result = _run_node(
        f"""
        const charts = require({json.dumps(str(CHARTS_JS))});
        const config = charts.chartConfig({{
          mode:'stock_price', unit:'元/股', periods:['2026-01-05','2026-01-06'],
          series:[{{code:'600900', label:'长江电力', closeValues:[10,11], changePctValues:[0,10]}}]
        }});
        const tooltip = config.options.plugins.tooltip.callbacks;
        const item = {{label:'2026-01-06', dataset:config.data.datasets[0], dataIndex:1, parsed:{{y:11}}}};
        console.log(JSON.stringify({{
          data:config.data.datasets[0].data,
          yTitle:config.options.scales.y.title.text,
          yTick:config.options.scales.y.ticks.callback(11),
          tooltip:tooltip.label(item),
          title:tooltip.title([item])
        }}));
        """
    )
    assert result['data'] == [10, 11]
    assert '元/股' in result['yTitle']
    assert '11' in result['yTick']
    assert result['title'] == '2026-01-06'
    assert '长江电力' in result['tooltip']
    assert '11' in result['tooltip']
    assert '较首个采样点' in result['tooltip']
    assert '+10.00%' in result['tooltip']


def test_market_chart_mount_fallback_and_destroy_all_manage_chart_instances():
    result = _run_node(
        f"""
        const loaded = (() => {{ try {{ return require({json.dumps(str(CHARTS_JS))}); }} catch (_) {{ return null; }} }})();
        if (!loaded || typeof loaded.mount !== 'function') {{ console.log(JSON.stringify({{available:false}})); }}
        else {{
          const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
          const facts = ['sh000001','sz399001'].flatMap((code, index) => [
            {{metric:'index_close', value:index ? 10000 : 3000, unit:'点', period:'2026-01-05', period_kind:'daily', entity_scope:'index', company_code:code, source_type:'tool', source_category:'market', verification:'reference'}},
            {{metric:'index_close', value:index ? 10100 : 3030, unit:'点', period:'2026-01-06', period_kind:'daily', entity_scope:'index', company_code:code, source_type:'tool', source_category:'market', verification:'reference'}}
          ]);
          const run = {{execution_plan:{{source_mode:'market_recap'}}, facts}};
          const instances = [];
          function FakeChart(context, config) {{ this.context = context; this.config = config; this.destroyCount = 0; instances.push(this); }}
          FakeChart.prototype.destroy = function () {{ this.destroyCount += 1; }};
          const container = {{
            querySelector(selector) {{
              if (selector.indexOf('canvas') >= 0) return canvas;
              if (selector.indexOf('plot') >= 0) return plot;
              return fallback;
            }}
          }};
          const canvas = {{hidden:false, getContext:() => ({{canvas:true}})}};
          const fallback = {{hidden:true, textContent:''}};
          const plotClasses = new Set();
          const plot = {{classList:{{add:name => plotClasses.add(name), contains:name => plotClasses.has(name)}}}};
          globalThis.ChatRendering = rendering;
          globalThis.Chart = FakeChart;
          const mounted = loaded.mount(container, run);
          loaded.destroyAll();
          const destroyed = instances[0] && instances[0].destroyCount;
          globalThis.Chart = undefined;
          const fallbackResult = loaded.mount(container, run);
          const fallbackCanvasHidden = canvas.hidden;
          const fallbackPlotMode = plot.classList.contains('chat-market-chart-plot-fallback');
          canvas.hidden = false;
          fallback.hidden = true;
          fallback.textContent = '';
          globalThis.Chart = function () {{ throw new Error('chart construction failed'); }};
          const constructorFallback = loaded.mount(container, run);
          const constructorCanvasHidden = canvas.hidden;
          console.log(JSON.stringify({{
            available:true, mounted:!!mounted, instanceCount:instances.length, destroyed,
            fallbackResult:fallbackResult === null, fallbackHidden:fallback.hidden,
            fallbackText:fallback.textContent, fallbackCanvasHidden, fallbackPlotMode,
            constructorFallback:constructorFallback === null, constructorFallbackHidden:fallback.hidden,
            constructorCanvasHidden
          }}));
        }}
        """
    )
    assert result["available"] is True
    assert result["mounted"] is True
    assert result["instanceCount"] == 1
    assert result["destroyed"] == 1
    assert result["fallbackResult"] is True
    assert result["fallbackHidden"] is False
    assert result["fallbackText"]
    assert result["fallbackCanvasHidden"] is True
    assert result["fallbackPlotMode"] is True
    assert result["constructorFallback"] is True
    assert result["constructorFallbackHidden"] is False
    assert result["constructorCanvasHidden"] is True
