"""渐进式财报分析前端契约测试。"""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from financial_report_fetcher.analysis_result import (
    AnalysisDocument,
    EvidenceReference,
    EvidenceSummary,
    QuickConclusion,
    QuickResult,
)
from financial_report_fetcher.evidence.models import (
    EntityScope,
    SourceLocator,
    SourceType,
    VerificationState,
)


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
          citation: html.includes('PDF 第 12 页'),
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
          pdfButtonOnce: (html.match(/data-evidence-page="12"/g) || []).length === 2,
          ocrPages: [13, 14, 15].some(page => html.includes('data-evidence-page="' + page + '"')),
          pdfPage: html.includes('PDF 第 12 页'),
          structuredHasButton: html.includes('data-evidence-page="16"'),
          bodyHasDetails: html.includes('<details')
        }}));
        """
    )

    assert result == {
        "summary": False,
        "summaryCount": False,
        "reportBody": True,
        "legacyCards": False,
        "riskLabel": True,
        "quickTabCount": True,
        "sectionTabCount": True,
        "evidenceHeading": False,
        "pdfButtonOnce": True,
        "ocrPages": False,
        "pdfPage": True,
        "structuredHasButton": False,
        "bodyHasDetails": False,
    }


def test_readable_duplicate_key_data_keeps_quick_conclusion_compact():
    """可读但重复的 key_data 不能把一句结论拆成卡片或重复同一批数字。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'completed',
          quick: {{ conclusions: [{{
            claim: '2026年上半年，公司营业收入为4108.71亿元，净利润为1480.62亿元。',
            key_data: '营业收入4108.71亿元，净利润1480.62亿元',
            significance: 'high', highlight_spans: ['4108.71亿元'],
            evidence_ids: ['pdf']
          }}] }},
          evidence_catalog: {{ pdf: {{ source_type: 'pdf_text',
            source_locator: {{ page: 8 }} }} }}
        }});
        console.log(JSON.stringify({{
          compact: html.includes('analysis-compact-finding'),
          card: html.includes('analysis-report-finding'),
          label: html.includes('analysis-finding-label'),
          keyDataLine: html.includes('analysis-key-data'),
          highlight: html.includes('<mark>4108.71亿元</mark>'),
          numberRepeats: html.split('1480.62亿元').length - 1,
          pdf: html.includes('data-evidence-page="8"')
        }}));
        """
    )

    assert result == {
        "compact": True,
        "card": False,
        "label": False,
        "keyDataLine": False,
        "highlight": True,
        "numberRepeats": 1,
        "pdf": True,
    }


def test_non_redundant_key_data_stays_inside_the_same_paragraph():
    """key_data 携带结论里没有的数据时必须保留，但不能因此变成独立模块。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'risk', stage: 'completed', sections: [{{
            section_id: 'risk', title: '信用风险',
            findings: [{{ claim: '资产质量总体保持稳定。',
                         key_data: '不良贷款率 1.05 1.13' }}]
          }}]
        }});
        console.log(JSON.stringify({{
          compact: html.includes('analysis-compact-finding'),
          card: html.includes('analysis-report-finding'),
          inlineSupplement: html.includes('不良贷款率 1.05 1.13')
        }}));
        """
    )

    assert result == {"compact": True, "card": False, "inlineSupplement": True}


def test_compact_conclusion_only_appends_the_missing_key_data_parts():
    """补充数据只接上结论里缺失的部分，不能把 key_data 整句重复一遍。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'completed',
          quick: {{ conclusions: [{{
            claim: '信用减值损失为1106.2亿元，同比增加，需关注资产质量风险。',
            key_data: '信用减值损失1106.2亿元，同比增加12.9%'
          }}] }}
        }});
        console.log(JSON.stringify({{
          compact: html.includes('analysis-compact-finding'),
          card: html.includes('analysis-report-finding'),
          kept: html.includes('同比增加12.9%'),
          repeated: (html.split('信用减值损失').length - 1)
        }}));
        """
    )

    assert result == {"compact": True, "card": False, "kept": True, "repeated": 1}


def test_compact_conclusion_keeps_key_data_for_a_different_metric_with_same_value():
    """数值相同但指标不同的 key_data 不能被当成重述删除（营收 100 亿 vs 成本 100 亿）。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'completed',
          quick: {{ conclusions: [{{
            claim: '营业收入为100亿元。',
            key_data: '营业成本为100亿元'
          }}] }}
        }});
        console.log(JSON.stringify({{
          compact: html.includes('analysis-compact-finding'),
          kept: html.includes('营业成本为100亿元')
        }}));
        """
    )

    assert result == {"compact": True, "kept": True}


def test_compact_conclusion_drops_key_data_restated_in_different_words():
    """key_data 只是换个说法重述同一批数字时，不要产生重复补充句。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'completed',
          quick: {{ conclusions: [{{
            claim: '经营活动现金流净额为2876.12亿元，投资活动现金流净额为-13607.84亿元。',
            key_data: '经营现金流2876.12亿元，投资现金流-13607.84亿元'
          }}] }}
        }});
        console.log(JSON.stringify({{
          compact: html.includes('analysis-compact-finding'),
          supplement: html.includes('analysis-key-data-inline')
        }}));
        """
    )

    assert result == {"compact": True, "supplement": False}


def test_compact_conclusion_keeps_key_data_with_a_reused_number():
    """同一数字出现在不同指标里时，key_data 不能被当成重复内容删除。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'completed',
          quick: {{ conclusions: [{{
            claim: '本期净利润为12.5亿元，盈利能力提升。',
            key_data: '净利润同比增长12.5%'
          }}] }}
        }});
        console.log(JSON.stringify({{
          compact: html.includes('analysis-compact-finding'),
          kept: html.includes('净利润同比增长12.5%')
        }}));
        """
    )

    assert result == {"compact": True, "kept": True}


def test_compact_conclusion_keeps_plain_text_supplement():
    """非数字补充信息（如待复核）不能因为压平而丢失。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'completed',
          quick: {{ conclusions: [{{ text: '旧缓存结论', key_data: '待复核' }}] }}
        }});
        console.log(JSON.stringify({{
          compact: html.includes('analysis-compact-finding'),
          kept: html.includes('待复核'),
          label: html.includes('analysis-finding-label')
        }}));
        """
    )

    assert result == {"compact": True, "kept": True, "label": False}


def test_risk_finding_keeps_its_layered_risk_label():
    """风险类结论必须保留红色标签，不能被压平成普通段落。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'risk', stage: 'completed', sections: [{{
            section_id: 'risk', title: '信用风险',
            findings: [{{ claim: '不良率上升，需关注资产质量。', risk_state: 'verified_risk' }}]
          }}]
        }});
        console.log(JSON.stringify({{
          compact: html.includes('analysis-compact-finding'),
          tone: html.includes('analysis-tone-risk'),
          label: html.includes('>风险<')
        }}));
        """
    )

    assert result == {"compact": False, "tone": True, "label": True}


def test_progressive_renderer_keeps_short_quick_conclusions_in_one_compact_flow():
    """短结论不应被重复摘要、标签和卡片层级拆散。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'completed',
          quick: {{ conclusions: [{{ text: '经营现金流同比下降 10%', evidence_ids: ['e1'] }}] }},
          evidence_catalog: {{ e1: {{ label: '现金流量表', source_type: 'pdf_text',
            source_locator: {{ page: 12 }} }} }}
        }}, {{ evidenceAnchor: 'analysis-evidence-main' }});
        console.log(JSON.stringify({{
          summary: html.includes('analysis-report-summary'),
          label: html.includes('analysis-finding-label'),
          finding: html.includes('analysis-report-finding'),
          citationTarget: html.includes('data-evidence-page="12"'),
          evidenceTarget: html.includes('analysis-evidence-item'),
          collapsedEvidence: html.includes('<details class="analysis-evidence-section"')
        }}));
        """
    )

    assert result == {
        "summary": False,
        "label": False,
        "finding": False,
        "citationTarget": True,
        "evidenceTarget": False,
        "collapsedEvidence": False,
    }


def test_progressive_renderer_keeps_machine_metadata_out_of_short_conclusions():
    """对象数据和 high/medium 等内部等级不能把一句结论拆成卡片。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'completed',
          quick: {{ conclusions: [{{
            text: '营业收入同比增长 10.5%。',
            key_data: "{{'revenue': 206255000000}}", significance: 'high', evidence_ids: ['e1']
          }}] }},
          evidence_catalog: {{ e1: {{ label: '利润表', source_type: 'pdf_text',
            source_locator: {{ page: 8 }} }} }}
        }});
        console.log(JSON.stringify({{
          compact: html.includes('analysis-compact-finding'),
          card: html.includes('analysis-report-finding'),
          machineObject: html.includes('206255000000'),
          machineLevel: html.includes('>high<')
        }}));
        """
    )

    assert result == {
        "compact": True,
        "card": False,
        "machineObject": False,
        "machineLevel": False,
    }


def test_progressive_renderer_does_not_split_findings_for_internal_sentiment_levels():
    """positive/negative/neutral 也是内部等级，不能使现金流条目变成卡片。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'cash', stage: 'completed', sections: [{{
            section_id: 'cash', title: '现金流量与流动性分析', summary: '不显示',
            findings: [{{ claim: '经营活动现金流净额为正。', significance: 'positive' }},
                       {{ claim: '投资活动现金净流出。', significance: 'negative' }}]
          }}]
        }});
        console.log(JSON.stringify({{
          compact: (html.match(/analysis-compact-finding/g) || []).length,
          cards: html.includes('analysis-report-finding'),
          summary: html.includes('不显示')
        }}));
        """
    )

    assert result == {"compact": 2, "cards": False, "summary": False}


def test_progressive_renderer_keeps_only_direct_pdf_and_web_evidence_links():
    """结构化/OCR 记录不能占用结论区；仅 PDF 页码与网页 URL 可作为证据入口。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({{
          activeTab: 'quick', stage: 'completed',
          quick: {{ conclusions: [{{ text: '现金流保持充足。', evidence_ids: ['pdf', 'web', 'structured', 'ocr'] }}] }},
          evidence_catalog: {{
            pdf: {{ source_type: 'pdf_text', source_locator: {{ page: 15 }} }},
            web: {{ source_type: 'web', url: 'https://example.com/source' }},
            structured: {{ source_type: 'structured', source_locator: {{ page: 3 }} }},
            ocr: {{ source_type: 'ocr_text', source_locator: {{ page: 16 }} }}
          }}
        }});
        console.log(JSON.stringify({{
          pdf: html.includes('data-evidence-page="15"'),
          web: html.includes('href="https://example.com/source"'),
          structured: html.includes('data-evidence-page="3"'),
          ocr: html.includes('data-evidence-page="16"'),
          bottomCatalog: html.includes('证据与出处'),
          sectionHeading: html.includes('analysis-section-summary')
        }}));
        """
    )

    assert result == {
        "pdf": True,
        "web": True,
        "structured": False,
        "ocr": False,
        "bottomCatalog": False,
        "sectionHeading": False,
    }


def test_progressive_renderer_uses_real_document_catalog_display_metadata():
    """渲染器必须消费真实 AnalysisDocument.to_dict() 中的可读证据元数据。"""
    document = AnalysisDocument(
        schema_version=3,
        analysis_id="analysis-1",
        report_id="601288:2025-12-31:annual",
        interests=[],
        stage="completed",
        quick=QuickResult(conclusions=[QuickConclusion(
            conclusion_id="q1",
            claim="经营现金流下降",
            key_data="",
            significance="",
            evidence_ids=("pdf-12",),
            verification_state=VerificationState.SINGLE_SOURCE,
        )]),
        sections=[],
        observations=[],
        filtered_topics=[],
        evidence_catalog={"pdf-12": EvidenceReference(
            EntityScope.CONSOLIDATED,
            "2025-12-31",
            None,
            None,
            SourceType.PDF_TEXT,
            SourceLocator(provider="pdf", page=12, record_id="page-12"),
            VerificationState.SINGLE_SOURCE,
            fact_name="pdf_page_12",
            label="PDF · 第 12 页",
            excerpt="经营现金流下降 10%",
        )},
        evidence_summary=EvidenceSummary(total=1, single_source=1),
        errors=[],
        created_at="2026-09-08T00:00:00Z",
        updated_at="2026-09-08T00:00:00Z",
    ).to_dict()
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const html = workflow.renderProgressiveAnalysis({json.dumps(document, ensure_ascii=False)});
        console.log(JSON.stringify({{
          label: html.includes('PDF 第 12 页'),
          excerpt: html.includes('经营现金流下降 10%'),
          opaqueId: html.includes('pdf-12')
        }}));
        """
    )

    assert result == {"label": True, "excerpt": False, "opaqueId": False}


