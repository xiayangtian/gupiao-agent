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


def test_progressive_renderer_uses_inline_citations_not_folded_evidence():
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'deep_processing',
          quick: {{ conclusions: [{{
            id: 'q1', text: '营收增长但现金流承压', style: 'verified_risk',
            highlight_spans: ['现金流承压', '营收增长', '第三处'], evidence_ids: ['e1']
          }}] }},
          evidence_catalog: {{ e1: {{
            label: '年报第 12 页', excerpt: '经营现金流下降',
            source_type: 'pdf_text', source_locator: {{ page: 12 }}
          }} }},
          sections: [{{ section_id: 'empty', title: '空主题', findings: [] }}]
        }});
        console.log(JSON.stringify({{
          hasDetails: html.includes('<details'),
          hasRisk: html.includes('analysis-tone-risk'),
          citation: html.includes('引用证据'),
          highlights: (html.match(/<mark/g) || []).length,
          emptyTopic: html.includes('空主题')
        }}));
        """
    )

    assert result == {
        "hasDetails": False,
        "hasRisk": True,
        "citation": True,
        "highlights": 2,
        "emptyTopic": False,
    }


def test_progressive_renderer_uses_report_layout_and_deduplicates_evidence():
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'completed',
          quick: {{ conclusions: [
            {{ id: 'q1', text: '现金流承压', style: 'verified_risk',
               evidence_ids: ['pdf-12', 'structured-1', 'ocr-text-13', 'ocr-table-14', 'chart-15'] }},
            {{ id: 'q2', text: '资本保持充足', evidence_ids: ['pdf-12'] }}
          ] }},
          sections: [{{
            section_id: 'cash', title: '现金流', findings: [
              {{ claim: '经营现金流下降', evidence_ids: ['pdf-12'] }},
              {{ claim: '融资成本上升', evidence_ids: ['pdf-12', 'structured-1', 'ocr-text-13'] }}
            ]
          }}],
          evidence_catalog: {{
            'pdf-12': {{ label: '合并现金流量表', excerpt: '经营现金流下降',
                         source_type: 'pdf_text', source_locator: {{ page: 12 }} }},
            'structured-1': {{ label: '营业收入', value: '100', unit: '亿元', period: '2025-12-31',
                               source_type: 'structured', source_locator: {{ page: 16 }} }},
            'ocr-text-13': {{ label: 'OCR 文字页', source_type: 'ocr_text', source_locator: {{ page: 13 }} }},
            'ocr-table-14': {{ label: 'OCR 表格页', source_type: 'ocr_table', source_locator: {{ page: 14 }} }},
            'chart-15': {{ label: 'OCR 图表页', source_type: 'chart', source_locator: {{ page: 15 }} }}
          }}
        }});
        console.log(JSON.stringify({{
          summary: html.includes('analysis-report-summary'),
          summaryCount: html.includes('本期要点（2）'),
          reportBody: html.includes('analysis-report-body'),
          legacyCards: html.includes('class="analysis-finding"'),
          riskLabel: html.includes('>风险<'),
          quickTabCount: html.includes('01 快速结论（2）'),
          sectionTabCount: html.includes('02 现金流（2）'),
          evidenceHeading: html.includes('证据与出处（5）'),
          pdfButtonOnce: (html.match(/data-evidence-page="12"/g) || []).length === 1,
          ocrPages: [13, 14, 15].every(page => html.includes('data-evidence-page="' + page + '"')),
          pdfPage: html.includes('PDF · 第 12 页'),
          structuredHasButton: html.includes('data-evidence-page="16"'),
          bodyHasDetails: html.includes('<details')
        }}));
        """
    )

    assert result == {
        "summary": True,
        "summaryCount": True,
        "reportBody": True,
        "legacyCards": False,
        "riskLabel": True,
        "quickTabCount": True,
        "sectionTabCount": True,
        "evidenceHeading": True,
        "pdfButtonOnce": True,
        "ocrPages": True,
        "pdfPage": True,
        "structuredHasButton": False,
        "bodyHasDetails": False,
    }


def test_progressive_renderer_can_disable_pdf_evidence_links_for_history_detail():
    """历史详情不支持主预览跳页时，应保留页码文字但不能渲染无效按钮。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const state = {{
          activeTab: 'quick', stage: 'completed',
          quick: {{ conclusions: [{{ text: '现金流承压', evidence_ids: ['pdf-12'] }}] }},
          evidence_catalog: {{
            'pdf-12': {{ label: '合并现金流量表', source_type: 'pdf_text',
                         source_locator: {{ page: 12 }} }}
          }}
        }};
        const primary = workflow.renderProgressiveAnalysis(state);
        const history = workflow.renderProgressiveAnalysis(state, {{ pdfEvidenceLinks: false }});
        console.log(JSON.stringify({{
          primaryButton: primary.includes('data-evidence-page="12"'),
          historyButton: history.includes('data-evidence-page="12"'),
          historyPageLabel: history.includes('PDF · 第 12 页')
        }}));
        """
    )

    assert result == {
        "primaryButton": True,
        "historyButton": False,
        "historyPageLabel": True,
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
