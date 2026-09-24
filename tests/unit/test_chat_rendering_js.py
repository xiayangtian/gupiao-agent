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


def test_chat_renderer_removes_leaked_dsml_tool_call_markup_but_keeps_answer_text():
    """模型把 DSML/XML 工具调用作为正文输出时，聊天不得展示原始标记。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        const raw = '先说明走势。<｜DSML｜ calls><｜DSML｜ invoke name="stock_prices">'
          + '<｜DSML｜ parameter name="symbol" string="true">600900</｜DSML｜ parameter>'
          + '</｜DSML｜ invoke></｜DSML｜ calls>再说明风险。';
        console.log(JSON.stringify({{normalized: rendering.normalizeAssistantMarkdown(raw)}}));
        """
    )

    assert result["normalized"] == "先说明走势。再说明风险。"


def test_chat_renderer_removes_double_delimiter_dsml_markup_from_actual_provider_format():
    """真实提供方使用 `｜｜DSML｜｜` 双竖线包裹，必须同样被完整剥离。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        const raw = '图表结论。<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="stock_prices">'
          + '<｜｜DSML｜｜ parameter name="symbol" string="true">600900</｜｜DSML｜｜ parameter>'
          + '<｜｜DSML｜｜ parameter name="period" string="true">daily</｜｜DSML｜｜ parameter>'
          + '</｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>请结合数据判断。';
        console.log(JSON.stringify({{normalized: rendering.normalizeAssistantMarkdown(raw)}}));
        """
    )

    assert result["normalized"] == "图表结论。请结合数据判断。"


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
        const noRetrieval = rendering.renderScope(
          {{mode: 'whole_corpus', companies: [], report_ids: []}},
          {{source_summary: {{local_pdf: '未使用'}}, execution_plan: {{steps: [{{kind: 'web_search'}}]}}}}
        );
        const retrieved = rendering.renderScope(
          {{mode: 'whole_corpus', companies: [], report_ids: []}},
          {{source_summary: {{local_pdf: '已使用'}}, execution_plan: {{steps: [{{kind: 'retrieve'}}]}}}}
        );
        console.log(JSON.stringify({{multi, whole, noRetrieval, retrieved}}));
        """
    )

    assert "2024 年报" in result["multi"]
    assert "2026 半年报" in result["multi"]
    assert "<农业银行>" not in result["multi"]
    assert "&lt;农业银行&gt;" in result["multi"]
    # 未执行 retrieve 时，不得把范围选择误描述为财报检索。
    assert "全库范围（未检索财报）" in result["noRetrieval"]
    assert "全库财报检索" in result["retrieved"]
    assert "<注入>" not in result["multi"]
    # 旧会话缺少执行记录时也必须避免暗示已检索财报。
    assert "全库范围（未检索财报）" in result["whole"]


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
    assert '<summary>证据与来源（4）</summary>' in html
    assert html.count('data-chat-pdf-home="true"') == 1
    assert '#page=1' in html
    assert 'data-chat-pdf-page=' not in html
    assert html.count('https://example.com/news') == 1
    assert html.count('market') == 2
    assert all(value not in html for value in ('duplicate', 'web detail', '参数摘要', '结果摘要'))


def test_tool_entries_with_different_as_of_remain_distinct():
    """同一来源但数据截至时间不同，不得被过度合并。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        const html = rendering.renderRunArtifacts({{
          tool_artifacts: [
            {{provider: 'market', tool_name: 'price', as_of: '2026-09-10T09:00:00'}},
            {{provider: 'market', tool_name: 'price', as_of: '2026-09-10T10:00:00'}},
          ]
        }});
        console.log(JSON.stringify({{html}}));
        """
    )

    html = result["html"]
    assert html.count('market') == 2
    assert '数据截至 2026-09-10T09:00:00' in html
    assert '数据截至 2026-09-10T10:00:00' in html


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


def test_run_status_renders_regenerate_for_incomplete_runs_without_a_fake_continue_placeholder():
    """停止/部分/失败给出清晰文本与「重新生成」；恢复入口只由真实研究运行提供。"""
    result = _run_node(
        f"""
        const rendering = require({json.dumps(str(CHAT_RENDERING_JS))});
        const stopped = rendering.renderRunStatus({{status: 'stopped', id: 'run-stopped'}});
        const partial = rendering.renderRunStatus({{status: 'partial', id: 'run-partial'}});
        const failed = rendering.renderRunStatus({{status: 'failed', id: 'run-failed'}});
        const completed = rendering.renderRunStatus({{status: 'completed', id: 'run-debug-123'}});
        const legacy = rendering.renderRunStatus({{status: 'completed', legacy_evidence_unavailable: true}});
        console.log(JSON.stringify({{stopped, partial, failed, completed, legacy}}));
        """
    )

    assert "已停止" in result["stopped"]
    assert "诊断 ID：<code>run-stopped</code>" in result["stopped"]
    assert 'data-chat-action="copy-run-id"' in result["stopped"]
    assert "重新生成" in result["stopped"]
    assert 'data-chat-action="continue"' not in result["stopped"]
    assert "继续研究" not in result["stopped"]
    assert "部分完成" in result["partial"]
    assert "诊断 ID：<code>run-partial</code>" in result["partial"]
    assert "重新生成" in result["partial"]
    assert "失败" in result["failed"]
    assert "诊断 ID：<code>run-failed</code>" in result["failed"]
    assert "重新生成" in result["failed"]
    assert "已完成" in result["completed"]
    assert "诊断 ID：<code>run-debug-123</code>" in result["completed"]
    assert 'data-chat-action="copy-run-id"' in result["completed"]
    assert 'data-chat-run-id="run-debug-123"' in result["completed"]
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


def test_m2_rendering_marks_reference_conflict_and_business_policy():
    result = _run_node(
        f"""
        const r = require({json.dumps(str(CHAT_RENDERING_JS))});
        console.log(JSON.stringify({{
          policy: r.renderPolicy({{intent: 'realtime_market', allowed_tools: ['get_quote']}}),
          facts: r.renderFactsAndConflicts({{facts: [{{metric: '价格', value: 10, unit: '元/股', verification: 'reference', source_type: 'tool', as_of: '2026-09-15'}}], conflicts: [{{reason: '同指标同期间同口径数值不一致', facts: [{{source_type: 'pdf'}}, {{source_type: 'tool'}}]}}]}}),
          verification: r.renderVerification({{status: 'partial'}})
        }}));
        """
    )
    assert '实时行情' in result['policy'] and 'get_quote' not in result['policy']
    assert '外部参考' in result['facts'] and '已确认' not in result['facts']
    assert '存在口径/时间差异' in result['facts'] and 'PDF 原文' in result['facts'] and '实时数据' in result['facts']
    assert 'role="status"' in result['verification']


def test_research_rendering_shows_business_steps_acceptance_and_recovery_only_when_resumable():
    result = _run_node(
        f"""
        const r = require({json.dumps(str(CHAT_RENDERING_JS))});
        const plan = {{steps:[{{id:'v', label:'核对来源'}}], acceptance:['证据充分']}};
        const stopped = {{status:'stopped', research_run_id:'r1', research_summary:{{resume_from_step_id:'比较'}}}};
        console.log(JSON.stringify({{plan:r.renderResearchPlan(plan), recovery:r.renderResearchRecovery(stopped), completed:r.renderResearchRecovery({{status:'completed', research_run_id:'r1'}})}}));
        """
    )
    assert '核对来源' in result['plan'] and '完成判据' in result['plan']
    assert '继续研究' in result['recovery'] and '从步骤：比较' in result['recovery']
    assert result['completed'] == ''


def test_research_recovery_hides_dead_button_for_finished_or_unavailable_runs():
    """完成/不可恢复的研究运行不得再渲染点击后必然失败的「继续研究」按钮。"""
    result = _run_node(
        f"""
        const r = require({json.dumps(str(CHAT_RENDERING_JS))});
        const stopped = {{status:'stopped', research_run_id:'r1', research_summary:{{status:'stopped', resume_from_step_id:'比较'}}}};
        const finished = {{status:'stopped', research_run_id:'r1', research_summary:{{status:'completed'}}}};
        const withCompletedRun = r.renderResearchRecovery({{status:'failed', research_run_id:'r1'}}, {{status:'completed'}});
        const liveStopped = r.renderResearchRecovery({{status:'stopped', research_run_id:'r1'}}, {{status:'running'}});
        console.log(JSON.stringify({{stopped:r.renderResearchRecovery(stopped), finished:r.renderResearchRecovery(finished), withCompletedRun, liveStopped}}));
        """
    )
    assert '继续研究' in result['stopped'] and '从步骤：比较' in result['stopped']
    assert result['finished'] == ''
    assert result['withCompletedRun'] == ''
    # 用户刚停止、持久化状态还没回到前端的运行仍要保留入口，不能把停止后的恢复入口也删掉。
    assert '继续研究' in result['liveStopped']


def test_research_steps_render_public_running_failed_and_blocked_states():
    result = _run_node(
        f"""
        const r = require({json.dumps(str(CHAT_RENDERING_JS))});
        const plan = {{steps:[{{id:'retrieve', label:'检索已授权披露'}}]}};
        const running = r.renderResearchSteps({{plan:plan, step_runs:[{{step_id:'retrieve', status:'running'}}]}});
        const failed = r.renderResearchSteps({{plan:plan, step_runs:[{{step_id:'retrieve', status:'failed'}}]}});
        console.log(JSON.stringify({{running, failed}}));
        """
    )
    assert '检索已授权披露' in result['running']
    assert '进行中' in result['running']
    assert '失败' in result['failed']


def test_workspace_item_shows_persisted_scope_status_and_favorite_actions():
    """工作台行显示持久化范围/状态，不能只依赖会话标题。"""
    result = _run_node(
        f"""
        const r = require({json.dumps(str(CHAT_RENDERING_JS))});
        console.log(JSON.stringify({{html: r.renderWorkspaceItem({{
          session_id: 'session-1', run_id: 'run-1', title: '农业银行现金流核验',
          company_codes: ['601288'], industry: '银行业', periods: ['2026-06-30'],
          intent: 'report_fact', status: 'completed', updated_at: '2026-09-17T10:00:00Z',
          favorite: false
        }})}}));
        """
    )
    html = result['html']
    assert '农业银行' in html
    assert 'completed' in html
    assert '收藏' in html
    assert '601288' in html
    assert 'data-research-action="favorite"' in html


def test_research_workspace_has_no_memory_or_decision_controls():
    """已完成研究不得再渲染保存记忆或决策的入口。"""
    result = _run_node(
        f"""
        const r = require({json.dumps(str(CHAT_RENDERING_JS))});
        const html = r.renderWorkspaceItem({{
          session_id: 'session-1', run_id: 'run-1', title: '现金流核验',
          status: 'completed', updated_at: '2026-09-17T10:00:00Z'
        }});
        console.log(JSON.stringify({{html}}));
        """
    )

    source = CHAT_RENDERING_JS.read_text(encoding="utf-8")
    assert 'data-research-action="save-decision"' not in result["html"]
    assert 'data-research-memory-kind' not in result["html"]
    assert 'data-research-action="save-decision"' not in source
    assert 'data-research-memory-kind' not in source


def test_research_workspace_document_has_no_memory_surface():
    page = (ROOT / "webapp/static/index.html").read_text(encoding="utf-8")
    script = APP_JS.read_text(encoding="utf-8")

    assert "research-memory-panel" not in page
    assert "research-decision-dialog" not in page
    assert "/api/research/memory/" not in script


def test_workspace_item_shows_persisted_evidence_availability_flag():
    """工作台行必须显示由持久化 artifacts 派生的证据可用性，而不是占位文案。"""
    result = _run_node(
        f"""
        const r = require({json.dumps(str(CHAT_RENDERING_JS))});
        const base = {{
          session_id: 'session-1', run_id: 'run-1', title: '现金流核验',
          company_codes: ['601288'], periods: ['2026-06-30'], status: 'completed',
          updated_at: '2026-09-17T10:00:00Z', favorite: false
        }};
        console.log(JSON.stringify({{
          available: r.renderWorkspaceItem({{...base, evidence_available: true}}),
          unavailable: r.renderWorkspaceItem({{...base, evidence_available: false}})
        }}));
        """
    )

    assert '证据：可用' in result['available']
    assert '证据：暂不可用' in result['unavailable']



def test_quality_renderer_shows_safe_health_and_probe_statuses():
    """质量 UI 显示转义刷新时间和本地命令，绝不回显任意失败原文。"""
    result = _run_node(
        f"""
        const r = require({json.dumps(str(CHAT_RENDERING_JS))});
        console.log(JSON.stringify({{
          available: r.renderResearchQuality({{
            available: true,
            generated_at: '<img src=x onerror=alert(1)>',
            health: {{passed: true, case_name: 'scope-leak-case'}},
            probe: {{passed: true, failure_prose: 'scope-leak-case'}}
          }}),
          unavailable: r.renderResearchQuality({{available: false}})
        }}));
        """
    )

    assert "健康评测" in result["available"]
    assert "负向探针" in result["available"]
    assert "生成时间：&lt;img src=x onerror=alert(1)&gt;" in result["available"]
    assert "<img src=x onerror=alert(1)>" not in result["available"]
    assert "scope-leak-case" not in result["available"]
    command = (
        "python3 scripts/run_chat_evaluation.py"
        " --fixture tests/fixtures/chat_eval_cases.json"
        " --output data/research_quality_summary.json"
    )
    assert "<code>" + command + "</code>" in result["available"]
    assert "<code>" + command + "</code>" in result["unavailable"]
    assert "尚无本地质量摘要" in result["unavailable"]


def test_refresh_workspace_requires_visible_chat_page_and_workspace_panel():
    """离开聊天页后的异步写入不能刷新隐藏的工作台。"""
    result = _run_node(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const src = fs.readFileSync({json.dumps(str(APP_JS))}, 'utf8');
        const start = src.indexOf('function researchWorkspacePanelIsVisible() {{');
        const end = src.indexOf('async function loadResearchQuality()', start);
        if (start < 0 || end < 0) throw new Error('refreshResearchWorkspaceIfOpen 未找到');
        const fnSource = src.slice(start, end);

        async function refreshCount(workspaceHidden, chatHidden) {{
          let calls = 0;
          const workspace = {{classList: {{contains: () => workspaceHidden}}}};
          const chatPage = {{classList: {{contains: () => chatHidden}}}};
          const sandbox = {{
            $: (selector) => selector === '#research-workspace' ? workspace :
              (selector === '#page-chat' ? chatPage : null),
            researchWorkspaceFilters: () => ({{status: 'completed'}}),
            loadResearchWorkspace: () => {{ calls += 1; return Promise.resolve(); }},
            Promise,
          }};
          vm.createContext(sandbox);
          vm.runInContext(fnSource, sandbox);
          await sandbox.refreshResearchWorkspaceIfOpen();
          return calls;
        }}

        (async () => console.log(JSON.stringify({{
          visible: await refreshCount(false, false),
          workspaceHidden: await refreshCount(true, false),
          chatHidden: await refreshCount(false, true),
        }})))();
        """
    )

    assert result == {"visible": 1, "workspaceHidden": 0, "chatHidden": 0}



