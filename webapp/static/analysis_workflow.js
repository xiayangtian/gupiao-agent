(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AnalysisWorkflow = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var DEFAULT_STORAGE_KEY = 'gp-agent.active-analysis-tasks.v1';

  function reportKey(code, period) {
    return String(code || '') + ':' + String(period || '');
  }

  function validTask(task) {
    return task && task.taskId && task.code && task.period;
  }

  function createAnalysisTaskRegistry(storage, storageKey) {
    var key = storageKey || DEFAULT_STORAGE_KEY;
    var tasks = {};
    var needsRepair = false;

    try {
      var saved = JSON.parse(storage.getItem(key) || '{}');
      if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
        Object.keys(saved).forEach(function (savedKey) {
          var task = saved[savedKey];
          if (!validTask(task) || Array.isArray(task)) {
            needsRepair = true;
            return;
          }
          var normalized = {
            taskId: String(task.taskId),
            code: String(task.code),
            period: String(task.period),
          };
          if (task.company) normalized.company = String(task.company);
          if (Array.isArray(task.dims)) normalized.dims = task.dims.slice();
          var normalizedKey = reportKey(normalized.code, normalized.period);
          if (savedKey !== normalizedKey) needsRepair = true;
          tasks[normalizedKey] = normalized;
        });
      } else {
        needsRepair = true;
      }
    } catch (_) {
      tasks = {};
      needsRepair = true;
    }

    function persist() {
      try {
        var keys = Object.keys(tasks);
        if (!keys.length) {
          storage.removeItem(key);
          return;
        }
        storage.setItem(key, JSON.stringify(tasks));
      } catch (_) {
        // 浏览器禁用本地存储时仍保留当前页面内的共享状态。
      }
    }

    if (needsRepair) persist();

    return {
      get: function (code, period) {
        return tasks[reportKey(code, period)] || null;
      },
      track: function (task) {
        if (!validTask(task)) throw new Error('分析任务缺少 taskId、code 或 period');
        var normalized = {
          taskId: String(task.taskId),
          code: String(task.code),
          period: String(task.period),
        };
        if (task.company) normalized.company = String(task.company);
        if (Array.isArray(task.dims)) normalized.dims = task.dims.slice();
        tasks[reportKey(normalized.code, normalized.period)] = normalized;
        persist();
        return normalized;
      },
      remove: function (code, period) {
        delete tasks[reportKey(code, period)];
        persist();
      },
      active: function () {
        return Object.keys(tasks).map(function (taskKey) { return tasks[taskKey]; });
      },
    };
  }

  function goToReportChat(state, report, startNewSession, navigate) {
    state.chatFocusReport = {
      code: report.code,
      period: report.period,
      company: report.company || '',
    };
    startNewSession();
    navigate('#/chat');
  }

  function goToHistoryReport(state, report, navigate) {
    state.pendingHistoryReport = {
      code: String(report.code),
      period: String(report.period),
    };
    navigate('#/history');
  }

  async function openPendingHistoryReport(state, selectHistoryItem) {
    var target = state.pendingHistoryReport;
    if (!target || !target.code || !target.period) return false;
    state.pendingHistoryReport = null;
    state.historyCollapsed = state.historyCollapsed || {};
    state.historyCollapsed[target.code] = false;
    await selectHistoryItem(target.code, target.period);
    return true;
  }

  function downloadedPdfPreviewUrl(selected, code, period) {
    if (!downloadCompletionEffect(selected, code, period).sameReport) return null;
    return '/api/reports/' + encodeURIComponent(code) + '/'
      + encodeURIComponent(period) + '.pdf';
  }

  function downloadCompletionEffect(selected, code, period) {
    var sameCompany = !!selected && String(selected.code) === String(code);
    return {
      sameCompany: sameCompany,
      sameReport: sameCompany && String(selected.period) === String(period),
    };
  }

  function pdfDownloadFallbackUrl(selected, code, period, status) {
    if (Number(status) !== 404 && Number(status) !== 405) return null;
    return downloadedPdfPreviewUrl(selected, code, period);
  }

  function analysisTerminalBadge(status, hasAnalysis) {
    if (status === 'running' || status === 'pending') {
      return { text: '分析中', className: 'badge badge-warn' };
    }
    if (status === 'failed') {
      return { text: '分析失败', className: 'badge badge-danger' };
    }
    if (status === 'cancelled') {
      return { text: '已停止', className: 'badge badge-warn' };
    }
    if (status === 'done' || hasAnalysis) {
      return { text: '已分析', className: 'badge badge-purple' };
    }
    return { text: '未分析', className: 'badge badge-warn' };
  }

  function historyDimensionDefaults(available, previous) {
    var availableIds = (available || []).map(function (item) { return String(item.id); });
    var prior = (previous || []).map(String).filter(function (id) {
      return availableIds.indexOf(id) >= 0;
    });
    if (prior.length) return prior;
    return (available || []).filter(function (item) { return !!item.default; })
      .map(function (item) { return String(item.id); });
  }

  function historySelectionIsCurrent(state, target) {
    var selected = state && state.historySelected;
    return !!selected && !!target
      && String(selected.code) === String(target.code)
      && String(selected.period) === String(target.period);
  }

  function reconcileAnalysisTerminal(input) {
    var code = String(input.code);
    var period = String(input.period);
    var reports = (input.reports || []).map(function (report) {
      if (input.status !== 'done' || String(report.period) !== period) return report;
      return Object.assign({}, report, { analyzed: true });
    });
    var selected = input.historySelected || null;
    var latest = null;
    (input.historyItems || []).some(function (item) {
      if (String(item.code) === code && String(item.period) === period) {
        latest = item;
        return true;
      }
      return false;
    });
    if (selected && String(selected.code) === code && String(selected.period) === period) {
      selected = latest || (input.status === 'done'
        ? Object.assign({}, selected, { has_analysis: true }) : selected);
    }
    return {
      reports: reports,
      historySelected: selected,
      badge: analysisTerminalBadge(input.status, !!(selected && selected.has_analysis)),
    };
  }

  function analysisProgressModel(task, dimensionNames) {
    var progress = Number(task && task.progress);
    if (!Number.isFinite(progress)) progress = 0;
    progress = Math.max(0, Math.min(1, progress));
    var dims = task && Array.isArray(task.dims) ? task.dims : [];
    var names = dimensionNames || {};
    var steps = [];
    var current = progress === 0 ? '等待任务开始' : '正在准备财报文件';

    steps.push({
      label: '准备财报文件',
      state: progress >= 0.08 ? 'done' : 'current',
    });
    steps.push({
      label: '构建知识上下文',
      state: progress >= 0.18 ? 'done' : (progress >= 0.08 ? 'current' : 'pending'),
    });
    if (progress >= 0.08 && progress < 0.18) current = '正在构建知识上下文';

    var completedDims = 0;
    if (progress >= 0.8) {
      completedDims = dims.length;
    } else if (progress >= 0.25 && dims.length) {
      completedDims = Math.floor(((progress - 0.25) / 0.55) * dims.length + 1e-9);
      completedDims = Math.max(0, Math.min(dims.length - 1, completedDims));
    }
    dims.forEach(function (dim, index) {
      var state = 'pending';
      if (progress >= 0.8 || index < completedDims) state = 'done';
      else if (progress >= 0.18 && index === completedDims) state = 'current';
      var name = names[dim] || dim;
      steps.push({ label: '分析' + name, state: state });
      if (state === 'current') {
        current = '正在分析' + name + '（' + (index + 1) + '/' + dims.length + '）';
      }
    });

    var metricsState = progress >= 0.94 ? 'done' : (progress >= 0.8 ? 'current' : 'pending');
    steps.push({ label: '提取指标并校验', state: metricsState });
    if (metricsState === 'current') current = '正在提取指标并校验';

    // 该模型只用于 pending/running 卡片；即使进度已写到 100%，在任务终态
    // 返回前仍显示“正在保存”，避免短暂出现 100% 却误报已完成。
    var saveState = progress >= 0.94 ? 'current' : 'pending';
    steps.push({ label: '保存分析结果', state: saveState });
    if (saveState === 'current') current = '正在保存分析结果';

    return {
      percent: Math.round(progress * 100),
      current: current,
      steps: steps,
    };
  }

  return {
    analysisTerminalBadge: analysisTerminalBadge,
    analysisProgressModel: analysisProgressModel,
    createAnalysisTaskRegistry: createAnalysisTaskRegistry,
    downloadCompletionEffect: downloadCompletionEffect,
    downloadedPdfPreviewUrl: downloadedPdfPreviewUrl,
    goToHistoryReport: goToHistoryReport,
    goToReportChat: goToReportChat,
    historyDimensionDefaults: historyDimensionDefaults,
    historySelectionIsCurrent: historySelectionIsCurrent,
    openPendingHistoryReport: openPendingHistoryReport,
    pdfDownloadFallbackUrl: pdfDownloadFallbackUrl,
    reconcileAnalysisTerminal: reconcileAnalysisTerminal,
    reportKey: reportKey,
  };
}));
