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
      if (payload.evidence_catalog) next.evidence_catalog = payload.evidence_catalog;
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

  function downloadedPdfPreviewUrl(selected, code, period, version) {
    if (!downloadCompletionEffect(selected, code, period).sameReport) return null;
    var url = '/api/reports/' + encodeURIComponent(code) + '/'
      + encodeURIComponent(period) + '.pdf';
    return version === undefined ? url : url + '?v=' + encodeURIComponent(version);
  }

  function downloadCompletionEffect(selected, code, period) {
    var sameCompany = !!selected && String(selected.code) === String(code);
    return {
      sameCompany: sameCompany,
      sameReport: sameCompany && String(selected.period) === String(period),
    };
  }

  function pdfDownloadFallbackUrl(selected, code, period, status, version) {
    if (Number(status) !== 404 && Number(status) !== 405) return null;
    return downloadedPdfPreviewUrl(selected, code, period, version);
  }

  var ANALYSIS_EVENT_TYPES = [
    'job.stage_changed', 'extraction.page_started', 'extraction.page_completed',
    'quick.ready', 'quick.corrected', 'theme.started', 'theme.filtered',
    'section.ready', 'section.updated', 'job.completed', 'job.partial',
    'job.failed', 'job.cancelled'
  ];

  function isTerminalStatus(status) {
    return ['done', 'completed', 'partial', 'failed', 'cancelled'].indexOf(status) >= 0;
  }

  function createAnalysisStreamController(options) {
    var opts = options || {};
    var EventSourceClass = opts.EventSourceClass;
    var setTimeoutFn = opts.setTimeoutFn || setTimeout;
    var clearTimeoutFn = opts.clearTimeoutFn || clearTimeout;
    var sources = {};
    var timers = {};
    var backoffs = {};

    function close(taskId) {
      if (sources[taskId]) sources[taskId].close();
      if (timers[taskId]) clearTimeoutFn(timers[taskId]);
      delete sources[taskId];
      delete timers[taskId];
      delete backoffs[taskId];
    }

    function poll(task) {
      Promise.resolve(opts.fetchSnapshot(task)).then(function (snapshot) {
        if (opts.onSnapshot) opts.onSnapshot(task, snapshot);
        if (isTerminalStatus(snapshot && snapshot.status)) {
          close(task.taskId);
          return;
        }
        schedulePoll(task);
      }).catch(function () { schedulePoll(task); });
    }

    function schedulePoll(task) {
      var delay = backoffs[task.taskId] || 1000;
      backoffs[task.taskId] = Math.min(delay * 2, 10000);
      timers[task.taskId] = setTimeoutFn(function () { poll(task); }, delay);
    }

    function connect(task) {
      if (!task || !task.taskId || sources[task.taskId] || timers[task.taskId]) return;
      if (!EventSourceClass) {
        schedulePoll(task);
        return;
      }
      var separator = String(task.eventUrl || '').indexOf('?') >= 0 ? '&' : '?';
      var source = new EventSourceClass(
        String(task.eventUrl || '') + separator + 'after=' + (Number(task.lastEventId) || 0)
      );
      sources[task.taskId] = source;
      ANALYSIS_EVENT_TYPES.forEach(function (type) {
        source.addEventListener(type, function (message) {
          var payload = {};
          try { payload = JSON.parse(message.data || '{}'); } catch (_) { return; }
          var event = { id: Number(message.lastEventId) || 0, type: type, payload: payload };
          task.lastEventId = Math.max(task.lastEventId || 0, event.id);
          if (opts.onEvent) opts.onEvent(task, event);
          if (type.indexOf('job.') === 0 && isTerminalStatus(type.slice(4))) close(task.taskId);
        });
      });
      source.onopen = function () { backoffs[task.taskId] = 1000; };
      source.onerror = function () {
        if (sources[task.taskId] !== source) return;
        if (sources[task.taskId]) sources[task.taskId].close();
        delete sources[task.taskId];
        schedulePoll(task);
      };
      return source;
    }

    return { connect: connect, close: close, closeAll: function () {
      Object.keys(sources).concat(Object.keys(timers)).forEach(close);
    } };
  }

  function escapeMarkup(value) {
    return String(value == null ? '' : value).replace(/&/g, '&amp;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function emphasizedText(value, spans) {
    var source = String(value || '');
    var selected = (spans || []).map(String).filter(Boolean).slice(0, 2);
    var ranges = [];
    selected.forEach(function (span) {
      var start = source.indexOf(span);
      if (start >= 0 && !ranges.some(function (range) {
        return start < range.end && start + span.length > range.start;
      })) ranges.push({ start: start, end: start + span.length });
    });
    ranges.sort(function (a, b) { return a.start - b.start; });
    var cursor = 0;
    return ranges.map(function (range) {
      var html = escapeMarkup(source.slice(cursor, range.start))
        + '<mark>' + escapeMarkup(source.slice(range.start, range.end)) + '</mark>';
      cursor = range.end;
      return html;
    }).join('') + escapeMarkup(source.slice(cursor));
  }

  function evidenceKind(evidence) {
    var type = String((evidence || {}).source_type || '');
    if (['pdf_text', 'ocr', 'ocr_text', 'ocr_table', 'chart'].indexOf(type) >= 0) {
      return 'pdf';
    }
    if (type === 'structured') return 'structured';
    return 'other';
  }

  function uniqueEvidenceIds(items) {
    var seen = {};
    return (items || []).reduce(function (ids, item) {
      (item && item.evidence_ids || []).forEach(function (id) {
        id = String(id);
        if (!seen[id]) {
          seen[id] = true;
          ids.push(id);
        }
      });
      return ids;
    }, []);
  }

  function evidencePdfPreviewUrl(selected, page, jumpVersion) {
    var number = Number(page);
    if (!selected || !selected.code || !selected.period
      || !Number.isInteger(number) || number < 1) return null;
    var url = '/api/reports/' + encodeURIComponent(selected.code) + '/'
      + encodeURIComponent(selected.period) + '.pdf';
    if (jumpVersion !== undefined) url += '?jump=' + encodeURIComponent(jumpVersion);
    return url + '#page=' + number;
  }

  function findingTone(item) {
    if (item && (item.risk_state === 'verified_risk' || item.style === 'verified_risk')) {
      return { key: 'risk', label: '风险' };
    }
    if (item && item.style === 'observation') return { key: 'observation', label: '观察' };
    if (item && (item.risk_state === 'neutral' || item.style === 'highlight')) {
      return { key: 'highlight', label: '重点' };
    }
    return { key: 'pending', label: '待核验' };
  }

  function evidenceAnchor(options) {
    var anchor = options && options.evidenceAnchor;
    return typeof anchor === 'string' && /^[A-Za-z][A-Za-z0-9_-]*$/.test(anchor)
      ? anchor : 'analysis-evidence';
  }

  function evidenceItemAnchor(anchor, id) {
    var encoded = String(id).split('').map(function (character) {
      return character.charCodeAt(0).toString(16);
    }).join('-');
    return anchor + '-' + encoded;
  }

  function renderEvidenceCitationLinks(ids, catalog) {
    return (ids || []).map(function (id) {
      var record = (catalog || {})[id] || {};
      var locator = record.source_locator || {};
      var page = Number(locator.page);
      if (record.source_type === 'pdf_text' && Number.isInteger(page) && page > 0) {
        return '<button type="button" class="analysis-evidence-page" data-evidence-page="'
          + page + '">PDF 第 ' + page + ' 页</button>';
      }
      var url = record.url || locator.url || '';
      if ((record.source_type === 'web' || record.source_type === 'web_search')
        && /^https?:\/\//i.test(String(url))) {
        return '<a class="analysis-evidence-link" href="' + escapeMarkup(url)
          + '" target="_blank" rel="noopener noreferrer">网页证据</a>';
      }
      return '';
    }).filter(Boolean).join('');
  }

  function readableSupplement(value) {
    var text = String(value == null ? '' : value).trim();
    return !!text && !/^(?:high|medium|low|positive|negative|neutral)$/i.test(text)
      && !/^[{[]/.test(text);
  }

  function isCompactFinding(finding, claim) {
    return !finding.title && !readableSupplement(finding.key_data)
      && !readableSupplement(finding.significance) && String(claim).length <= 160;
  }

  function renderReportFinding(item, index, catalog) {
    var finding = item || {};
    var claim = finding.claim || finding.text || finding.summary || '';
    if (!claim) return '';
    var citations = renderEvidenceCitationLinks(finding.evidence_ids, catalog);
    var keyData = readableSupplement(finding.key_data) ? finding.key_data : '';
    var significance = readableSupplement(finding.significance) ? finding.significance : '';
    if (isCompactFinding(finding, claim)) {
      return '<p class="analysis-compact-finding">' + emphasizedText(claim, finding.highlight_spans)
        + (citations ? ' <span class="analysis-inline-citations">' + citations + '</span>' : '') + '</p>';
    }
    var tone = findingTone(finding);
    return '<article class="analysis-report-finding analysis-tone-' + tone.key + '">'
      + '<span class="analysis-finding-label">' + tone.label + '</span>'
      + (finding.title ? '<h3>' + escapeMarkup(finding.title) + '</h3>' : '')
      + '<p>' + emphasizedText(claim, finding.highlight_spans) + '</p>'
      + (keyData ? '<p class="analysis-key-data">' + escapeMarkup(keyData) + '</p>' : '')
      + (significance ? '<p class="analysis-significance">' + escapeMarkup(significance) + '</p>' : '')
      + (citations ? '<span class="analysis-inline-citations">' + citations + '</span>' : '')
      + '</article>';
  }

  function renderSummary(items) {
    var total = (items || []).length;
    var cards = (items || []).slice(0, 3).map(function (item) {
      var finding = item || {};
      var claim = finding.claim || finding.text || finding.summary || '';
      if (!claim) return '';
      var tone = findingTone(finding);
      return '<article class="summary-item summary-tone-' + tone.key + '">'
        + '<span class="analysis-finding-label">' + tone.label + '</span>'
        + '<p>' + escapeMarkup(claim) + '</p>'
        + (finding.key_data ? '<strong>' + escapeMarkup(finding.key_data) + '</strong>' : '')
        + '</article>';
    }).filter(Boolean).join('');
    return cards ? '<section class="analysis-report-summary"><h3>本期要点（' + total + '）</h3>'
      + cards + '</section>' : '';
  }

  function renderEvidenceSection(items, catalog, options) {
    var evidence = catalog || {};
    var anchor = evidenceAnchor(options);
    var allowPdfLinks = !options || options.pdfEvidenceLinks !== false;
    var entries = uniqueEvidenceIds(items).map(function (id, index) {
      var record = evidence[id];
      if (!record) return '';
      var locator = record.source_locator || {};
      var kind = evidenceKind(record);
      var page = Number(locator.page);
      var hasPage = kind === 'pdf' && Number.isInteger(page) && page > 0;
      var label = record.label || record.fact_name || id;
      var excerpt = record.excerpt || [record.value, record.unit].filter(Boolean).join(' ');
      var meta = [record.period, excerpt].filter(Boolean).join(' · ');
      var sourceLabel = kind === 'structured' ? '结构化数据'
        : (hasPage ? 'PDF · 第 ' + page + ' 页' : (record.source_type || '来源记录'));
      return '<li id="' + escapeMarkup(evidenceItemAnchor(anchor, id))
        + '" class="analysis-evidence-item" tabindex="-1"><span class="analysis-evidence-index">'
        + (index + 1) + '</span><strong>' + escapeMarkup(label) + '</strong>'
        + (hasPage && allowPdfLinks ? '<button type="button" class="analysis-evidence-page" data-evidence-page="'
          + page + '">PDF · 第 ' + page + ' 页</button>'
          : '<span class="analysis-evidence-source">'
            + escapeMarkup(sourceLabel) + '</span>')
        + (meta ? '<span class="analysis-evidence-excerpt">' + escapeMarkup(meta) + '</span>' : '')
        + '</li>';
    }).filter(Boolean).join('');
    return entries ? '<details class="analysis-evidence-section"><summary>证据与出处（'
      + (entries.match(/<li /g) || []).length + '）</summary><ol>' + entries + '</ol></details>' : '';
  }

  function missingValue(value) {
    return /^(?:|[-—/]|未披露|未提供|暂无数据|无数据|不适用|n\/?a)$/i.test(String(value == null ? '' : value).trim());
  }

  function cleanTableRows(rows) {
    return (rows || []).filter(function (row) {
      var cells = Array.isArray(row) ? row : Object.values(row || {});
      return cells.slice(1).some(function (cell) { return !missingValue(cell); });
    });
  }

  function renderProgressiveAnalysis(state, options) {
    var current = state || {};
    var catalog = current.evidence_catalog || {};
    var quick = current.quick || {};
    var conclusions = (quick.conclusions || []).filter(Boolean);
    var observations = (current.observations || []).filter(function (item) {
      return item && item.title && item.summary;
    }).map(function (item) {
      return Object.assign({}, item, { style: item.style || 'observation' });
    });
    var sections = (current.sections || []).filter(function (section) {
      return Array.isArray(section.findings) && section.findings.length > 0;
    });
    var active = current.activeTab || 'quick';
    var anchor = evidenceAnchor(options);
    var quickItems = conclusions.length ? conclusions : observations;
    var completedWithoutQuick = current.stage === 'completed' || current.stage === 'partial';
    var tabs = sections.map(function (section, index) {
      var selected = active === section.section_id;
      return '<button type="button" class="analysis-result-tab' + (selected ? ' active' : '')
        + '" data-analysis-tab="' + escapeMarkup(section.section_id) + '" role="tab" aria-selected="'
        + (selected ? 'true' : 'false') + '">0' + (index + 2) + ' '
        + escapeMarkup(section.title) + '（' + section.findings.length + '）</button>';
    }).join('');
    var correctionHtml = (quick.corrections || []).map(function (item) {
      return '<div class="analysis-correction"><strong>快速结论已校正</strong><p>'
        + escapeMarkup(item.before) + ' → ' + escapeMarkup(item.after) + '</p></div>';
    }).join('');
    var observationIntro = !conclusions.length && observations.length
      ? '<h3 class="analysis-observation-title">参考观察</h3>'
        + '<p class="hint">以下候选观察证据不足，未达到详细分析标准，仅供参考。</p>'
      : '';
    var body = '';
    if (active === 'quick') {
      if (quickItems.length) {
        body = observationIntro + '<section class="analysis-report-body">'
          + quickItems.map(function (item, index) {
            return renderReportFinding(item, index, catalog);
          }).join('') + '</section>' + correctionHtml;
      } else if (completedWithoutQuick) {
        body = '<section class="analysis-report-body"><p class="hint">本次未生成可核验的快速结论。</p></section>';
      } else {
        body = '<section class="analysis-report-body"><p class="hint">快速结论生成中…</p></section>';
      }
    } else {
      body = sections.filter(function (section) { return section.section_id === active; })
        .map(function (section) {
          return '<section class="analysis-report-body">'
            + section.findings.map(function (item, index) {
              return renderReportFinding(item, index, catalog);
            }).join('') + '</section>';
        }).join('');
    }
    return '<div class="analysis-result-tabs" role="tablist">'
      + '<button type="button" class="analysis-result-tab' + (active === 'quick' ? ' active' : '')
      + '" data-analysis-tab="quick" role="tab" aria-selected="'
      + (active === 'quick' ? 'true' : 'false') + '">01 快速结论（' + quickItems.length + '）</button>'
      + '<span class="analysis-dynamic-tabs">' + tabs + '</span></div>'
      + '<div class="analysis-progressive-body">' + body + '</div>';
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

  function analysisErrorMessage(status, contentType, bodyText) {
    var type = String(contentType || '').toLowerCase();
    if (type.indexOf('application/json') >= 0) {
      try {
        var data = JSON.parse(String(bodyText || '').trim());
        if (data && typeof data === 'object') {
          var detail = data.detail || data.error || data.message;
          if (typeof detail === 'string' && detail.trim()) return detail.trim();
        }
      } catch (_) { /* 忽略解析失败，回退到通用提示 */ }
    }
    var hint = status >= 500 ? '服务暂时不可用，请稍后重试' : '请求失败，请稍后重试';
    return 'HTTP ' + status + '：' + hint;
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
    analysisErrorMessage: analysisErrorMessage,
    cleanTableRows: cleanTableRows,
    analysisTerminalBadge: analysisTerminalBadge,
    analysisProgressModel: analysisProgressModel,
    createAnalysisTaskRegistry: createAnalysisTaskRegistry,
    createAnalysisStreamController: createAnalysisStreamController,
    downloadCompletionEffect: downloadCompletionEffect,
    downloadedPdfPreviewUrl: downloadedPdfPreviewUrl,
    evidencePdfPreviewUrl: evidencePdfPreviewUrl,
    goToHistoryReport: goToHistoryReport,
    goToReportChat: goToReportChat,
    historyDimensionDefaults: historyDimensionDefaults,
    historySelectionIsCurrent: historySelectionIsCurrent,
    mergeSnapshot: mergeSnapshot,
    openPendingHistoryReport: openPendingHistoryReport,
    pdfDownloadFallbackUrl: pdfDownloadFallbackUrl,
    reconcileAnalysisTerminal: reconcileAnalysisTerminal,
    renderProgressiveAnalysis: renderProgressiveAnalysis,
    reportKey: reportKey,
  };
}));
