"""渐进式财报分析前端契约测试。"""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "webapp" / "static" / "index.html"
WORKFLOW_JS = ROOT / "webapp" / "static" / "analysis_workflow.js"
NODE = shutil.which("node")


class _MarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


def _nodes():
    parser = _MarkupParser()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    return parser.nodes


def _run_node(source: str) -> dict:
    if NODE is None:
        pytest.skip("渐进式前端测试需要 Node.js")
    completed = subprocess.run(
        [NODE, "-e", source], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return json.loads(completed.stdout)


def test_progressive_analysis_places_quick_result_before_dynamic_topics():
    nodes = _nodes()
    status = next(attrs for _, attrs in nodes if attrs.get("id") == "analysis-background-status")
    quick_index = next(i for i, (_, attrs) in enumerate(nodes) if attrs.get("data-analysis-tab") == "quick")
    topics_index = next(i for i, (_, attrs) in enumerate(nodes) if attrs.get("id") == "analysis-dynamic-tabs")

    assert status["role"] == "status"
    assert status["aria-live"] == "polite"
    assert status["aria-atomic"] == "true"
    assert quick_index < topics_index


def test_analysis_picker_uses_interest_language_for_first_and_repeat_runs():
    markup = INDEX.read_text(encoding="utf-8")
    assert "选择关注方向" in markup
    assert 'id="analysis-interest-open"' in markup
    assert "选择分析维度" not in markup


def test_stream_controller_deduplicates_connection_and_falls_back_with_backoff():
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const sources = [];
        const delays = [];
        class FakeSource {{
          constructor(url) {{ this.url = url; this.listeners = {{}}; sources.push(this); }}
          addEventListener(type, fn) {{ this.listeners[type] = fn; }}
          close() {{ this.closed = true; }}
        }}
        const controller = workflow.createAnalysisStreamController({{
          EventSourceClass: FakeSource,
          setTimeoutFn: (fn, delay) => {{ delays.push(delay); return delays.length; }},
          clearTimeoutFn: () => {{}},
          fetchSnapshot: async () => ({{ status: 'running', result: {{ stage: 'deep_processing' }} }}),
          onEvent: () => {{}}, onSnapshot: () => {{}}
        }});
        const task = {{ taskId: 't1', eventUrl: '/events', statusUrl: '/status', lastEventId: 7 }};
        controller.connect(task);
        controller.connect(task);
        sources[0].onerror();
        console.log(JSON.stringify({{ count: sources.length, url: sources[0].url, delays }}));
        """
    )

    assert result == {"count": 1, "url": "/events?after=7", "delays": [1000]}


def test_progressive_renderer_keeps_evidence_folded_and_limits_emphasis():
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'deep_processing',
          quick: {{ conclusions: [{{
            id: 'q1', text: '营收增长但现金流承压', style: 'verified_risk',
            highlight_spans: ['现金流承压', '营收增长', '第三处'], evidence_ids: ['e1']
          }}] }},
          evidence_catalog: {{ e1: {{ label: '年报第 12 页', excerpt: '经营现金流下降' }} }},
          sections: [{{ section_id: 'empty', title: '空主题', findings: [] }}]
        }});
        console.log(JSON.stringify({{
          hasDetails: html.includes('<details'),
          hasRisk: html.includes('analysis-emphasis-risk'),
          highlights: (html.match(/<mark/g) || []).length,
          emptyTopic: html.includes('空主题')
        }}));
        """
    )

    assert result == {
        "hasDetails": True,
        "hasRisk": True,
        "highlights": 2,
        "emptyTopic": False,
    }


def test_progressive_renderer_shows_observations_after_completed_empty_quick_result():
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'completed', quick: {{ conclusions: [] }},
          observations: [{{ title: '现金流观察', summary: '经营现金流下降', evidence_ids: ['e1'] }}],
          evidence_catalog: {{ e1: {{ label: 'PDF 第 12 页' }} }}
        }});
        console.log(JSON.stringify({{
          observation: html.includes('现金流观察'),
          pending: html.includes('快速结论生成中'),
          referenceLabel: html.includes('参考观察'),
          disclaimer: html.includes('未达到详细分析标准'),
          staleCopy: html.includes('证据尚待结构化核验'),
          hint: html.includes('未生成可核验的快速结论')
        }}));
        """
    )

    assert result == {
        "observation": True,
        "pending": False,
        "referenceLabel": True,
        "disclaimer": True,
        "staleCopy": False,
        "hint": False,
    }


def test_progressive_renderer_distinguishes_no_quick_and_no_observations():
    """完成但既无快速结论也无观察时，只显示可核验结论缺失的说明。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'completed', quick: {{ conclusions: [] }},
          observations: [], evidence_catalog: {{}}
        }});
        console.log(JSON.stringify({{
          hint: html.includes('未生成可核验的快速结论'),
          reference: html.includes('参考观察')
        }}));
        """
    )

    assert result == {"hint": True, "reference": False}


def test_table_cleanup_drops_missing_rows_and_the_whole_empty_table():
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        console.log(JSON.stringify({{
          mixed: workflow.cleanTableRows([
            ['营业收入', '100 亿元'], ['净利润', '未披露'], ['现金流', '-']
          ]),
          empty: workflow.cleanTableRows([['净利润', '未披露'], ['现金流', '暂无数据']])
        }}));
        """
    )

    assert result == {"mixed": [["营业收入", "100 亿元"]], "empty": []}
