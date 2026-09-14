"""智能问答 Markdown 与网页来源展示的前端回归测试。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CHAT_RENDERING_JS = ROOT / "webapp" / "static" / "chat_rendering.js"
APP_JS = ROOT / "webapp" / "static" / "app.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="前端渲染回归测试需要 Node.js")


def _run_node(source: str) -> dict:
    completed = subprocess.run(
        [NODE, "-e", source], cwd=ROOT, check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout)


def test_chat_renderer_normalizes_html_sup_citations_to_markdown_labels():
    """模型返回 HTML/转义 HTML 上标时，聊天内容应显示为可读的 [n]。"""
    result = _run_node(
        f"""
        let rendering = {{}};
        try {{ rendering = require({json.dumps(str(CHAT_RENDERING_JS))}); }} catch (_) {{}}
        const normalize = rendering.normalizeAssistantMarkdown || (value => value);
        console.log(JSON.stringify({{
          plain: normalize('结论<sup>1</sup> 来自工具'),
          escaped: normalize('结论\\\\<sup>2\\\\</sup> 来自网页')
        }}));
        """
    )

    assert result == {"plain": "结论[1] 来自工具", "escaped": "结论[2] 来自网页"}


def test_chat_renderer_keeps_only_web_source_citation_fields():
    """网页来源的展示数据不得携带用于模型核验的长摘要正文。"""
    result = _run_node(
        f"""
        let rendering = {{}};
        try {{ rendering = require({json.dumps(str(CHAT_RENDERING_JS))}); }} catch (_) {{}}
        const references = rendering.webSourceReferences || (items => items);
        console.log(JSON.stringify(references([{{
          title: '新浪财经', url: 'https://finance.sina.com.cn/a',
          content: '这段摘要只供模型核验，不能展示给用户。', published_date: '2026-09-04'
        }}])));
        """
    )

    assert result == [{
        "title": "新浪财经", "url": "https://finance.sina.com.cn/a", "published_date": "2026-09-04",
    }]


def test_scope_renderer_identifies_local_industry_sample_and_fallback():
    """行业范围必须标注「本地可检索同业样本」，绝不承诺全市场排名。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        console.log(JSON.stringify(rendering.renderScope({{
          mode: 'company_industry',
          companies: [{{code: '601288', name: '农业银行'}}],
          industry: {{name: '银行业', sample_kind: 'local_indexed'}},
          report_ids: ['601288:2026-06-30:semi_annual'],
          fallback_reason: ''
        }})));
        """
    )

    assert "本地可检索同业样本" in result
    assert "601288" in result
    assert "农业银行" in result
    assert "银行业（数据源分类）" in result
    assert "2026 半年报" in result


def test_scope_renderer_derives_periods_from_report_ids_and_escapes():
    """展示期次必须从 report_ids 逐条推导，且所有文本转义，绝不注入 HTML。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        const multi = rendering.renderScope({{
          mode: 'company_only',
          companies: [{{code: '601288', name: '<农业银行>'}}],
          report_ids: ['601288:2024-12-31:annual', '601288:2026-06-30:semi_annual'],
          fallback_reason: '<注入>'
        }});
        const whole = rendering.renderScope({{mode: 'whole_corpus', companies: [], report_ids: []}});
        console.log(JSON.stringify({{multi, whole}}));
        """
    )

    assert "2024 年报" in result["multi"]
    assert "2026 半年报" in result["multi"]
    assert "<农业银行>" not in result["multi"]
    assert "&lt;农业银行&gt;" in result["multi"]
    assert "<注入>" not in result["multi"]
    assert "全库财报检索" in result["whole"]


def test_pdf_artifact_renders_only_one_collapsed_page_link_or_unavailable_state():
    """默认收起的证据区只显示 PDF 页码跳转，不展示片段。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        const available = rendering.renderRunArtifacts({{
          artifacts: [{{
            source: 'pdf', report_id: '601288:2026-06-30:semi_annual',
            pdf_filename: '农业银行_601288_半年报_2026.pdf', page: 12,
            snippet: '营业收入为 862 亿元',
            pdf_url: '/api/history-pdf/x.pdf?jump=12345#page=12', availability: 'available'
          }}]
        }});
        const missing = rendering.renderRunArtifacts({{
          artifacts: [{{
            source: 'pdf', report_id: '601288:2026-06-30:semi_annual',
            pdf_filename: '农业银行_601288_半年报_2026.pdf', page: 12,
            snippet: '营业收入为 862 亿元',
            pdf_url: null, availability: 'missing_file'
          }}]
        }});
        console.log(JSON.stringify({{available, missing}}));
        """
    )

    assert '<details class="chat-artifacts"' in result["available"]
    assert '<summary>证据与来源（1）</summary>' in result["available"]
    assert 'data-chat-pdf-page="12"' in result["available"]
    assert "/api/history-pdf/x.pdf" in result["available"]
    assert "PDF · 第 12 页" in result["available"]
    assert "营业收入为 862 亿元" not in result["available"]
    assert "片段" not in result["available"]
    assert "data-chat-pdf-page" not in result["missing"]
    assert "PDF · 第 12 页（文件不可用）" in result["missing"]


def test_artifacts_deduplicate_same_pdf_page_and_collapse_many_pages_to_home_link():
    """同一页 PDF 合并；同一 PDF 超过三页时只给首页入口；网页和工具也去重。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        const html = rendering.renderRunArtifacts({{
          artifacts: [
            {{source: 'pdf', report_id: '600900:2026-06-30:semi_annual', pdf_filename: '长江电力.pdf', page: 1, snippet: 'a', pdf_url: '/api/history-pdf/yangtze.pdf?jump=1#page=1'}},
            {{source: 'pdf', report_id: '600900:2026-06-30:semi_annual', pdf_filename: '长江电力.pdf', page: 1, snippet: 'duplicate', pdf_url: '/api/history-pdf/yangtze.pdf?jump=2#page=1'}},
            {{source: 'pdf', report_id: '600900:2026-06-30:semi_annual', pdf_filename: '长江电力.pdf', page: 2, snippet: 'b', pdf_url: '/api/history-pdf/yangtze.pdf?jump=3#page=2'}},
            {{source: 'pdf', report_id: '600900:2026-06-30:semi_annual', pdf_filename: '长江电力.pdf', page: 3, snippet: 'c', pdf_url: '/api/history-pdf/yangtze.pdf?jump=4#page=3'}},
            {{source: 'pdf', report_id: '600900:2026-06-30:semi_annual', pdf_filename: '长江电力.pdf', page: 4, snippet: 'd', pdf_url: '/api/history-pdf/yangtze.pdf?jump=5#page=4'}},
            {{source: 'web', url: 'https://example.com/news', title: '公告', snippet: 'web detail'}},
            {{source: 'web', url: 'https://example.com/news', title: '公告重复', snippet: 'web duplicate'}},
          ],
          tool_artifacts: [
            {{provider: 'market', tool_name: 'price', as_of: '2026-09-10', status: 'success', result_summary: 'r1'}},
            {{provider: 'market', tool_name: 'volume', as_of: '2026-09-10', status: 'success', result_summary: 'r2'}},
          ]
        }});
        console.log(JSON.stringify({{html}}));
        """
    )

    html = result["html"]
    assert '<summary>证据与来源（3）</summary>' in html
    assert html.count('data-chat-pdf-home="true"') == 1
    assert '#page=1' in html
    assert 'data-chat-pdf-page=' not in html
    assert html.count('https://example.com/news') == 1
    assert html.count('market') == 1
    assert all(value not in html for value in ('duplicate', 'web detail', '参数摘要', '结果摘要'))


def test_pdf_with_exactly_three_pages_keeps_each_page_link():
    """阈值是超过三页；恰好三页仍应保留三个精准页码链接。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        const html = rendering.renderRunArtifacts({{
          artifacts: [1, 2, 3].map(page => ({{
            source: 'pdf', report_id: '600900:2026-06-30:semi_annual',
            pdf_filename: '长江电力.pdf', page,
            pdf_url: '/api/history-pdf/yangtze.pdf?jump=1#page=' + page
          }}))
        }});
        console.log(JSON.stringify({{html}}));
        """
    )

    html = result["html"]
    assert '<summary>证据与来源（3）</summary>' in html
    assert html.count('data-chat-pdf-page=') == 3
    assert 'data-chat-pdf-home=' not in html


def test_pdf_artifact_rejects_unsafe_or_non_positive_page_urls():
    """PDF 跳页 URL 只接受服务端同源相对路径；不可信输入渲染为不可用。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        const dangerous = rendering.renderRunArtifacts({{
          artifacts: [{{
            source: 'pdf', report_id: 'r', pdf_filename: 'a.pdf', page: 1,
            snippet: 's', pdf_url: 'javascript:alert(1)', availability: 'available'
          }}]
        }});
        const zeroPage = rendering.renderRunArtifacts({{
          artifacts: [{{
            source: 'pdf', report_id: 'r', pdf_filename: 'a.pdf', page: 0,
            snippet: 's', pdf_url: '/api/history-pdf/a.pdf#page=1', availability: 'available'
          }}]
        }});
        console.log(JSON.stringify({{dangerous, zeroPage}}));
        """
    )

    assert "javascript:" not in result["dangerous"]
    assert "data-chat-pdf-page" not in result["dangerous"]
    assert "PDF · 第 1 页（文件不可用）" in result["dangerous"]
    assert "data-chat-pdf-page" not in result["zeroPage"]


def test_web_artifact_requires_http_url_and_escapes_title():
    """网页证据只接受 http/https URL，标题与摘要必须转义。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        const ok = rendering.renderRunArtifacts({{
          artifacts: [{{
            source: 'web', url: 'https://finance.example.com/a',
            title: '<公告>', snippet: '摘要', published_at: '2026-09-01', fetched_at: '2026-09-02'
          }}]
        }});
        const bad = rendering.renderRunArtifacts({{
          artifacts: [{{
            source: 'web', url: 'ftp://example.com/a', title: 'x', snippet: 'y',
            published_at: '', fetched_at: '2026-09-02'
          }}]
        }});
        console.log(JSON.stringify({{ok, bad}}));
        """
    )

    assert "https://finance.example.com/a" in result["ok"]
    assert "&lt;公告&gt;" in result["ok"]
    assert "<公告>" not in result["ok"]
    assert "发布于" not in result["ok"]
    assert "抓取于" not in result["ok"]
    assert "摘要" not in result["ok"]
    assert "ftp://" not in result["bad"]


def test_run_status_renders_regenerate_for_stopped_and_disabled_continue():
    """停止/部分/失败给出清晰文本与「重新生成」；恢复占位禁用，不承诺 M3 能力。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        const stopped = rendering.renderRunStatus({{status: 'stopped'}});
        const partial = rendering.renderRunStatus({{status: 'partial'}});
        const failed = rendering.renderRunStatus({{status: 'failed'}});
        const completed = rendering.renderRunStatus({{status: 'completed'}});
        const legacy = rendering.renderRunStatus({{status: 'completed', legacy_evidence_unavailable: true}});
        console.log(JSON.stringify({{stopped, partial, failed, completed, legacy}}));
        """
    )

    assert "已停止" in result["stopped"]
    assert "重新生成" in result["stopped"]
    assert 'data-chat-action="continue"' in result["stopped"]
    assert "disabled" in result["stopped"]
    assert "部分完成" in result["partial"]
    assert "重新生成" in result["partial"]
    assert "失败" in result["failed"]
    assert "重新生成" in result["failed"]
    assert "已完成" in result["completed"]
    assert "重新生成" not in result["completed"]
    assert "历史回答，未保留证据包" in result["legacy"]


def test_tool_artifact_renders_only_provider_and_as_of_without_summaries():
    """无跳转 URL 的工具证据仅保留来源与数据截至时间。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        const html = rendering.renderRunArtifacts({{
          tool_artifacts: [{{
            provider: 'stock-data-mcp', tool_name: 'get_financial_metrics',
            as_of: '2026-09-10T10:00:00', status: 'success',
            arguments_summary: '{{"code": "601288", "metric": "营收',
            result_summary: '营业收入为 862 亿元'
          }}]
        }});
        console.log(JSON.stringify({{html}}));
        """
    )

    assert "stock-data-mcp" in result["html"]
    assert "数据截至 2026-09-10T10:00:00" in result["html"]
    assert "get_financial_metrics" not in result["html"]
    assert "参数摘要" not in result["html"]
    assert "结果摘要" not in result["html"]
    assert "营业收入为 862 亿元" not in result["html"]


def test_chat_focus_bar_clears_label_when_no_focus_report():
    """无聚焦报告时 renderChatFocusBar 不抛错且清空标签，聚焦存在时文案不变。

    浏览器回归（tests/browser/test_chat_trust_flow.py）暴露了 ``STATE.chatFocusReport``
    为 null 时直接读取 ``fr.company`` 的运行时错误；本测试在纯 JS 环境复现该守卫，
    未加守卫时（读取空引用）会在 Node 抛错，从而构成红态。
    """
    result = _run_node(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const src = fs.readFileSync({json.dumps(str(APP_JS))}, 'utf8');
        const start = src.indexOf('function renderChatFocusBar() {{');
        const end = src.indexOf('function currentScopeMode()', start);
        if (start < 0 || end < 0) throw new Error('renderChatFocusBar/currentScopeMode 未找到');
        const fnSource = src.slice(start, end);

        function runWith(focusReport) {{
          const events = [];
          const label = {{ textContent: 'STALE' }};
          const bar = {{ classList: {{ add: (c) => events.push('bar-add:' + c), remove: (c) => events.push('bar-remove:' + c) }} }};
          const sandbox = {{
            $: (sel) => sel === '#chat-focus-bar' ? bar : (sel === '#chat-focus-label' ? label : null),
            $$: () => [],
            STATE: {{ chatFocusReport: focusReport }},
            renderChatScopeBar: () => events.push('scope-bar'),
          }};
          vm.createContext(sandbox);
          vm.runInContext(fnSource + '\\nrenderChatFocusBar();', sandbox);
          return {{ labelText: label.textContent, events }};
        }}

        let nullThrew = false;
        let nullResult = null;
        try {{ nullResult = runWith(null); }} catch (e) {{ nullThrew = true; }}
        const withCompany = runWith({{ code: '601288', period: '2026-06-30', company: '农业银行' }});
        console.log(JSON.stringify({{ nullThrew, nullResult, withCompany }}));
        """
    )

    assert result["nullThrew"] is False
    assert result["nullResult"]["labelText"] == ""
    assert "bar-add:hidden" in result["nullResult"]["events"]
    assert "scope-bar" in result["nullResult"]["events"]
    assert result["withCompany"]["labelText"] == "聚焦报告：农业银行（601288 · 2026-06-30）——检索优先本报告"
    assert "bar-add:hidden" not in result["withCompany"]["events"]
    assert "bar-remove:hidden" in result["withCompany"]["events"]
