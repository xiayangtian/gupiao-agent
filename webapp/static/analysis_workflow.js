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

  function normalizeTask(task) {
    return {
      taskId: String(task.taskId),
      code: String(task.code),
      period: String(task.period),
      analysisId: String(task.analysisId || ''),
      stage: String(task.stage || 'pending'),
      lastEventId: Math.max(0, Number(task.lastEventId) || 0),
      updatedAt: String(task.updatedAt || ''),
    };
  }

  function parseSavedTasks(raw) {
    var parsed = JSON.parse(raw || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    var normalized = {};
    Object.keys(parsed).forEach(function (savedKey) {
      var task = parsed[savedKey];
      if (!validTask(task) || Array.isArray(task)) return;
      var item = normalizeTask(task);
      normalized[reportKey(item.code, item.period)] = item;
    });
    return normalized;
  }

  function createAnalysisTaskRegistry(storage, storageKey) {
    var key = storageKey || DEFAULT_STORAGE_KEY;
    var tasks = {};
    var needsRepair = false;

    try {
      var raw = storage.getItem(key) || '{}';
      var saved = JSON.parse(raw);
      tasks = parseSavedTasks(raw);
      needsRepair = !saved || typeof saved !== 'object' || Array.isArray(saved)
        || Object.keys(tasks).length !== Object.keys(saved).length
        || Object.keys(saved).some(function (savedKey) {
          var item = saved[savedKey];
          return !validTask(item) || savedKey !== reportKey(item.code, item.period);
        });
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
        var normalized = normalizeTask(task);
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
      applyStorageEvent: function (event) {
        if (!event || event.key !== key) return false;
        try {
          tasks = parseSavedTasks(event.newValue || '{}');
        } catch (_) {
          return false;
        }
        return true;
      },
    };
  }

  function upsertById(items, incoming, idField) {
    var id = incoming && incoming[idField];
    if (!id) return (items || []).slice();
    var replaced = false;
    var output = (items || []).map(function (item) {
      if (String(item[idField]) !== String(id)) return item;
      replaced = true;
      return incoming;
    });
    if (!replaced) output.push(incoming);
    return output;
  }

  function mergeSnapshot(state, snapshot) {
    var current = state || {};
    var wrapper = snapshot || {};
    var result = wrapper.result && typeof wrapper.result === 'object'
      ? wrapper.result : wrapper;
    var merged = Object.assign({}, current);
    Object.keys(result || {}).forEach(function (key) {
      if (key === 'activeTab' || key === 'lastEventId') return;
      merged[key] = result[key];
    });
    if (!merged.stage && wrapper.status) merged.stage = wrapper.status;
    merged.activeTab = current.activeTab || 'quick';
    merged.lastEventId = Math.max(0, Number(current.lastEventId) || 0);
    return merged;
  }

  function applyAnalysisEvent(state, event) {
    var current = state || {};
    var eventId = Math.max(0, Number(event && event.id) || 0);
    if (!event || eventId <= (Number(current.lastEventId) || 0)) return current;
    var payload = event.payload || {};
    var next = Object.assign({}, current, { lastEventId: eventId });
    var type = event.type;

    if (type === 'quick.ready' && payload.quick) {
      next.quick = payload.quick;
      if (!next.activeTab) next.activeTab = 'quick';
    } else if (type === 'quick.corrected' && payload.correction) {
      var quick = Object.assign({}, next.quick || {});
      quick.corrections = (quick.corrections || []).concat([payload.correction]);
      next.quick = quick;
    } else if ((type === 'section.ready' || type === 'section.updated') && payload.section) {
      next.sections = upsertById(next.sections, payload.section, 'section_id');
      next.hasNewFindings = next.activeTab !== payload.section.section_id;
    } else if (type === 'job.stage_changed' && payload.stage) {
      next.stage = payload.stage;
    } else if (type === 'extraction.page_started' || type === 'extraction.page_completed') {
      var pages = Object.assign({}, next.extractionPages || {});
      pages[String(payload.page)] = Object.assign({}, payload, {
        status: type === 'extraction.page_started' ? 'running' : (payload.status || 'completed'),
      });
      next.extractionPages = pages;
    } else if (type === 'theme.started' && payload.candidate_id) {
      next.currentTheme = payload.candidate_id;
    } else if (type === 'theme.filtered' && payload.candidate_id) {
      next.filteredTopics = upsertById(
        next.filteredTopics,
        { candidate_id: payload.candidate_id, reason: payload.reason || '' },
        'candidate_id'
      );
    } else if (type === 'job.completed' || type === 'job.partial'
      || type === 'job.failed' || type === 'job.cancelled') {
      var activeTab = next.activeTab;
      var cursor = next.lastEventId;
      next = mergeSnapshot(next, payload.analysis || {});
      next.activeTab = activeTab || 'quick';
      next.lastEventId = cursor;
      next.stage = type.slice(4);
      if (payload.error) next.error = payload.error;
    }
    return next;
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
    applyAnalysisEvent: applyAnalysisEvent,
    analysisTerminalBadge: analysisTerminalBadge,
    analysisProgressModel: analysisProgressModel,
    createAnalysisTaskRegistry: createAnalysisTaskRegistry,
    downloadCompletionEffect: downloadCompletionEffect,
    downloadedPdfPreviewUrl: downloadedPdfPreviewUrl,
    goToHistoryReport: goToHistoryReport,
    goToReportChat: goToReportChat,
    historyDimensionDefaults: historyDimensionDefaults,
    historySelectionIsCurrent: historySelectionIsCurrent,
    mergeSnapshot: mergeSnapshot,
    openPendingHistoryReport: openPendingHistoryReport,
    pdfDownloadFallbackUrl: pdfDownloadFallbackUrl,
    reconcileAnalysisTerminal: reconcileAnalysisTerminal,
    reportKey: reportKey,
  };
}));
