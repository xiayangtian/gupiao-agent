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

  function pdfArtifactHtml(artifact) {
    var page = positivePage(artifact && artifact.page);
    var url = safePdfUrl(artifact && artifact.pdf_url);
    var snippet = String((artifact && artifact.snippet) || '');
    var label = 'PDF 原文';
    if (page) label += ' · 第 ' + page + ' 页';

    var html = '<span class="chat-artifact-label">' + escapeHtml(label) + '</span>';
    if (url && page) {
      html += '<a class="chat-pdf-page" href="' + escapeHtml(url) + '"'
        + ' target="chat-pdf-viewer" rel="noopener noreferrer"'
        + ' data-chat-pdf-page="' + page + '">'
        + '打开 PDF 原文 · 第 ' + page + ' 页</a>';
    } else {
      // 文件缺失 / URL 不可信：显示不可用，绝不生成伪页码跳转。
      html += '<span class="chat-artifact-unavailable">来源文件不可用</span>';
    }
    if (snippet) {
      html += '<details class="chat-artifact-snippet"><summary>片段</summary>'
        + '<div>' + escapeHtml(snippet) + '</div></details>';
    }
    return '<div class="chat-artifact chat-artifact-pdf">' + html + '</div>';
  }

  function webArtifactHtml(artifact) {
    var url = isHttpUrl(artifact && artifact.url) ? String(artifact.url) : '';
    var title = String((artifact && artifact.title) || '');
    var publishedAt = String((artifact && artifact.published_at) || '');
    var fetchedAt = String((artifact && artifact.fetched_at) || '');
    var snippet = String((artifact && artifact.snippet) || '');
    if (!url) return '';

    var metaParts = [];
    if (publishedAt) metaParts.push('发布于 ' + publishedAt);
    if (fetchedAt) metaParts.push('抓取于 ' + fetchedAt);

    var html = '<span class="chat-artifact-label">网页来源</span>'
      + '<a class="chat-web-link" href="' + escapeHtml(url) + '"'
      + ' target="_blank" rel="noopener noreferrer">' + escapeHtml(title || url) + '</a>';
    if (metaParts.length) {
      html += '<span class="chat-artifact-meta">' + metaParts.map(escapeHtml).join(' · ') + '</span>';
    }
    if (snippet) {
      html += '<details class="chat-artifact-snippet"><summary>摘要</summary>'
        + '<div>' + escapeHtml(snippet) + '</div></details>';
    }
    return '<div class="chat-artifact chat-artifact-web">' + html + '</div>';
  }

  function toolArtifactHtml(artifact) {
    var provider = String((artifact && artifact.provider) || '');
    var toolName = String((artifact && artifact.tool_name) || '');
    var asOf = String((artifact && artifact.as_of) || '');
    var status = String((artifact && artifact.status) || '');
    var argumentsSummary = String((artifact && artifact.arguments_summary) || '');
    var resultSummary = String((artifact && artifact.result_summary) || '');
    if (!provider && !toolName) return '';

    var metaParts = [];
    if (toolName) metaParts.push(toolName);
    if (asOf) metaParts.push('数据截至 ' + asOf);
    if (status) metaParts.push(status === 'success' ? '已获取' : '获取失败');

    var html = '<span class="chat-artifact-label">实时工具</span>'
      + '<span class="chat-artifact-title">' + escapeHtml(provider || '实时工具') + '</span>';
    if (metaParts.length) {
      html += '<span class="chat-artifact-meta">' + metaParts.map(escapeHtml).join(' · ') + '</span>';
    }
    // arguments_summary 可能被截断成非 JSON 文本：一律按纯文本转义展示，绝不 JSON.parse。
    if (argumentsSummary) {
      html += '<details class="chat-artifact-snippet"><summary>参数摘要</summary>'
        + '<div>' + escapeHtml(argumentsSummary) + '</div></details>';
    }
    if (resultSummary) {
      html += '<details class="chat-artifact-snippet"><summary>结果摘要</summary>'
        + '<div>' + escapeHtml(resultSummary) + '</div></details>';
    }
    return '<div class="chat-artifact chat-artifact-tool">' + html + '</div>';
  }

  function renderRunArtifacts(run) {
    if (!run || typeof run !== 'object') return '';
    var artifacts = Array.isArray(run.artifacts) ? run.artifacts : [];
    var tools = Array.isArray(run.tool_artifacts) ? run.tool_artifacts : [];
    if (!artifacts.length && !tools.length) return '';

    var parts = [];
    artifacts.forEach(function (artifact) {
      if (!artifact || typeof artifact !== 'object') return;
      if (artifact.source === 'pdf') {
        var pdf = pdfArtifactHtml(artifact);
        if (pdf) parts.push(pdf);
      } else if (artifact.source === 'web') {
        var web = webArtifactHtml(artifact);
        if (web) parts.push(web);
      }
    });
    tools.forEach(function (tool) {
      var rendered = toolArtifactHtml(tool);
      if (rendered) parts.push(rendered);
    });
    if (!parts.length) return '';

    return '<section class="chat-artifacts" aria-label="回答证据与来源">'
      + '<div class="chat-artifacts-head">证据与来源</div>'
      + parts.join('')
      + '</section>';
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