def test_progressive_renderer_uses_unique_evidence_anchors_and_inline_notes():
    """同时渲染主/历史详情时，引用必须命中各自证据区，普通结论不再挂待核验标签。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const state = {{
          activeTab: 'quick', stage: 'completed',
          quick: {{ conclusions: [{{ text: '旧缓存结论', key_data: '待复核', evidence_ids: ['pdf-12'] }}] }},
          evidence_catalog: {{
            'pdf-12': {{ fact_name: 'pdf_page_12', label: 'PDF · 第 12 页',
                         excerpt: '经营现金流下降', source_type: 'pdf_text',
                         source_locator: {{ page: 12 }} }}
          }}
        }};
        const main = workflow.renderProgressiveAnalysis(
          state, {{ evidenceAnchor: 'analysis-evidence-main' }}
        );
        const history = workflow.renderProgressiveAnalysis(
          state, {{ evidenceAnchor: 'analysis-evidence-history', pdfEvidenceLinks: false }}
        );
        console.log(JSON.stringify({{
          mainTarget: main.includes('data-evidence-page="12"'),
          historyTarget: history.includes('data-evidence-page="12"'),
          duplicateAnchor: (main + history).match(/data-evidence-page="12"/g)?.length || 0,
          pendingTone: main.includes('analysis-tone-pending'),
          pendingLabel: main.includes('>待核验<'),
          inlineNote: main.includes('待复核')
        }}));
        """
    )

    assert result == {
        "mainTarget": True,
        "historyTarget": True,
        "duplicateAnchor": 2,
        "pendingTone": False,
        "pendingLabel": False,
        "inlineNote": True,
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
        "historyButton": True,
        "historyPageLabel": False,
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
