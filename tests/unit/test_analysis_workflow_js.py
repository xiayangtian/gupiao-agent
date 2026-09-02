"""前端财报分析任务联动的回归测试。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_JS = ROOT / "webapp" / "static" / "analysis_workflow.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="前端工作流回归测试需要 Node.js")


def _run_node(source: str) -> dict:
    completed = subprocess.run(
        [NODE, "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


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
