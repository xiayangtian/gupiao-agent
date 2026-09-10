"""前端财报分析任务联动的回归测试。"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_JS = ROOT / "webapp" / "static" / "analysis_workflow.js"
APP_JS = ROOT / "webapp" / "static" / "app.js"
STYLE_CSS = ROOT / "webapp" / "static" / "style.css"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="前端工作流回归测试需要 Node.js")


def _css_rule(css: str, selector: str) -> str:
    """返回单条 CSS 规则体，缺少规则时直接失败。"""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"缺少样式规则：{selector}"
    return match.group(1)


def _run_node(source: str) -> dict:
    completed = subprocess.run(
        [NODE, "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_conclusion_emphasis_uses_readable_red_instead_of_browser_yellow():
    """关键信息强调必须是可读的深红色，紧凑段落与分层次段落保持一致。"""
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert "--key-emphasis: #b91c1c;" in css
    assert "--key-emphasis-light: #fef2f2;" in css
    for selector in (".analysis-report-finding mark", ".analysis-compact-finding mark"):
        rule = _css_rule(css, selector)
        assert "var(--key-emphasis)" in rule
        assert "var(--key-emphasis-light)" in rule
        assert "var(--accent-light)" not in rule
        assert "yellow" not in rule


def test_unverified_conclusion_card_uses_a_neutral_label():
    """待核验不是告警，不应使用橙黄色危险标识。"""
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert "var(--warning)" not in _css_rule(css, ".analysis-tone-pending")
    assert "var(--warning" not in _css_rule(css, ".analysis-tone-pending .analysis-finding-label")


def test_topic_tab_row_wraps_instead_of_clipping():
    """主题 Tab 行必须换行展示，不能靠横向滚动截断最后一个主题。"""
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert "flex-wrap: wrap" in _css_rule(css, ".analysis-result-tabs")


def test_v4_history_report_uses_progressive_renderer_and_mounts_visualizations():
    """v4 结果必须沿用渐进式正文，并在重渲染时管理可视化图表实例。"""
    source = APP_JS.read_text(encoding="utf-8")

    assert "Number(content.schema_version) >= 3" in source
    assert "AnalysisVisualizations.mount" in source
    assert "AnalysisVisualizations.destroy" in source


def test_visualization_lifecycle_clears_before_all_replaced_analysis_containers():
    """v4 图表切到旧/空内容时，容器替换前必须销毁并清空实例映射。"""
    source = APP_JS.read_text(encoding="utf-8")

    assert "function clearAnalysisVisualizations()" in source
    assert "clearAnalysisVisualizations();\n  var st = STATE.analysisCache[key];" in source
    assert "clearAnalysisVisualizations();\n  detail.classList.remove('hint');" in source
    assert "function showHistoryNoAnalysis(item)" in source
    assert "clearAnalysisVisualizations();\n  detail.innerHTML" in source
    assert "clearAnalysisVisualizations();\n        detail.innerHTML = '<p class=\"hint\">无法读取分析文件</p>'" in source
    assert "clearAnalysisVisualizations();\n      detail2.innerHTML = '<p class=\"hint\">读取分析文件失败</p>'" in source
    assert "clearAnalysisVisualizations();\n}" in source[source.index("function destroyAllCharts()"):]


def test_stop_analysis_clears_visualizations_before_replacing_result_container():
    """用户停止分析时也必须销毁 Chart 实例，不能留下复用容器中的旧图。"""
    source = APP_JS.read_text(encoding="utf-8")
    stop = source[source.index("async function stopAnalysis"):source.index("async function startAnalysis")]
    clear = source[source.index("function clearAnalysisVisualizations()"):source.index("function mountAnalysisVisualizations")]
    visualizations = (ROOT / "webapp" / "static" / "analysis_visualizations.js").read_text(encoding="utf-8")

    assert stop.index("clearAnalysisVisualizations();") < stop.index(
        "ar.innerHTML = '<div class=\"hint\">正在停止分析"
    )
    assert "AnalysisVisualizations.destroy(charts)" in clear
    assert "STATE.charts.visualizations = null" in clear
    assert "charts.clear();" in visualizations


def test_visualization_chartjs_fallback_keeps_table_and_removes_canvas():
    """缺失 Chart.js 时不能留下空画布，必须保留数据表并告知用户。"""
    source = (ROOT / "webapp" / "static" / "analysis_visualizations.js").read_text(encoding="utf-8")

    assert "function chartUnavailable(slot, canvas)" in source
    assert "图表组件不可用，已展示数据表" in source
    assert "chartBox.innerHTML = message" in source
    assert "if (!ChartClass)" in source


def test_topic_tab_binding_always_uses_the_current_report_key():
    """同一容器复用时必须刷新 key，否则切换报告后主题 Tab 会串到上一份报告。"""
    source = APP_JS.read_text(encoding="utf-8")

    assert "container.dataset.progressiveKey = key" in source
    assert "var currentKey = container.dataset.progressiveKey" in source
    assert source.index("container.dataset.progressiveKey = key") < source.index(
        "container.dataset.progressiveTabsBound"
    )


def test_analysis_report_styles_define_semantic_tones_and_visible_focus():
    """报告式分析页应定义语义状态、页码按钮焦点与激活 Tab。"""
    css = STYLE_CSS.read_text(encoding="utf-8")

    for selector in (
        ".analysis-report-body", ".analysis-report-finding",
        ".analysis-tone-risk", ".analysis-tone-highlight",
        ".analysis-tone-observation", ".analysis-tone-pending", ".analysis-evidence-section",
        ".analysis-evidence-page:focus-visible", ".analysis-result-tab.active",
    ):
        assert selector in css
    assert ".analysis-finding {" not in css


def test_progressive_render_call_sites_assign_distinct_evidence_anchors():
    """主分析页与历史详情同时存在时必须使用不同的证据锚点。"""
    source = APP_JS.read_text(encoding="utf-8")

    assert source.count("evidenceAnchor: 'analysis-evidence-main'") == 2
    assert source.count("evidenceAnchor: 'analysis-evidence-history'") == 2


def test_evidence_citation_opens_its_compact_source_before_scrolling():
    """点击正文证据时，应先展开折叠来源再定位到对应条目。"""
    source = APP_JS.read_text(encoding="utf-8")

    assert "closest('.analysis-evidence-link')" in source
    assert "details.open = true" in source
    assert "target.scrollIntoView" in source
    assert "target.focus({ preventScroll: true })" in source
    assert "openHistoryEvidencePdfPage" in source
    assert "setHistoryView('pdf')" in source
    assert 'tabindex="-1"' in WORKFLOW_JS.read_text(encoding="utf-8")


def test_history_analysis_delete_control_is_guarded_and_refreshes_local_state():
    """删除入口必须二次确认，成功后清理缓存并刷新历史列表。"""
    source = APP_JS.read_text(encoding="utf-8")
    index = (ROOT / "webapp" / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="history-delete-analysis-btn"' in index
    assert "window.confirm('确定删除此分析报告吗？原始 PDF 将保留。')" in source
    assert "fetch('/api/history/' + encodeURIComponent(item.analysis_filename), { method: 'DELETE' })" in source
    assert "delete STATE.analysisCache[analysisKey(item.code, item.period)]" in source


def test_analysis_report_mobile_evidence_items_use_one_column():
    """窄屏证据项必须单列，所有子项均回到首列，避免证据内容被挤压。"""
    css = STYLE_CSS.read_text(encoding="utf-8")
    mobile = css.split("@media (max-width: 560px) {", 1)[1].split(
        "@media (prefers-reduced-motion: reduce)", 1
    )[0]

    assert ".analysis-evidence-item { grid-template-columns: minmax(0, 1fr); }" in mobile
    assert ".analysis-evidence-page,\n  .analysis-evidence-source { grid-column: 1; justify-self: start; }" in mobile
    assert ".analysis-evidence-excerpt { grid-column: 1; }" in mobile


def test_analysis_task_registry_survives_reload_and_deduplicates_by_report():
    """刷新后仍应找到同一报告唯一的活跃任务，而不是丢失或重复轮询。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const values = {{}};
        const storage = {{
          getItem: key => Object.prototype.hasOwnProperty.call(values, key) ? values[key] : null,
          setItem: (key, value) => {{ values[key] = value; }},
          removeItem: key => {{ delete values[key]; }}
        }};
        const first = workflow.createAnalysisTaskRegistry(storage);
        first.track({{
          taskId: 'task-old', code: '600900', period: '2025-12-31',
          analysisId: 'a-old', stage: 'fast_ready', lastEventId: 2,
          updatedAt: '2026-09-02T10:00:00Z'
        }});
        first.track({{
          taskId: 'task-new', code: '600900', period: '2025-12-31',
          analysisId: 'a-new', stage: 'deep_processing', lastEventId: 5,
          updatedAt: '2026-09-02T10:01:00Z'
        }});
        const reloaded = workflow.createAnalysisTaskRegistry(storage);
        console.log(JSON.stringify({{
          active: reloaded.active(),
          found: reloaded.get('600900', '2025-12-31')
        }}));
        """
    )

    assert result["active"] == [
        {
            "taskId": "task-new",
            "code": "600900",
            "period": "2025-12-31",
            "analysisId": "a-new",
            "stage": "deep_processing",
            "lastEventId": 5,
            "updatedAt": "2026-09-02T10:01:00Z",
        }
    ]
    assert result["found"]["taskId"] == "task-new"


def test_analysis_event_reducer_deduplicates_and_preserves_active_tab():
    """重复游标必须原样返回；新主题只能提示更新，不能抢占用户当前 Tab。"""
    result = _run_node(
        f"""
        const assert = require('node:assert/strict');
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const initial = {{ lastEventId: 4, activeTab: 'quick', sections: [] }};
        const duplicate = workflow.applyAnalysisEvent(initial, {{
          id: 4, type: 'section.ready', payload: {{ section: {{ section_id: 'cash' }} }}
        }});
        const updated = workflow.applyAnalysisEvent(initial, {{
          id: 5, type: 'section.ready',
          payload: {{ section: {{ section_id: 'cash', title: '现金流' }} }}
        }});
        console.log(JSON.stringify({{
          duplicateSameObject: duplicate === initial,
          activeTab: updated.activeTab,
          sections: updated.sections,
          lastEventId: updated.lastEventId,
          hasNewFindings: updated.hasNewFindings
        }}));
        """
    )

    assert result == {
        "duplicateSameObject": True,
        "activeTab": "quick",
        "sections": [{"section_id": "cash", "title": "现金流"}],
        "lastEventId": 5,
        "hasNewFindings": True,
    }


def test_analysis_event_reducer_merges_quick_corrections_and_terminal_snapshot():
    """修正应追加而非覆盖快速结论，终态快照要保留当前 Tab。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        let state = {{ lastEventId: 0, activeTab: 'cash', quick: null, sections: [] }};
        state = workflow.applyAnalysisEvent(state, {{
          id: 1, type: 'quick.ready', payload: {{ quick: {{ conclusions: [{{ id: 'q1' }}] }} }}
        }});
        state = workflow.applyAnalysisEvent(state, {{
          id: 2, type: 'quick.corrected',
          payload: {{ correction: {{ conclusion_id: 'q1', before: '100', after: '101' }} }}
        }});
        state = workflow.applyAnalysisEvent(state, {{
          id: 3, type: 'job.completed',
          payload: {{ analysis: {{ stage: 'completed', sections: [{{ section_id: 'cash' }}] }} }}
        }});
        console.log(JSON.stringify(state));
        """
    )

    assert result["stage"] == "completed"
    assert result["activeTab"] == "cash"
    assert result["quick"]["conclusions"] == [{"id": "q1"}]
    assert result["quick"]["corrections"] == [
        {"conclusion_id": "q1", "before": "100", "after": "101"}
    ]
    assert result["sections"] == [{"section_id": "cash"}]
    assert result["lastEventId"] == 3


def test_merge_snapshot_restores_refresh_state_without_resetting_active_tab():
    """刷新轮询到完整快照后，应恢复进度和内容但保持用户当前主题。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const merged = workflow.mergeSnapshot(
          {{ activeTab: 'cash', lastEventId: 8, sections: [{{ section_id: 'cash' }}] }},
          {{ status: 'running', result: {{ stage: 'deep_processing', quick: {{ conclusions: [] }},
             sections: [{{ section_id: 'cash' }}, {{ section_id: 'risk' }}] }} }}
        );
        console.log(JSON.stringify(merged));
        """
    )

    assert result["activeTab"] == "cash"
    assert result["lastEventId"] == 8
    assert result["stage"] == "deep_processing"
    assert [item["section_id"] for item in result["sections"]] == ["cash", "risk"]


def test_registry_storage_event_merges_remote_cursor_without_duplicate_task():
    """另一标签页推进游标后，本页注册表应原位更新同一报告任务。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const values = {{}};
        const storage = {{
          getItem: key => values[key] || null,
          setItem: (key, value) => {{ values[key] = value; }},
          removeItem: key => {{ delete values[key]; }}
        }};
        const registry = workflow.createAnalysisTaskRegistry(storage, 'tasks');
        registry.track({{
          taskId: 't1', code: '600900', period: '2025-12-31', analysisId: 'a1',
          stage: 'fast_ready', lastEventId: 2, updatedAt: '2026-09-02T10:00:00Z'
        }});
        registry.applyStorageEvent({{
          key: 'tasks',
          newValue: JSON.stringify({{
            '600900:2025-12-31': {{
              taskId: 't1', code: '600900', period: '2025-12-31', analysisId: 'a1',
              stage: 'deep_processing', lastEventId: 7, updatedAt: '2026-09-02T10:01:00Z'
            }}
          }})
        }});
        console.log(JSON.stringify({{ active: registry.active(), found: registry.get('600900', '2025-12-31') }}));
        """
    )

    assert len(result["active"]) == 1
    assert result["found"]["taskId"] == "t1"
    assert result["found"]["stage"] == "deep_processing"
    assert result["found"]["lastEventId"] == 7


def test_analysis_task_registry_removes_terminal_task_from_persistent_storage():
    """任务进入终态后应从恢复队列清理，避免刷新后再次启动轮询。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const values = {{}};
        const storage = {{
          getItem: key => values[key] || null,
          setItem: (key, value) => {{ values[key] = value; }},
          removeItem: key => {{ delete values[key]; }}
        }};
        const registry = workflow.createAnalysisTaskRegistry(storage);
        registry.track({{ taskId: 'task-1', code: '600900', period: '2025-12-31' }});
        registry.remove('600900', '2025-12-31');
        const reloaded = workflow.createAnalysisTaskRegistry(storage);
        console.log(JSON.stringify({{ active: reloaded.active(), values }}));
        """
    )

    assert result["active"] == []
    assert result["values"] == {}


def test_report_chat_navigation_focuses_report_and_opens_a_new_chat():
    """财报页提问入口应跳到聚焦报告的新会话，不能留在当前页面内问答。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const state = {{}};
        const calls = [];
        workflow.goToReportChat(
          state,
          {{ code: '600900', period: '2025-12-31', company: '长江电力' }},
          () => calls.push('new-session'),
          hash => calls.push(hash)
        );
        console.log(JSON.stringify({{ focus: state.chatFocusReport, calls }}));
        """
    )

    assert result == {
        "focus": {
            "code": "600900",
            "period": "2025-12-31",
            "company": "长江电力",
        },
        "calls": ["new-session", "#/chat"],
    }


def test_local_analysis_navigation_opens_matching_history_report_after_load():
    """“查看本地分析”应跳到历史页，待数据加载后展开并选中对应报告。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const state = {{ historyCollapsed: {{ '600900': true }} }};
        const calls = [];
        const go = workflow.goToHistoryReport || (() => {{}});
        const open = workflow.openPendingHistoryReport || (async () => false);
        go(
          state,
          {{ code: '600900', period: '2025-12-31' }},
          hash => calls.push(['navigate', hash])
        );
        open(state, async (code, period) => calls.push(['select', code, period]))
          .then(opened => console.log(JSON.stringify({{
            opened,
            calls,
            collapsed: state.historyCollapsed['600900'],
            pending: state.pendingHistoryReport || null
          }})));
        """
    )

    assert result == {
        "opened": True,
        "calls": [
            ["navigate", "#/history"],
            ["select", "600900", "2025-12-31"],
        ],
        "collapsed": False,
        "pending": None,
    }


def test_downloaded_pdf_only_previews_when_report_is_still_selected():
    """下载完成应自动展示当前报告，但不能覆盖用户后来切换的报告。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        console.log(JSON.stringify({{
          current: workflow.downloadedPdfPreviewUrl(
            {{ code: '600900', period: '2025-12-31' }}, '600900', '2025-12-31', 12345
          ),
          stale: workflow.downloadedPdfPreviewUrl(
            {{ code: '600900', period: '2024-12-31' }}, '600900', '2025-12-31'
          )
        }}));
        """
    )

    assert result == {
        "current": "/api/reports/600900/2025-12-31.pdf?v=12345",
        "stale": None,
    }


def test_download_completion_only_mutates_the_report_that_is_still_visible():
    """旧下载完成不能把同期间的另一家公司标为已下载，也不能关闭其 loading。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        console.log(JSON.stringify({{
          same: workflow.downloadCompletionEffect(
            {{ code: '600900', period: '2025-12-31' }}, '600900', '2025-12-31'
          ),
          otherCompany: workflow.downloadCompletionEffect(
            {{ code: '000001', period: '2025-12-31' }}, '600900', '2025-12-31'
          ),
          otherPeriod: workflow.downloadCompletionEffect(
            {{ code: '600900', period: '2024-12-31' }}, '600900', '2025-12-31'
          )
        }}));
        """
    )

    assert result == {
        "same": {"sameCompany": True, "sameReport": True},
        "otherCompany": {"sameCompany": False, "sameReport": False},
        "otherPeriod": {"sameCompany": True, "sameReport": False},
    }


def test_evidence_pdf_preview_url_only_accepts_current_report_and_positive_page():
    """证据页码预览只定位当前已选报告，且拒绝非正整数页码。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        console.log(JSON.stringify({{
          valid: workflow.evidencePdfPreviewUrl({{code:'601288', period:'2025-12-31'}}, 12),
          decimal: workflow.evidencePdfPreviewUrl({{code:'601288', period:'2025-12-31'}}, 1.5),
          zero: workflow.evidencePdfPreviewUrl({{code:'601288', period:'2025-12-31'}}, 0),
          noReport: workflow.evidencePdfPreviewUrl(null, 12)
        }}));
        """
    )

    assert result == {
        "valid": "/api/reports/601288/2025-12-31.pdf#page=12",
        "decimal": None,
        "zero": None,
        "noReport": None,
    }


def test_pdf_evidence_url_uses_a_cache_buster_before_the_page_fragment():
    """同一 PDF 再次定位时必须强制 iframe 重新加载，不能只改 fragment。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        console.log(JSON.stringify(workflow.evidencePdfPreviewUrl(
          {{ code: '601288', period: '2026-03-31' }}, 12, 12345
        )));
        """
    )

    assert result == "/api/reports/601288/2026-03-31.pdf?jump=12345#page=12"


def test_history_detail_keeps_analysis_content_left_aligned_and_delete_button_compact():
    """历史结果不能继承空状态的居中样式，删除按钮尺寸应与重新分析一致。"""
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert "#history-detail:not(.hint) { text-align: left; }" in css
    assert "detail.classList.remove('hint')" in APP_JS.read_text(encoding="utf-8")
    assert "#history-delete-analysis-btn" in css
    assert "padding: 4px 12px" in css
    assert "font-size: 12px" in css


def test_pdf_evidence_event_is_scoped_to_the_primary_analysis_result():
    """历史详情复用 Tab 绑定时，不能触发主分析页 PDF iframe 的跳页事件。"""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function bindProgressiveTabs")
    end = source.index("function openEvidencePdfPage", start)
    handler = source[start:end]

    assert "container.id === 'analyze-result'" in handler


def test_pdf_download_fallback_requires_missing_endpoint_and_same_report():
    """兼容回退仅允许 404/405，且不能让旧请求覆盖用户新选中的报告。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const selected = {{ code: '600900', period: '2025-12-31' }};
        console.log(JSON.stringify({{
          missing: workflow.pdfDownloadFallbackUrl(selected, '600900', '2025-12-31', 404, 12345),
          methodMissing: workflow.pdfDownloadFallbackUrl(selected, '600900', '2025-12-31', 405, 12345),
          serverError: workflow.pdfDownloadFallbackUrl(selected, '600900', '2025-12-31', 500),
          stale: workflow.pdfDownloadFallbackUrl(
            {{ code: '000001', period: '2025-12-31' }}, '600900', '2025-12-31', 404
          )
        }}));
        """
    )

    assert result == {
        "missing": "/api/reports/600900/2025-12-31.pdf?v=12345",
        "methodMissing": "/api/reports/600900/2025-12-31.pdf?v=12345",
        "serverError": None,
        "stale": None,
    }


def test_registry_discards_malformed_saved_tasks_and_repairs_storage():
    """损坏或旧版本的 localStorage 条目不能阻断页面初始化与任务恢复。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const key = 'tasks';
        const values = {{}};
        values[key] = JSON.stringify({{
          good: {{ taskId: 'task-1', code: '600900', period: '2025-12-31' }},
          nullEntry: null,
          arrayEntry: [],
          missingId: {{ code: '000001', period: '2025-12-31' }},
          oldSchema: {{ id: 'old-task', stock: '300750' }}
        }});
        const storage = {{
          getItem: name => values[name] || null,
          setItem: (name, value) => {{ values[name] = value; }},
          removeItem: name => {{ delete values[name]; }}
        }};
        const registry = workflow.createAnalysisTaskRegistry(storage, key);
        console.log(JSON.stringify({{
          active: registry.active(),
          persisted: JSON.parse(values[key])
        }}));
        """
    )

    assert result["active"] == [
        {
            "taskId": "task-1", "code": "600900", "period": "2025-12-31",
            "analysisId": "", "stage": "pending", "lastEventId": 0,
            "updatedAt": "",
        }
    ]
    assert result["persisted"] == {
        "600900:2025-12-31": {
            "taskId": "task-1",
            "code": "600900",
            "period": "2025-12-31",
            "analysisId": "",
            "stage": "pending",
            "lastEventId": 0,
            "updatedAt": "",
        }
    }


def test_terminal_analysis_reconciles_report_and_history_metadata():
    """任务终态应同步财报卡片、历史选中项与详情徽标。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const done = workflow.reconcileAnalysisTerminal({{
          code: '600900', period: '2025-12-31', status: 'done',
          reports: [{{ period: '2025-12-31', analyzed: false }}],
          historyItems: [{{ code: '600900', period: '2025-12-31', has_analysis: true,
                           analysis_filename: 'result.json' }}],
          historySelected: {{ code: '600900', period: '2025-12-31', has_analysis: false }}
        }});
        console.log(JSON.stringify({{
          done,
          failedBadge: workflow.analysisTerminalBadge('failed', false),
          cancelledBadge: workflow.analysisTerminalBadge('cancelled', false)
        }}));
        """
    )

    assert result["done"]["reports"][0]["analyzed"] is True
    assert result["done"]["historySelected"]["has_analysis"] is True
    assert result["done"]["historySelected"]["analysis_filename"] == "result.json"
    assert result["done"]["badge"] == {"text": "已分析", "className": "badge badge-purple"}
    assert result["failedBadge"] == {"text": "分析失败", "className": "badge badge-danger"}
    assert result["cancelledBadge"] == {"text": "已停止", "className": "badge badge-warn"}


def test_analysis_progress_builds_completed_current_and_pending_steps():
    """轮询到中间进度时应形成可解释的步骤时间线，而不是只显示笼统等待。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const model = workflow.analysisProgressModel({{
          progress: 0.525,
          dims: ['financial_summary', 'risk_warning']
        }}, {{
          financial_summary: '财务摘要',
          risk_warning: '风险识别'
        }});
        console.log(JSON.stringify(model));
        """
    )

    assert result["percent"] == 53
    assert result["current"] == "正在分析风险识别（2/2）"
    assert result["steps"] == [
        {"label": "准备财报文件", "state": "done"},
        {"label": "构建知识上下文", "state": "done"},
        {"label": "分析财务摘要", "state": "done"},
        {"label": "分析风险识别", "state": "current"},
        {"label": "提取指标并校验", "state": "pending"},
        {"label": "保存分析结果", "state": "pending"},
    ]


def test_history_dimension_picker_uses_defaults_first_and_previous_dimensions_again():
    """首次分析采用默认维度，重新分析优先恢复该报告上次使用的有效维度。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const choose = workflow.historyDimensionDefaults || (() => []);
        const available = [
          {{ id: 'financial_summary', default: true }},
          {{ id: 'risk_warning', default: true }},
          {{ id: 'cashflow', default: false }}
        ];
        console.log(JSON.stringify({{
          first: choose(available, []),
          again: choose(available, ['cashflow', 'removed_dimension'])
        }}));
        """
    )

    assert result == {
        "first": ["financial_summary", "risk_warning"],
        "again": ["cashflow"],
    }


def test_analysis_error_message_handles_json_and_html_error_responses():
    """分析提交失败时，HTML 错误页不能再触发 JSON 解析异常。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        console.log(JSON.stringify({{
          json: workflow.analysisErrorMessage(
            503, 'application/json', '{{"detail":"财报数据源暂时不可用，请稍后重试"}}'
          ),
          html: workflow.analysisErrorMessage(
            500, 'text/html', '<html>Internal Server Error</html>'
          )
        }}));
        """
    )

    assert result == {
        "json": "财报数据源暂时不可用，请稍后重试",
        "html": "HTTP 500：服务暂时不可用，请稍后重试",
    }


def test_history_async_result_only_applies_to_the_report_still_selected():
    """快速从 A 切到 B 后，A 的迟到详情或维度响应必须被丢弃。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const current = {{ historySelected: {{ code: '000001', period: '2025-12-31' }} }};
        const check = workflow.historySelectionIsCurrent || (() => true);
        console.log(JSON.stringify({{
          current: check(current, {{ code: '000001', period: '2025-12-31' }}),
          staleCompany: check(current, {{ code: '600900', period: '2025-12-31' }}),
          stalePeriod: check(current, {{ code: '000001', period: '2024-12-31' }})
        }}));
        """
    )

    assert result == {
        "current": True,
        "staleCompany": False,
        "stalePeriod": False,
    }


def test_analysis_event_reducer_merges_visualizations_ready_payload():
    """结构图完成事件必须在终态前刷新当前渐进式报告。"""
    result = _run_node(
        f"""
        const workflow = require({json.dumps(str(WORKFLOW_JS))});
        const next = workflow.applyAnalysisEvent({{ lastEventId: 8, activeTab: 'cash' }}, {{
          id: 9, type: 'visualizations.ready',
          payload: {{ visualizations: {{ version: 1, cards: [{{ id: 'cash_flow_structure' }}] }} }}
        }});
        console.log(JSON.stringify(next));
        """
    )

    assert result["lastEventId"] == 9
    assert result["activeTab"] == "cash"
    assert result["visualizations"] == {
        "version": 1, "cards": [{"id": "cash_flow_structure"}],
    }


def test_v2_history_dimension_panels_offer_the_same_reanalysis_hint():
    """旧 v2 维度正文也应提示重新分析，而不创建图表或自动重分析。"""
    source = APP_JS.read_text(encoding="utf-8")

    render_dimension_tabs = source[source.index("function renderDimensionTabs"):source.index("function bindDimTabs")]
    assert 'analysis-visualization-legacy-hint' in render_dimension_tabs
    assert '重新分析后可生成结构图' in render_dimension_tabs
