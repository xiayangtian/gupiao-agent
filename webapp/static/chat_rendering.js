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
    // 某些模型/兼容端会把未执行的工具调用协议误放进 content，而非原生
    // tool_calls 事件。这些协议标记不是用户可读答案，必须在 Markdown 渲染前剥离。
    return String(value == null ? '' : value)
      .replace(/<\s*｜{1,2}DSML｜{1,2}\s*calls\b[^>]*>[\s\S]*?<\/\s*｜{1,2}DSML｜{1,2}\s*calls\s*>/gi, '')
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
    var runId = String(run.id || '');
    if (runId) {
      html += '<span class="chat-run-diagnostic">诊断 ID：<code>' + escapeHtml(runId) + '</code></span>'
        + '<button type="button" class="chat-run-action chat-run-copy-id"'
        + ' data-chat-action="copy-run-id" data-chat-run-id="' + escapeHtml(runId) + '"'
        + ' aria-label="复制诊断 ID" title="复制诊断 ID">复制</button>';
    }
    return html + '</div>';
  }

  return {
    normalizeAssistantMarkdown: normalizeAssistantMarkdown,
    webSourceReferences: webSourceReferences,
    renderScope: renderScope,
    renderRunArtifacts: renderRunArtifacts,
    renderRunStatus: renderRunStatus,
  };
}));
