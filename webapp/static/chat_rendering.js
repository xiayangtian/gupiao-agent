/* 智能问答展示辅助：将模型输出与可信回答契约规范成安全、简洁的 UI 数据。

本模块只做纯函数式渲染，所有文本与 URL 都经过转义/校验；不依赖 DOM、
不依赖 markdown 渲染器，因此可在 Node 单元测试中直接 require。
*/
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.ChatRendering = api;
}(typeof window !== 'undefined' ? window : globalThis, function () {
  function normalizeAssistantMarkdown(value) {
    return String(value == null ? '' : value)
      .replace(/\\?<sup>\s*(\d+)\s*\\?<\/sup>/gi, '[$1]');
  }

  function webSourceReferences(sources) {
    return (Array.isArray(sources) ? sources : [])
      .filter(function (source) {
        return source && /^https?:\/\//i.test(String(source.url || ''));
      })
      .map(function (source) {
        return {
          title: String(source.title || source.url),
          url: String(source.url),
          published_date: String(source.published_date || ''),
        };
      });
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function isHttpUrl(value) {
    return typeof value === 'string' && /^https?:\/\//i.test(value);
  }

  // PDF 跳页 URL 只接受服务端生成的同源相对路径；任何跨协议、带空白或引号的
  // 输入一律拒绝，避免把不可信持久化数据渲染成可点击的伪链接。
  function safePdfUrl(value) {
    if (typeof value !== 'string' || !value) return '';
    if (!/^\//.test(value)) return '';
    if (/^\/\//.test(value)) return '';
    if (/[\s"'<>]/.test(value)) return '';
    return value;
  }

  function positivePage(value) {
    return (typeof value === 'number' && Number.isInteger(value) && value > 0)
      ? value : null;
  }

  var PERIOD_TYPE_LABELS = { annual: '年报', semi_annual: '半年报', quarterly: '季报' };

  // 展示期次必须从 report_ids 推导；多个报告期时逐条列出，绝不谎称单一期次。
  function scopePeriodLabels(reportIds) {
    var seen = {};
    var labels = [];
    (Array.isArray(reportIds) ? reportIds : []).forEach(function (rid) {
      if (typeof rid !== 'string') return;
      var parts = rid.split(':');
      if (parts.length < 3) return;
      var year = parts[1].slice(0, 4);
      var typeLabel = PERIOD_TYPE_LABELS[parts[2]];
      if (!year || !typeLabel) return;
      var label = year + ' ' + typeLabel;
      if (!seen[label]) {
        seen[label] = true;
        labels.push(label);
      }
    });
    return labels;
  }

  function companyLabel(company) {
    if (!company || typeof company !== 'object') return '';
    var name = String(company.name || '');
    var code = String(company.code || '');
    if (!name && !code) return '';
    return escapeHtml(name || code) + (code ? '（' + escapeHtml(code) + '）' : '');
  }

  // 补充授权状态：等待类状态不渲染任何“确认”按钮，重载后无法再授权。
  var SUPPLEMENT_WAITING_STATUSES = {
    proposed: true, approved: true, downloading: true, ingesting: true, resuming: true,
  };
  // 失败原因只暴露受控分类，绝不展示原始错误码、URL 或堆栈。
  var SUPPLEMENT_FAILURE_REASONS = { download_failed: '下载失败', ingest_failed: '索引失败' };

  function supplementPeriodLabel(entry) {
    if (!entry || typeof entry !== 'object') return '';
    var label = String(entry.label || '');
    if (label) return label;
    var reportId = String(entry.report_id || '');
    var parts = reportId.split(':');
    if (parts.length < 3) return '';
    var year = parts[1].slice(0, 4);
    var typeLabel = PERIOD_TYPE_LABELS[parts[2]];
    return (year && typeLabel) ? year + ' ' + typeLabel : '';
  }

  function uniqueLabels(labels) {
    var seen = {};
    var result = [];
    labels.forEach(function (label) {
      if (!label || seen[label]) return;
      seen[label] = true;
      result.push(label);
    });
    return result;
  }

  /* 把已持久化的 run.supplement 摘要转成可渲染的数据；无法诚实表达时返回 null。

  只使用服务端控白的摘要字段（状态/候选身份/已摄取报告 id/受控失败分类），
  不引用模型正文，也不回传候选 id、报告 id、URL 或异常文本。
  */
  function supplementSummaryView(run) {
    if (!run || typeof run !== 'object') return null;
    var supplement = run.supplement;
    if (!supplement || typeof supplement !== 'object' || Array.isArray(supplement)) return null;
    var status = String(supplement.status || '');
    if (!status) return null;

    var candidates = Array.isArray(supplement.candidates) ? supplement.candidates : [];
    var byCandidateId = {};
    candidates.forEach(function (candidate) {
      if (!candidate || typeof candidate !== 'object') return;
      byCandidateId[String(candidate.id || '')] = candidate;
    });
    var waiting = !!SUPPLEMENT_WAITING_STATUSES[status];
    var periods;
    if (waiting) {
      periods = uniqueLabels(candidates.map(supplementPeriodLabel));
    } else {
      var ingested = Array.isArray(supplement.ingested_report_ids)
        ? supplement.ingested_report_ids : [];
      periods = uniqueLabels(ingested.map(function (reportId) {
        return supplementPeriodLabel({ report_id: reportId });
      }));
    }

    var failures = [];
    (Array.isArray(supplement.failed) ? supplement.failed : []).forEach(function (item) {
      if (!item || typeof item !== 'object') return;
      var candidate = byCandidateId[String(item.candidate_id || '')] || {};
      var entry = { reason: SUPPLEMENT_FAILURE_REASONS[String(item.reason || '')] || '补充未成功' };
      var company = String(candidate.company || '');
      var period = supplementPeriodLabel(candidate);
      if (company) entry.company = company;
      if (period) entry.period = period;
      failures.push(entry);
    });

    var headline;
    if (waiting) {
      headline = status === 'proposed' ? '等待你确认是否补充以下财报' : '正在补充财报原文';
    } else if (status === 'completed') {
      headline = periods.length
        ? '本次经授权补充 ' + periods.length + ' 份财报'
        : '本次补充未取得可核验的财报原文';
    } else if (status === 'declined') {
      headline = '本次未补充财报，已基于现有信息回答';
    } else if (status === 'expired') {
      headline = '本次补充授权已过期，已基于现有信息回答';
    } else if (status === 'failed') {
      headline = '本次补充未成功，已基于现有信息回答';
    } else {
      return null;
    }

    return {
      status: status,
      waiting: waiting,
      headline: headline,
      periods: periods,
      skippedCount: Array.isArray(supplement.skipped_report_ids)
        ? supplement.skipped_report_ids.length : 0,
      failures: failures,
    };
  }

  function renderScope(scope) {
    if (!scope || typeof scope !== 'object') return '';
    var mode = String(scope.mode || '');
    var companies = Array.isArray(scope.companies) ? scope.companies : [];
    var company = companyLabel(companies[0]);
    var periodText = scopePeriodLabels(scope.report_ids).join('、');
    var head = '';
    var note = '';

    if (mode === 'company_industry') {
      var industry = scope.industry || {};
      var industryName = String(industry.name || '');
      var parts = [];
      if (company) parts.push(company);
      if (industryName) parts.push(escapeHtml(industryName) + '（数据源分类）');
      if (periodText) parts.push(escapeHtml(periodText));
      head = parts.join(' · ');
      // 本地可检索同业样本：M1 只做本地已索引同业报告的对照，绝不承诺全市场排名。
      note = (industry && industry.sample_kind === 'local_indexed')
        ? '已扩展同业范围：本地可检索同业样本'
        : '已扩展同业范围';
    } else if (mode === 'company_only') {
      var onlyParts = [];
      if (company) onlyParts.push(company);
      onlyParts.push('本公司');
      if (periodText) onlyParts.push(escapeHtml(periodText));
      head = onlyParts.join(' · ');
      if (scope.fallback_reason) note = escapeHtml(scope.fallback_reason);
    } else if (mode === 'whole_corpus') {
      head = '全库财报检索';
    } else {
      return '';
    }

    if (!head && !note) return '';
    return '<div class="chat-scope" role="status" aria-live="polite">'
      + '<span class="chat-scope-line">' + head + '</span>'
      + (note ? '<span class="chat-scope-note">' + note + '</span>' : '')
      + '</div>';
  }

  var PDF_PAGE_LINK_LIMIT = 3;

  function pdfDocumentKey(artifact, url) {
    var reportId = String((artifact && artifact.report_id) || '');
    var filename = String((artifact && artifact.pdf_filename) || '');
    return reportId + '|' + filename + '|' + String(url || '').replace(/[#?].*$/, '');
  }

  function pdfHomeUrl(url) {
    return url ? url.replace(/#.*$/, '') + '#page=1' : '';
  }

  function pdfEntryHtml(entry) {
    if (entry.home) {
      if (entry.url) {
        return '<a class="chat-artifact-link chat-pdf-page" href="' + escapeHtml(entry.url) + '"'
          + ' target="chat-pdf-viewer" rel="noopener noreferrer" data-chat-pdf-home="true">'
          + 'PDF · 打开首页</a>';
      }
      return '<span class="chat-artifact-unavailable">PDF（文件不可用）</span>';
    }
    if (entry.url && entry.page) {
      return '<a class="chat-artifact-link chat-pdf-page" href="' + escapeHtml(entry.url) + '"'
        + ' target="chat-pdf-viewer" rel="noopener noreferrer" data-chat-pdf-page="' + entry.page + '">'
        + 'PDF · 第 ' + entry.page + ' 页</a>';
    }
    return '<span class="chat-artifact-unavailable">PDF · 第 ' + (entry.page || '—') + ' 页（文件不可用）</span>';
  }

  function compactPdfEntries(artifacts) {
    var documents = [];
    var byDocument = {};
    artifacts.forEach(function (artifact) {
      if (!artifact || artifact.source !== 'pdf') return;
      var url = safePdfUrl(artifact.pdf_url);
      var key = pdfDocumentKey(artifact, url);
      var document = byDocument[key];
      if (!document) {
        document = { pages: [], pageMap: {}, firstUrl: url };
        byDocument[key] = document;
        documents.push(document);
      }
      if (!document.firstUrl && url) document.firstUrl = url;
      var page = positivePage(artifact.page);
      var pageKey = page ? String(page) : 'unavailable';
      var pageEntry = document.pageMap[pageKey];
      if (!pageEntry) {
        pageEntry = { page: page, url: url };
        document.pageMap[pageKey] = pageEntry;
        document.pages.push(pageEntry);
      } else if (!pageEntry.url && url) {
        pageEntry.url = url;
      }
    });

    var entries = [];
    documents.forEach(function (document) {
      if (document.pages.length > PDF_PAGE_LINK_LIMIT) {
        entries.push({ home: true, url: pdfHomeUrl(document.firstUrl) });
      } else {
        document.pages.forEach(function (page) { entries.push(page); });
      }
    });
    return entries;
  }

  function compactWebEntries(artifacts) {
    var seen = {};
    var entries = [];
    artifacts.forEach(function (artifact) {
      if (!artifact || artifact.source !== 'web') return;
      var url = isHttpUrl(artifact.url) ? String(artifact.url) : '';
      if (!url || seen[url]) return;
      seen[url] = true;
      entries.push({ url: url, title: String(artifact.title || url) });
    });
    return entries;
  }

  function compactToolEntries(tools) {
    var seen = {};
    var entries = [];
    tools.forEach(function (tool) {
      if (!tool || typeof tool !== 'object') return;
      var provider = String(tool.provider || '');
      var toolName = String(tool.tool_name || '');
      var asOf = String(tool.as_of || '');
      if (!provider && !toolName) return;
      // 工具名不展示时，同一来源、同一数据截至时间只保留一行。
      var key = provider + '|' + asOf;
      if (seen[key]) return;
      seen[key] = true;
      entries.push({ provider: provider || '实时数据', asOf: asOf });
    });
    return entries;
  }

  function renderRunArtifacts(run) {
    if (!run || typeof run !== 'object') return '';
    var artifacts = Array.isArray(run.artifacts) ? run.artifacts : [];
    var tools = Array.isArray(run.tool_artifacts) ? run.tool_artifacts : [];
    var pdfEntries = compactPdfEntries(artifacts);
    var webEntries = compactWebEntries(artifacts);
    var toolEntries = compactToolEntries(tools);
    var parts = [];

    pdfEntries.forEach(function (entry) { parts.push(pdfEntryHtml(entry)); });
    webEntries.forEach(function (entry) {
      parts.push('<a class="chat-artifact-link chat-web-link" href="' + escapeHtml(entry.url) + '"'
        + ' target="_blank" rel="noopener noreferrer">网页 · ' + escapeHtml(entry.title) + '</a>');
    });
    toolEntries.forEach(function (entry) {
      var label = '实时数据 · ' + entry.provider;
      if (entry.asOf) label += ' · 数据截至 ' + entry.asOf;
      parts.push('<span class="chat-artifact-tool">' + escapeHtml(label) + '</span>');
    });
    if (!parts.length) return '';

    return '<details class="chat-artifacts" aria-label="回答证据与来源">'
      + '<summary>证据与来源（' + parts.length + '）</summary>'
      + '<div class="chat-artifacts-list">' + parts.join('') + '</div>'
      + '</details>';
  }

  function renderRunStatus(run) {
    if (!run || typeof run !== 'object') return '';
    if (run.legacy_evidence_unavailable) {
      return '<div class="chat-run-status chat-run-status-legacy" role="status">'
        + '<span class="chat-run-status-label">历史回答，未保留证据包</span></div>';
    }
    var status = String(run.status || '');
    var label = {
      completed: '✅ 已完成',
      partial: '⚠️ 部分完成',
      stopped: '⏹ 已停止',
      failed: '❌ 失败',
      waiting_consent: '⏸ 等待授权补充财报',
    }[status];
    if (!label) return '';

    var html = '<div class="chat-run-status chat-run-status-' + escapeHtml(status)
      + '" role="status" aria-live="polite">'
      + '<span class="chat-run-status-label">' + label + '</span>';
    if (status === 'stopped' || status === 'partial') {
      // 恢复状态机到 M3 才实现：仅渲染禁用占位，避免承诺当前不存在的能力。
      html += '<button type="button" class="chat-run-action chat-run-continue"'
        + ' data-chat-action="continue" disabled title="恢复研究将在后续版本提供">继续研究</button>';
    }
    if (status === 'stopped' || status === 'partial' || status === 'failed') {
      html += '<button type="button" class="chat-run-action chat-run-regenerate"'
        + ' data-chat-action="regenerate">重新生成</button>';
    }
    return html + '</div>';
  }

  return {
    normalizeAssistantMarkdown: normalizeAssistantMarkdown,
    webSourceReferences: webSourceReferences,
    renderScope: renderScope,
    renderRunArtifacts: renderRunArtifacts,
    renderRunStatus: renderRunStatus,
    supplementSummaryView: supplementSummaryView,
  };
}));
