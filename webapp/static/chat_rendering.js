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
    var ingestedCount = 0;
    if (waiting) {
      periods = uniqueLabels(candidates.map(supplementPeriodLabel));
    } else {
      var ingested = Array.isArray(supplement.ingested_report_ids)
        ? supplement.ingested_report_ids : [];
      // 数量忠实反映已摄取报告 ID 的条数；人类可读期次列表仍去重。
      ingestedCount = ingested.length;
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
      headline = ingestedCount
        ? '本次经授权补充 ' + ingestedCount + ' 份财报'
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

  function renderPolicy(policy) {
    if (!policy || typeof policy !== 'object') return '';
    var label = {
      report_fact: '本地财报查证', company_trend: '本地财报趋势对照',
      industry_benchmark: '本地可检索同业样本', realtime_market: '实时行情查证',
      event_attribution: '公开事件参考', research_task: '受限研究计划与来源核对'
    }[String(policy.intent || '')];
    return label ? '<details class="chat-policy"><summary>本次查证方式</summary><span>'
      + escapeHtml(label) + '</span></details>' : '';
  }

  function factSourceLabel(fact) {
    return fact && fact.source_type === 'pdf' ? 'PDF 原文' : '实时数据';
  }

  function renderFactsAndConflicts(run) {
    if (!run || typeof run !== 'object') return '';
    var parts = [];
    (Array.isArray(run.facts) ? run.facts : []).forEach(function (fact) {
      if (!fact || typeof fact !== 'object') return;
      var label = fact.verification === 'reference' ? '外部参考' : 'PDF 原文';
      var asOf = fact.verification === 'reference' && fact.as_of ? ' · 数据截至 ' + escapeHtml(fact.as_of) : '';
      parts.push('<div class="chat-fact chat-fact-' + escapeHtml(fact.verification || '') + '">'
        + escapeHtml(label + ' · ' + String(fact.metric || '') + '：' + String(fact.value || '') + ' ' + String(fact.unit || '')) + asOf + '</div>');
    });
    (Array.isArray(run.conflicts) ? run.conflicts : []).forEach(function (conflict) {
      if (!conflict || typeof conflict !== 'object') return;
      var labels = (Array.isArray(conflict.facts) ? conflict.facts : []).map(factSourceLabel).filter(function (value, index, all) { return all.indexOf(value) === index; });
      parts.push('<div class="chat-conflict" role="status"><strong>存在口径/时间差异</strong>：'
        + escapeHtml(String(conflict.reason || '来源数值不一致')) + '（' + escapeHtml(labels.join('、')) + '）</div>');
    });
    return parts.length ? '<div class="chat-facts-conflicts">' + parts.join('') + '</div>' : '';
  }

  function renderVerification(report) {
    if (!report || typeof report !== 'object' || report.status === 'passed') return '';
    var label = report.status === 'blocked' ? '未找到可核验的披露，不能确认该数值。' : '该回答含外部参考或口径差异，请结合来源核对。';
    return '<div class="chat-verification chat-verification-' + escapeHtml(report.status) + '" role="status">' + escapeHtml(label) + '</div>';
  }

  function researchStepLabel(step) {
    return String((step && step.label) || '研究步骤');
  }

  var RESEARCH_STEP_STATUS_LABELS = {
    pending: '待执行', running: '进行中', completed: '已完成',
    stopped: '已停止', failed: '失败', skipped: '已跳过',
  };

  function renderResearchPlan(plan) {
    if (!plan || typeof plan !== 'object') return '';
    var steps = Array.isArray(plan.steps) ? plan.steps : [];
    var acceptance = Array.isArray(plan.acceptance) ? plan.acceptance : [];
    if (!steps.length) return '';
    return '<details class="research-plan"><summary>研究计划（' + steps.length + ' 步）</summary>'
      + '<ol>' + steps.map(function (step) { return '<li>' + escapeHtml(researchStepLabel(step)) + '</li>'; }).join('') + '</ol>'
      + '<div class="research-acceptance"><strong>完成判据</strong>：' + escapeHtml(acceptance.join('；')) + '</div></details>';
  }

  function renderResearchSteps(run) {
    if (!run || typeof run !== 'object' || !Array.isArray(run.step_runs)) return '';
    var labels = {};
    ((run.plan && Array.isArray(run.plan.steps)) ? run.plan.steps : []).forEach(function (step) {
      labels[String(step.id || '')] = researchStepLabel(step);
    });
    return '<div class="research-steps" role="status" aria-live="polite">' + run.step_runs.map(function (step) {
      var status = String(step.status || 'pending');
      var label = labels[String(step.step_id || '')] || String(step.step_id || '研究步骤');
      return '<div class="research-step research-step-' + escapeHtml(status) + '"><span aria-hidden="true">'
        + (status === 'completed' ? '✓' : status === 'failed' ? '!' : status === 'running' ? '…' : '•') + '</span> '
        + escapeHtml(label) + '：' + escapeHtml(RESEARCH_STEP_STATUS_LABELS[status] || status) + '</div>';
    }).join('') + '</div>';
  }

  // 研究运行的恢复资格只看可恢复终态：已完成或核验中的持久化状态一律不提供入口。
  var RESUMABLE_RESEARCH_STATUSES = { stopped: true, partial: true, failed: true };
  var UNAVAILABLE_RESEARCH_STATUSES = { completed: true, awaiting_input: true, verifying: true };

  /* 只对确实还可恢复的研究运行返回 true。

  已完成（或核验/等待补充）的持久化状态意味着恢复端点必然拒绝，此时不得渲染
  可点击的「继续研究」死按钮。前端刚停止、持久化状态还未回到页面时，运行仍是
  stopped/failed 且没有不可恢复的持久化状态，入口必须保留。
  */
  function researchRunIsResumable(run, researchRun) {
    var summaryStatus = (run.research_summary && typeof run.research_summary === 'object')
      ? String(run.research_summary.status || '') : '';
    var liveStatus = (researchRun && typeof researchRun === 'object')
      ? String(researchRun.status || '') : '';
    if (UNAVAILABLE_RESEARCH_STATUSES[summaryStatus] || UNAVAILABLE_RESEARCH_STATUSES[liveStatus]) return false;
    return !!(RESUMABLE_RESEARCH_STATUSES[summaryStatus] || RESUMABLE_RESEARCH_STATUSES[liveStatus]
      || RESUMABLE_RESEARCH_STATUSES[String(run.status || '')]);
  }

  function renderResearchRecovery(run, researchRun) {
    if (!run || typeof run !== 'object' || !run.research_run_id) return '';
    if (!researchRunIsResumable(run, researchRun)) return '';
    var summary = run.research_summary || {};
    var from = String(summary.resume_from_step_id || '未完成步骤');
    return '<div class="research-recovery"><span>从步骤：' + escapeHtml(from) + '</span><button type="button" class="chat-run-action" data-chat-action="resume-research" data-research-run-id="' + escapeHtml(String(run.research_run_id)) + '">继续研究</button></div>';
  }

  function renderWorkspaceItem(item) {
    if (!item || typeof item !== 'object' || !item.run_id || !item.session_id) return '';
    var companies = (Array.isArray(item.company_codes) ? item.company_codes : []).filter(Boolean);
    var periods = (Array.isArray(item.periods) ? item.periods : []).filter(Boolean);
    var scope = [companies.join('、'), item.industry, periods.join('、'), item.intent].filter(Boolean).join(' · ') || '未记录范围元数据';
    var evidence = item.evidence_available === true ? '证据：可用'
      : item.evidence_available === false ? '证据：暂不可用'
      : '证据：打开回答查看';
    var favorite = !!item.favorite;
    var summary = String(item.searchable_summary || '').trim();
    return '<article class="research-workspace-item" data-research-run-id="' + escapeHtml(String(item.run_id))
      + '" data-research-session-id="' + escapeHtml(String(item.session_id)) + '">'
      + '<div class="research-workspace-item-main"><h3>' + escapeHtml(String(item.title || '未命名研究')) + '</h3>'
      + '<p class="research-workspace-scope"><strong>范围：</strong>' + escapeHtml(scope) + '</p>'
      + (summary ? '<p class="research-workspace-summary">已保存决策：' + escapeHtml(summary) + '</p>' : '')
      + '<p class="research-workspace-meta"><span>状态：' + escapeHtml(String(item.status || 'unknown')) + '</span>'
      + '<span>更新时间：' + escapeHtml(String(item.updated_at || '未记录')) + '</span><span>' + escapeHtml(evidence) + '</span></p></div>'
      + '<div class="research-workspace-actions"><button type="button" class="btn" data-research-action="open">打开回答</button>'
      + '<button type="button" class="btn" data-research-action="favorite" aria-pressed="' + (favorite ? 'true' : 'false') + '">'
      + (favorite ? '取消收藏' : '收藏') + '</button>'
      + '<button type="button" class="btn" data-research-action="export" data-research-format="markdown">导出 Markdown</button>'
      + '<button type="button" class="btn" data-research-action="export" data-research-format="json">导出 JSON</button></div></article>';
  }

  function renderFactActions(fact, run) {
    if (!fact || !run || fact.verification !== 'verified' || fact.source_type !== 'pdf'
      || (run.status !== 'completed' && run.status !== 'partial')) return '';
    var factId = String(fact.id || '').trim();
    var evidenceIds = Array.isArray(fact.evidence_ids) ? fact.evidence_ids.filter(Boolean) : [];
    var report = run.verification_report || {};
    var supported = Array.isArray(report.supported_fact_ids) ? report.supported_fact_ids : [];
    if (!factId || !evidenceIds.length || !run.id || (report.status !== 'passed' && report.status !== 'partial')
      || !evidenceIds.every(function (id) { return supported.indexOf(id) >= 0; })) return '';
    return '<button type="button" class="chat-run-action" data-research-action="save-memory"'
      + ' data-research-memory-kind="fact" data-research-id="' + escapeHtml(factId) + '"'
      + ' data-research-run-id="' + escapeHtml(String(run.id)) + '">保存到研究记忆</button>';
  }

  // Only safe aggregate booleans are rendered.  The API deliberately omits case,
  // prompt, and failure details, and this renderer must never infer or expose them.
  function renderResearchQuality(summary) {
    var refreshCommand = '<code>python3 scripts/run_chat_evaluation.py</code>';
    if (!summary || summary.available !== true
      || !summary.health || typeof summary.health.passed !== 'boolean'
      || !summary.probe || typeof summary.probe.passed !== 'boolean') {
      return '<p class="research-quality-unavailable"><span aria-hidden="true">○</span> '
        + '尚无本地质量摘要。可运行 ' + refreshCommand + ' 生成。</p>';
    }
    function suite(label, passed) {
      return '<p class="research-quality-status research-quality-' + (passed ? 'passed' : 'failed') + '">'
        + '<span aria-hidden="true">' + (passed ? '✓' : '×') + '</span> '
        + escapeHtml(label) + '：' + (passed ? '通过' : '未通过') + '</p>';
    }
    return '<div class="research-quality-summary">'
      + suite('健康评测', summary.health.passed)
      + suite('负向探针', summary.probe.passed)
      + '<p class="research-quality-generated-at">生成时间：'
      + escapeHtml(String(summary.generated_at || '未记录')) + '</p>'
      + '<p class="research-quality-refresh">可运行 ' + refreshCommand + ' 刷新。</p></div>';
  }

  function renderArtifactActions(run) {
    if (!run || !run.id || !Array.isArray(run.artifacts)) return '';
    var entries = run.artifacts.map(function (artifact) {
      if (!artifact || typeof artifact !== 'object') return '';
      var id = artifact.source === 'web' ? artifact.url
        : (artifact.pdf_url || artifact.pdf_filename || (artifact.report_id && artifact.page ? artifact.report_id + '#p' + artifact.page : ''));
      if (!id || (artifact.source === 'pdf' && artifact.availability !== 'available')) return '';
      return '<button type="button" class="chat-run-action" data-research-action="save-memory"'
        + ' data-research-memory-kind="artifact" data-research-id="' + escapeHtml(String(id)) + '"'
        + ' data-research-run-id="' + escapeHtml(String(run.id)) + '">保存证据到研究记忆</button>';
    }).filter(Boolean);
    return entries.length ? '<div class="research-memory-actions">' + entries.join('') + '</div>' : '';
  }

  function renderDecisionAction(run, evidenceIds) {
    if (!run || !run.id || !Array.isArray(evidenceIds) || !evidenceIds.length) return '';
    return '<button type="button" class="chat-run-action" data-research-action="save-decision"'
      + ' data-research-run-id="' + escapeHtml(String(run.id)) + '">保存研究决策</button>';
  }

  function renderMemoryEntry(entry) {
    if (!entry || typeof entry !== 'object') return '';
    var kind = { fact: '事实', artifact: '证据', decision: '决策' }[String(entry.kind || '')] || '研究记忆';
    return '<div class="research-memory-entry" role="status"><span>已保存' + escapeHtml(kind)
      + '到研究记忆</span><button type="button" class="chat-run-action" data-research-action="revoke-memory"'
      + ' data-research-memory-id="' + escapeHtml(String(entry.id || '')) + '">撤销记忆</button></div>';
  }

  function memoryEntryText(entry) {
    var payload = (entry && typeof entry.payload === 'object' && entry.payload) ? entry.payload : {};
    if (entry.kind === 'decision') return String(payload.text || '');
    if (entry.kind === 'fact') {
      return [payload.metric, payload.value, payload.unit].filter(function (value) {
        return value !== undefined && value !== null && value !== '';
      }).join(' ');
    }
    return String(payload.title || payload.url || payload.pdf_filename || '');
  }

  function renderMemoryRow(entry) {
    var kind = { fact: '事实', artifact: '证据', decision: '决策' }[String(entry.kind || '')] || '研究记忆';
    var id = String(entry.id || '');
    return '<div class="research-memory-row" data-research-memory-id="' + escapeHtml(id) + '">'
      + '<span class="research-memory-kind">' + escapeHtml(kind) + '</span>'
      + '<span class="research-memory-text">' + escapeHtml(memoryEntryText(entry).trim() || '未记录内容') + '</span>'
      + '<span class="research-memory-time">' + escapeHtml(String(entry.created_at || '未记录时间')) + '</span>'
      + '<button type="button" class="chat-run-action" data-research-action="revoke-memory"'
      + ' data-research-memory-id="' + escapeHtml(id) + '">撤销记忆</button></div>';
  }

  /* 持久化记忆列表：按归属会话分组展示，并允许逐条撤销。

  没有记录归属的旧条目在此 fail-closed 不渲染：归属无法推断，列表中不得猜测；
  归属会话已不存在的条目单独成组，仍可按记录 id 撤销。
  */
  function renderMemoryList(entries, sessionTitles) {
    var items = Array.isArray(entries) ? entries : [];
    var titles = (sessionTitles && typeof sessionTitles === 'object') ? sessionTitles : {};
    var order = [];
    var groups = {};
    items.forEach(function (entry) {
      if (!entry || typeof entry !== 'object' || !entry.id) return;
      var owner = String(entry.owner_session_id || '');
      if (!owner) return;
      if (!groups[owner]) {
        groups[owner] = {
          label: Object.prototype.hasOwnProperty.call(titles, owner) && titles[owner]
            ? String(titles[owner]) : '来自已删除会话',
          entries: [],
        };
        order.push(owner);
      }
      groups[owner].entries.push(entry);
    });
    return order.map(function (owner) {
      var group = groups[owner];
      return '<section class="research-memory-group" data-research-memory-owner="' + escapeHtml(owner) + '">'
        + '<h4>' + escapeHtml(group.label) + '</h4>'
        + '<div class="research-memory-group-entries">'
        + group.entries.map(renderMemoryRow).join('')
        + '</div></section>';
    }).join('');
  }

  function renderExportState(status) {
    var label = { completed: '导出完成', partial: '部分完成：导出保留范围、冲突和验证状态',
      stopped: '已停止：导出保留未完成状态', failed: '导出失败' }[String(status || '')] || '正在准备导出';
    return '<div class="research-export-state" role="status">' + escapeHtml(label) + '</div>';
  }

  function renderDeleteResult(result) {
    var count = result && Number.isInteger(result.retained_memory_count) ? result.retained_memory_count : 0;
    return '<div class="research-delete-result" role="status">会话已删除；' + count
      + ' 条研究记忆仍被保留，原始 PDF 不会被删除。</div>';
  }

  /* 复用既有 run/session 身份的追加动作：只回填该轮原始问题，不复制证据、
  不新增特权端点，也不改写或删除历史轮次。 */
  function renderRunReuseActions(run) {
    if (!run || typeof run !== 'object' || !run.id) return '';
    return '<div class="chat-run-reuse">'
      + '<button type="button" class="chat-run-action" data-chat-action="edit-reask">编辑重问</button>'
      + '<button type="button" class="chat-run-action" data-chat-action="branch-followup"'
      + ' data-chat-run-id="' + escapeHtml(String(run.id)) + '">分支追问</button></div>';
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
    renderWorkspaceItem: renderWorkspaceItem,
    renderFactActions: renderFactActions,
    renderResearchQuality: renderResearchQuality,
    renderArtifactActions: renderArtifactActions,
    renderDecisionAction: renderDecisionAction,
    renderMemoryEntry: renderMemoryEntry,
    renderMemoryList: renderMemoryList,
    renderRunReuseActions: renderRunReuseActions,
    renderExportState: renderExportState,
    renderDeleteResult: renderDeleteResult,
    renderRunArtifacts: renderRunArtifacts,
    renderPolicy: renderPolicy,
    renderFactsAndConflicts: renderFactsAndConflicts,
    renderVerification: renderVerification,
    renderRunStatus: renderRunStatus,
    renderResearchPlan: renderResearchPlan,
    renderResearchSteps: renderResearchSteps,
    renderResearchRecovery: renderResearchRecovery,
    supplementSummaryView: supplementSummaryView,
  };
}));