def test_delete_result_and_export_state_are_textual():
    """会话删除和导出状态均以文本表达，不能只用颜色传达。"""
    result = _run_node(
        f"""
        const r = require({json.dumps(str(CHAT_RENDERING_JS))});
        console.log(JSON.stringify({{
          deleted: r.renderDeleteResult(),
          export: r.renderExportState('partial')
        }}));
        """
    )
    assert '会话已删除' in result['deleted']
    assert '研究记忆' not in result['deleted']
    assert '部分完成' in result['export']


def test_run_reuse_actions_reuse_existing_run_identity_without_new_privileges():
    """编辑重问/分支追问沿用既有 run/session 身份，不新增特权动作。"""
    result = _run_node(
        f"""
        const r = require({json.dumps(str(CHAT_RENDERING_JS))});
        console.log(JSON.stringify({{
          run: r.renderRunReuseActions({{id: 'run-1', status: 'completed'}}),
          legacy: r.renderRunReuseActions({{legacy_evidence_unavailable: true}}),
          missing: r.renderRunReuseActions(null)
        }}));
        """
    )

    assert 'data-chat-action="edit-reask"' in result['run']
    assert 'data-chat-action="branch-followup"' in result['run']
    assert '编辑重问' in result['run'] and '分支追问' in result['run']
    assert result['legacy'] == ''
    assert result['missing'] == ''
