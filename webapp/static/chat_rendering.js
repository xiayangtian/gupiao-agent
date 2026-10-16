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
  var SUPPLEMENT_FAILURE_REASONS = { download_failed: '下载未成功', ingest_failed: '报告处理未成功' };

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
      headline = status === 'proposed' ? '请选择要补充的财报' : '正在补充财报原文';
    } else if (status === 'completed') {
      headline = ingestedCount
        ? '已按你的确认补充 ' + ingestedCount + ' 份财报'
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

  function renderScope(scope, run) {
    if (!scope || typeof scope !== 'object') return '';
    run = (run && typeof run === 'object') ? run : {};
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
      if (industryName) parts.push(escapeHtml(industryName));
      if (periodText) parts.push(escapeHtml(periodText));
      head = parts.join(' · ');
      // 本地可检索同业样本：M1 只做本地已索引同业报告的对照，绝不承诺全市场排名。
      note = (industry && industry.sample_kind === 'local_indexed')
        ? '仅包含本地已收录的同业报告'
        : '范围已扩展至同业公司';
    } else if (mode === 'company_only') {
      var onlyParts = [];
      if (company) onlyParts.push(company);
      onlyParts.push('仅分析这家公司');
      if (periodText) onlyParts.push(escapeHtml(periodText));
      head = onlyParts.join(' · ');
      if (scope.fallback_reason) note = escapeHtml(scope.fallback_reason);
    } else if (mode === 'whole_corpus') {
      var sourceSummary = run.source_summary || {};
      var planSteps = ((run.execution_plan || {}).steps || []);
      var usedLocalPdf = sourceSummary.local_pdf !== '未使用'
        && planSteps.some(function(step) { return step && step.kind === 'retrieve'; });
      head = usedLocalPdf ? '已查询本地财报库' : '未查询本地财报';
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
      entries.push({ url: url, title: String(artifact.title || url), fetchedAt: String(artifact.fetched_at || '') });
    });
    return entries;
  }

  function compactToolEntries(tools) {
    var byKey = {};
    var entries = [];
    tools.forEach(function (tool) {
      if (!tool || typeof tool !== 'object') return;
      var provider = String(tool.provider || '');
      var toolName = String(tool.tool_name || '');
      var asOf = String(tool.as_of || '');
      var fetchedAt = String(tool.fetched_at || '');
      var sourceId = String(tool.source_id || '');
      var status = String(tool.status || '');
      var searchQuery = '';
      if ((provider === 'web' || provider === 'web_search') && toolName === 'web_search') {
        try {
          var args = JSON.parse(String(tool.arguments_summary || '{}'));
          if (args && typeof args.query === 'string') searchQuery = args.query.slice(0, 300);
        } catch (_) { /* malformed historical summaries remain hidden */ }
      }
      if (!provider && !toolName) return;
      // 搜索词不同的调用必须保留为独立来源组；其余字段仍按既有来源身份归组。
      var key = JSON.stringify([provider, toolName, asOf, status, searchQuery]);
      var entry = byKey[key];
      if (!entry) {
        entry = { provider: provider || '外部数据', toolName: toolName, asOf: asOf,
          fetchedAt: [], sourceIds: [], status: status, searchQuery: searchQuery, facts: [] };
        byKey[key] = entry;
        entries.push(entry);
      }
      if (sourceId && entry.sourceIds.indexOf(sourceId) === -1) entry.sourceIds.push(sourceId);
      if (fetchedAt && entry.fetchedAt.indexOf(fetchedAt) === -1) entry.fetchedAt.push(fetchedAt);
    });
    return entries;
  }

  function isExternalFact(fact) {
    if (!fact || typeof fact !== 'object') return false;
    return fact.verification === 'reference'
      || fact.source_type === 'tool' || fact.source_type === 'web'
      || ['market', 'mcp', 'web'].indexOf(String(fact.source_category || '')) !== -1;
  }

  function factMatchesToolGroup(fact, group) {
    var evidenceIds = Array.isArray(fact.evidence_ids) ? fact.evidence_ids.map(String) : [];
    var matchedByCall = group.sourceIds.some(function (sourceId) {
      return evidenceIds.some(function (id) { return id === sourceId || id.indexOf(sourceId + ':') === 0; });
    });
    if (matchedByCall) return true;
    var toolEvidenceId = 'tool:' + group.provider + ':' + group.toolName;
    return evidenceIds.indexOf(toolEvidenceId) !== -1 && String(fact.as_of || '') === group.asOf;
  }

  function sourceProviderLabel(provider) {
    return ({ tencent: '腾讯行情', akshare: 'A 股行情快照', market: '行情数据', 'stock-data-mcp': '股票数据服务', mcp: '数据服务', web: '网页来源', web_search: '网页来源' })[provider]
      || '外部数据来源';
  }

  function sourceToolLabel(toolName) {
    return ({
      kline: '历史行情', quote: '实时行情', price: '行情数据', volume: '成交量数据',
      market_overview: 'A 股市场概况', market_indices: 'A 股指数行情',
      market_quote: '个股行情', market_breadth: '市场涨跌数据',
      sector_performance: '行业板块表现', market_fund_flow: '市场资金流向',
      get_realtime_data: '实时行情', get_realtime_quote: '实时行情',
      get_financial_metrics: '财务指标',
      get_stock_basic_info: '公司基本信息', get_stock_a_code_name: '股票代码与名称',
      stock_sector_fund_flow_rank: '行业资金流向', stock_zt_pool: '涨停数据',
      a_share_market_snapshot: '全市场成交与涨跌统计',
      web_search: '网页资料',
    })[toolName] || (toolName ? '相关数据' : '来源记录');
  }

  function chatToolActivity(toolName, phase) {
    var label = sourceToolLabel(toolName);
    var separator = /^[A-Za-z]/.test(label) ? ' ' : '';
    if (phase === 'started') return '正在查询' + separator + label + '…';
    if (phase === 'failed') return '暂时无法获取' + separator + label;
    return '已获取' + separator + label;
  }

  var EXECUTION_STEP_LABELS = {
    retrieve: '本地财报', market_quote: '个股行情', market_indices: 'A 股指数行情',
    market_breadth: '市场涨跌数据', sector_performance: '行业板块表现',
    market_fund_flow: '市场资金流向', market_overview: 'A 股市场概况',
    web_search: '网页资料', answer: '综合分析'
  };

  function executionStepMessage(kind, phase) {
    if (kind === 'answer') {
      if (phase === 'started') return '正在整理回答…';
      if (phase === 'failed') return '回答暂未完成';
      return '回答已整理';
    }
    var label = EXECUTION_STEP_LABELS[kind] || '相关信息';
    var separator = /^A /.test(label) ? ' ' : '';
    if (phase === 'started') return '正在查询' + separator + label + '…';
    if (phase === 'failed') return '暂时无法获取' + separator + label;
    if (phase === 'completed') return '已获取' + separator + label;
    return label;
  }

  function sourceStatusLabel(status) {
    return ({ success: '获取成功', partial: '部分获取', failed: '获取失败', unavailable: '暂不可用' })[status] || '状态未说明';
  }

  function factObjectLabel(fact, run) {
    var code = String(fact.company_code || '');
    var companies = run && run.scope && Array.isArray(run.scope.companies) ? run.scope.companies : [];
    var company = companies.filter(function (item) { return item && String(item.code || '') === code; })[0];
    if (company) return String(company.name || code) + '（' + code + '）';
    if (code === 'ALL_A') return '全 A 市场';
    var indices = { sh000001: '上证指数', sz399001: '深证成指', sz399006: '创业板指', sh000688: '科创50指数' };
    return (indices[code] ? indices[code] + '（' + code + '）' : code) || '未记录对象';
  }

  function factMetricLabel(metric) {
    return ({ price: '股价', index_close: '指数点位', index_close_delta: '指数涨跌点数',
      index_close_pct_change: '指数涨跌幅', volume: '成交量', amount: '成交额',
      market_advancing_count: '上涨家数', market_declining_count: '下跌家数',
      market_unchanged_count: '平盘家数', market_limit_up_count: '涨停家数',
      market_limit_down_count: '跌停家数', market_volume_ratio: '全 A 加权量比',
      market_total_turnover: '全 A 成交额', market_total_volume: '全 A 成交量',
      market_security_sample_count: '全 A 快照证券数',
      market_classified_security_count: '已分类证券数',
      market_volume_sample_count: '成交量样本数',
      market_turnover_sample_count: '成交额样本数',
      market_volume_ratio_sample_count: '量比有效样本数',
      market_volume_ratio_active_count: '有成交股票数',
      market_volume_ratio_coverage: '量比样本覆盖率',
      revenue: '营业收入', net_profit: '净利润', roe: '净资产收益率', pct_change: '涨跌幅' })[metric] || metric || '未记录指标';
  }

  function factPeriodLabel(fact) {
    var period = String(fact.period || '');
    var kind = ({ daily: '日线', weekly: '周线', monthly: '月线', point_in_time: '时点' })[String(fact.period_kind || '')] || '';
    return [period, kind].filter(Boolean).join(' · ') || String(fact.as_of || '未记录期间');
  }

  function formatFactValue(value) {
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value.toLocaleString('zh-CN', { maximumSignificantDigits: 15 });
    }
    return String(value == null ? '' : value);
  }

  function displayFactUnit(unit) {
    var value = String(unit || '');
    return value === '百分比' ? '%' : value;
  }

  function renderFactTable(facts, run) {
    if (!facts.length) return '<p class="chat-source-empty">当前来源没有可展示的数据项。</p>';
    function rows(items) {
      return items.map(function (fact) {
        var value = formatFactValue(fact.value);
        var unit = displayFactUnit(fact.unit);
        return '<tr class="chat-source-fact-row">'
          + '<td><span class="chat-source-cell-label">对象</span>' + escapeHtml(factObjectLabel(fact, run)) + '</td>'
          + '<td><span class="chat-source-cell-label">指标</span>' + escapeHtml(factMetricLabel(String(fact.metric || ''))) + '</td>'
          + '<td><span class="chat-source-cell-label">期间</span>' + escapeHtml(factPeriodLabel(fact)) + '</td>'
          + '<td class="chat-source-value"><span class="chat-source-cell-label">数值</span>'
          + escapeHtml(value) + (unit ? ' ' + escapeHtml(unit) : '') + '</td></tr>';
      }).join('');
    }
    function table(items) {
      return '<div class="chat-source-table-wrap"><table class="chat-source-table">'
        + '<thead><tr><th scope="col">对象</th><th scope="col">指标</th><th scope="col">期间</th><th scope="col">数值</th></tr></thead>'
        + '<tbody>' + rows(items) + '</tbody></table></div>';
    }
    var first = facts.slice(0, 20);
    var remaining = facts.slice(20);
    return table(first) + (remaining.length
      ? '<details class="chat-source-more"><summary>查看更多 ' + remaining.length + ' 条</summary>' + table(remaining) + '</details>'
      : '');
  }

  function marketIndexChartData(run) {
    if (!run || typeof run !== 'object') return null;
    var plan = run.execution_plan && typeof run.execution_plan === 'object' ? run.execution_plan : {};
    if (String(plan.source_mode || '') !== 'market_recap') return null;

    var labels = {
      sh000001: '上证指数', sz399001: '深证成指',
      sz399006: '创业板指', sh000688: '科创50指数'
    };
    var orderedCodes = ['sh000001', 'sz399001', 'sz399006', 'sh000688'];
    var groupsByCode = {};
    (Array.isArray(run.facts) ? run.facts : []).forEach(function (fact) {
      if (!fact || fact.source_category !== 'market' || fact.metric !== 'index_close'
        || fact.unit !== '点' || fact.source_type !== 'tool' || fact.verification !== 'reference'
        || fact.entity_scope !== 'index' || !labels[String(fact.company_code || '')]
        || typeof fact.value !== 'number' || !Number.isFinite(fact.value) || fact.value <= 0
        || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(String(fact.period || ''))
        || !['daily', 'weekly', 'monthly'].includes(String(fact.period_kind || ''))) return;
      var periodDate = new Date(String(fact.period) + 'T00:00:00Z');
      if (!Number.isFinite(periodDate.getTime()) || periodDate.toISOString().slice(0, 10) !== fact.period) return;
      var code = String(fact.company_code);
      var group = groupsByCode[code];
      if (!group) {
        group = { code: code, periodKind: String(fact.period_kind), values: {}, conflict: false, mixedPeriod: false };
        groupsByCode[code] = group;
      }
      if (group.periodKind !== String(fact.period_kind)) group.mixedPeriod = true;
      var period = String(fact.period);
      if (Object.prototype.hasOwnProperty.call(group.values, period)) {
        if (group.values[period] !== fact.value) group.conflict = true;
      } else {
        group.values[period] = fact.value;
      }
    });

    var groups = orderedCodes.map(function (code) { return groupsByCode[code]; })
      .filter(function (group) { return group && !group.conflict && !group.mixedPeriod; });
    if (groups.length < 2) return null;
    var periodKinds = {};
    groups.forEach(function (group) { periodKinds[group.periodKind] = true; });
    if (Object.keys(periodKinds).length !== 1) return null;

    var periods = Object.keys(groups[0].values).filter(function (period) {
      return groups.every(function (group) {
        return Object.prototype.hasOwnProperty.call(group.values, period);
      });
    }).sort();
    if (periods.length < 2) return null;

    return {
      periods: periods,
      periodKind: groups[0].periodKind,
      series: groups.map(function (group) {
        var closeValues = periods.map(function (period) { return group.values[period]; });
        var baseline = closeValues[0];
        return {
          code: group.code,
          label: labels[group.code],
          closeValues: closeValues,
          changePctValues: closeValues.map(function (value) { return (value / baseline - 1) * 100; })
        };
      })
    };
  }

  function marketChartData(run) {
    var indexData = marketIndexChartData(run);
    if (indexData) return Object.assign({ mode: 'index_change' }, indexData);
    if (!run || typeof run !== 'object') return null;
    var plan = run.execution_plan && typeof run.execution_plan === 'object' ? run.execution_plan : {};
    if (String(plan.source_mode || '') !== 'external_market') return null;

    var scopedCompanies = run.scope && Array.isArray(run.scope.companies) ? run.scope.companies : [];
    var scopedCodes = scopedCompanies.map(function (item) { return item && String(item.code || ''); }).filter(Boolean);
    var candidates = (Array.isArray(run.facts) ? run.facts : []).filter(function (fact) {
      return fact && (!scopedCodes.length || scopedCodes.indexOf(String(fact.company_code || '')) !== -1)
        && fact.metric === 'price' && fact.source_category === 'market'
        && fact.source_type === 'tool' && fact.verification === 'reference'
        && fact.entity_scope === 'security' && typeof fact.company_code === 'string'
        && fact.company_code && typeof fact.value === 'number' && Number.isFinite(fact.value)
        && fact.value > 0 && typeof fact.unit === 'string' && fact.unit
        && ['daily', 'weekly', 'monthly'].indexOf(String(fact.period_kind || '')) !== -1;
    });
    var byCode = {};
    candidates.forEach(function (fact) {
      var date = String(fact.period || '');
      if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(date)) return;
      var parsed = new Date(date + 'T00:00:00Z');
      if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== date) return;
      var group = byCode[fact.company_code];
      if (!group) {
        group = { code: fact.company_code, unit: fact.unit, periodKind: fact.period_kind, values: {}, invalid: false };
        byCode[fact.company_code] = group;
      }
      if (group.unit !== fact.unit || group.periodKind !== fact.period_kind) group.invalid = true;
      if (Object.prototype.hasOwnProperty.call(group.values, date)) {
        if (group.values[date] !== fact.value) group.invalid = true;
      } else {
        group.values[date] = fact.value;
      }
    });
    var codes = Object.keys(byCode);
    if (codes.length !== 1) return null;
    var group = byCode[codes[0]];
    var periods = Object.keys(group.values).sort();
    if (group.invalid || periods.length < 2) return null;
    var closeValues = periods.map(function (period) { return group.values[period]; });
    var baseline = closeValues[0];
    var companies = run.scope && Array.isArray(run.scope.companies) ? run.scope.companies : [];
    var company = companies.filter(function (item) { return item && String(item.code || '') === group.code; })[0];
    var label = company ? String(company.name || group.code) : group.code;
    return {
      mode: 'stock_price', periods: periods, periodKind: group.periodKind, unit: group.unit,
      series: [{ code: group.code, label: label, closeValues: closeValues,
        changePctValues: closeValues.map(function (value) { return (value / baseline - 1) * 100; }) }]
    };
  }

  function renderMarketIndexChart(data) {
    if (!data || !Array.isArray(data.periods) || !Array.isArray(data.series)) return '';
    var baseline = String(data.periods[0] || '');
    var stockPrice = data.mode === 'stock_price';
    var title = stockPrice ? '个股收盘价走势' : '多指数区间走势';
    var names = data.series.map(function (series) { return String(series.label || series.code || (stockPrice ? '个股' : '指数')); });
    var legend = data.series.map(function (series, index) {
      return '<li><span class="chat-market-chart-swatch chat-market-chart-swatch-' + (index % 4)
        + '" aria-hidden="true"></span><span>' + escapeHtml(series.label || series.code || (stockPrice ? '个股' : '指数')) + '</span></li>';
    }).join('');
    var chartLabel = stockPrice ? names.join('、') + '收盘价折线图' : '多指数区间涨跌幅折线图，包含：' + names.join('、');
    var description = stockPrice
      ? '纵轴为收盘价（' + String(data.unit || '') + '）；悬停可查看原始价格与相对首个采样点的涨跌幅。'
      : '以 ' + baseline + ' 为共同基准点（0%）；悬停可查看原始收盘点位与区间涨跌，下方表格保留收盘点位。';
    return '<figure class="chat-market-chart" aria-label="' + escapeHtml(title) + '">'
      + '<figcaption><h4>' + escapeHtml(title) + '</h4><p>' + escapeHtml(description) + '</p></figcaption>'
      + '<div class="chat-market-chart-plot"><canvas data-chat-market-chart role="img" aria-label="'
      + escapeHtml(chartLabel) + '"></canvas><p class="chat-market-chart-fallback" role="status"'
      + ' aria-live="polite" hidden></p></div>'
      + '<ul class="chat-market-chart-legend" aria-label="' + (stockPrice ? '个股图例' : '指数图例') + '">' + legend + '</ul></figure>';
  }

  function renderInlineMarketFacts(run) {
    if (!run || typeof run !== 'object') return '';
    var plan = run.execution_plan && typeof run.execution_plan === 'object' ? run.execution_plan : {};
    var intent = run.intent_decision && String(run.intent_decision.intent || '');
    var marketMode = ['external_market', 'market_recap'].indexOf(String(plan.source_mode || '')) !== -1;
    if (!marketMode && intent !== 'realtime_market') return '';
    var dailyRecap = plan.source_mode === 'market_recap'
      && plan.market_window && String(plan.market_window.label || '') === '今日';
    var summaryLabels = {
      market_advancing_count: '上涨家数', market_declining_count: '下跌家数',
      market_unchanged_count: '平盘家数', market_limit_up_count: '涨停家数',
      market_limit_down_count: '跌停家数', market_total_turnover: '全 A 成交额',
      market_total_volume: '全 A 成交量', market_volume_ratio: '全 A 加权量比',
      market_security_sample_count: '全 A 快照证券数',
      market_classified_security_count: '已分类证券数',
      market_volume_sample_count: '成交量样本数',
      market_turnover_sample_count: '成交额样本数',
      market_volume_ratio_sample_count: '量比有效样本数',
      market_volume_ratio_active_count: '有成交股票数',
      market_volume_ratio_coverage: '量比样本覆盖率'
    };

    var groupsByKey = {};
    var groups = [];
    (Array.isArray(run.facts) ? run.facts : []).forEach(function (fact) {
      if (!fact || fact.source_category !== 'market'
        || ['price', 'index_close'].indexOf(String(fact.metric || '')) === -1
        || typeof fact.value !== 'number' || !Number.isFinite(fact.value)
        || !fact.company_code || !fact.period) return;
      var key = JSON.stringify([String(fact.company_code), String(fact.metric), String(fact.unit || '')]);
      var group = groupsByKey[key];
      if (!group) {
        group = { code: String(fact.company_code), metric: String(fact.metric), unit: String(fact.unit || ''),
          periods: {}, conflict: false };
        groupsByKey[key] = group;
        groups.push(group);
      }
      var period = String(fact.period);
      if (Object.prototype.hasOwnProperty.call(group.periods, period)) {
        if (group.periods[period] !== fact.value) group.conflict = true;
      } else {
        group.periods[period] = fact.value;
      }
    });

    groups = groups.map(function (group) {
      group.rows = Object.keys(group.periods).sort().map(function (period) {
        return { period: period, value: group.periods[period] };
      });
      return group;
    }).filter(function (group) {
      return !group.conflict && (group.rows.length >= 2 || (marketMode && group.metric === 'index_close'));
    });

    if (dailyRecap) {
      var metricFacts = {};
      var metricConflicts = {};
      (Array.isArray(run.facts) ? run.facts : []).forEach(function (fact) {
        if (!fact || fact.source_category !== 'market' || fact.company_code !== 'ALL_A'
          || !summaryLabels[fact.metric] || typeof fact.value !== 'number' || !Number.isFinite(fact.value)) return;
        if (Object.prototype.hasOwnProperty.call(metricFacts, fact.metric)
          && metricFacts[fact.metric].value !== fact.value) metricConflicts[fact.metric] = true;
        else metricFacts[fact.metric] = fact;
      });
      Object.keys(metricConflicts).forEach(function (metric) { delete metricFacts[metric]; });
      var indexRows = groups.filter(function (group) { return group.metric === 'index_close'; });
      var marketRows = Object.keys(summaryLabels).filter(function (metric) { return metricFacts[metric]; }).map(function (metric) {
        var fact = metricFacts[metric];
        return '<tr><td>' + escapeHtml(summaryLabels[metric]) + '</td><td class="chat-inline-market-value">'
          + escapeHtml(formatFactValue(fact.value)) + (fact.unit ? ' ' + escapeHtml(displayFactUnit(fact.unit)) : '')
          + '</td><td>—</td></tr>';
      });
      var indexHtmlRows = indexRows.map(function (group) {
        var current = group.rows[group.rows.length - 1];
        var previous = group.rows.length > 1 ? group.rows[group.rows.length - 2] : null;
        var value = escapeHtml(current.period + ' · ' + formatFactValue(current.value) + (group.unit ? ' ' + group.unit : ''));
        var change = '—';
        if (previous && previous.value !== 0) {
          var delta = Math.round((current.value - previous.value) * 100) / 100;
          var pointChange = (delta > 0 ? '+' : '') + formatFactValue(delta) + (group.unit ? ' ' + group.unit : '');
          change = pointChange + '（' + escapeHtml(percent(current.value, previous.value)) + '）';
        }
        return '<tr><td>' + escapeHtml(factObjectLabel({company_code: group.code}, run))
          + '</td><td class="chat-inline-market-value">' + value + '</td><td>' + change + '</td></tr>';
      });
      var tableRows = indexHtmlRows.concat(marketRows);
      if (!tableRows.length) return '';
      var asOf = '';
      (Array.isArray(run.facts) ? run.facts : []).some(function (fact) {
        if (fact && fact.source_category === 'market' && fact.company_code === 'ALL_A' && fact.as_of) {
          asOf = String(fact.as_of); return true;
        }
        return false;
      });
      var note = '指数涨跌按上一交易日收盘计算；未取得或覆盖不足的市场指标不补零。涨跌停按数据源池返回统计，未提供独立总量校验。';
      if (asOf) note += ' 数据获取时间（上海）：' + asOf.replace('T', ' ').replace(/[+-]\d{2}:\d{2}$/, '').slice(0, 16) + '。';
      var snapshotArtifact = (Array.isArray(run.tool_artifacts) ? run.tool_artifacts : []).find(function (artifact) {
        return artifact && artifact.provider === 'akshare' && artifact.tool_name === 'a_share_market_snapshot';
      });
      if (snapshotArtifact && snapshotArtifact.status !== 'success') note += ' 全市场来源不完整，表中仅列出已核验数据。';
      if (snapshotArtifact && snapshotArtifact.result_summary) {
        var resultSummary = String(snapshotArtifact.result_summary);
        var coverageMatch = resultSummary.match(/快照证券 ([\d,]+)\/([\d,]+) 行；涨跌分类 ([\d,]+)\/([\d,]+) 行/);
        if (coverageMatch) note += ' 全 A 快照返回 ' + coverageMatch[1] + '/' + coverageMatch[2]
          + ' 行，涨跌分类 ' + coverageMatch[3] + '/' + coverageMatch[4] + ' 行。';
        var limitationIndex = resultSummary.indexOf('限制：');
        if (limitationIndex >= 0) note += ' 数据限制：' + resultSummary.slice(limitationIndex + 3);
      }
      var windowLabel = String(plan.market_window.label || '最近交易日');
      var indexDataDates = indexRows.map(function (group) { return group.rows[group.rows.length - 1].period; });
      var summaryDataDates = Object.keys(metricFacts).map(function (metric) { return String(metricFacts[metric].period || ''); }).filter(Boolean);
      var uniqueDataDates = Array.from(new Set(indexDataDates.concat(summaryDataDates)));
      if (uniqueDataDates.length === 1) note = '行情数据日期：' + uniqueDataDates[0] + '；' + note;
      else if (uniqueDataDates.length > 1) note = '指数/全市场统计数据日期：' + uniqueDataDates.join('、') + '；' + note;
      var heading = windowLabel === '今日' ? 'A 股行情概览' : windowLabel + ' A 股行情概览';
      return '<section class="chat-inline-market" aria-label="' + escapeHtml(heading) + '">'
        + '<h3>' + escapeHtml(heading) + '</h3>'
        + '<p class="chat-inline-market-note">' + escapeHtml(note) + '</p>'
        + '<div class="chat-inline-market-table-wrap"><table class="chat-inline-market-table" aria-label="'
        + escapeHtml(windowLabel + ' A 股市场摘要') + '"><thead><tr><th scope="col">指标</th>'
        + '<th scope="col">当日数据</th><th scope="col">较上一交易日</th></tr></thead><tbody>'
        + tableRows.join('') + '</tbody></table></div></section>';
    }
    if (!groups.length) return '';

    function percent(value, previous) {
      if (!Number.isFinite(value) || !Number.isFinite(previous) || previous === 0) return '—';
      var change = (value / previous - 1) * 100;
      return (change > 0 ? '+' : '') + change.toFixed(2) + '%';
    }
    function table(group, items, offset) {
      var body = items.map(function (row, index) {
        var absoluteIndex = offset + index;
        var previous = absoluteIndex > 0 ? group.rows[absoluteIndex - 1].value : null;
        return '<tr><td>' + escapeHtml(row.period) + '</td><td class="chat-inline-market-value">'
          + escapeHtml(formatFactValue(row.value)) + (group.unit ? ' ' + escapeHtml(group.unit) : '')
          + '</td><td>' + (previous == null ? '—' : escapeHtml(percent(row.value, previous))) + '</td></tr>';
      }).join('');
      var closeLabel = group.metric === 'index_close' ? '收盘点位' : '收盘价';
      return '<div class="chat-inline-market-table-wrap"><table class="chat-inline-market-table" aria-label="'
        + escapeHtml(factObjectLabel({company_code: group.code}, run) + '行情采样') + '">'
        + '<thead><tr><th scope="col">日期</th><th scope="col">' + closeLabel + '</th><th scope="col">较前一采样</th></tr></thead>'
        + '<tbody>' + body + '</tbody></table></div>';
    }

    var label = plan.market_window && String(plan.market_window.label || '') || '行情采样区间';
    var indexChartHtml = renderMarketIndexChart(marketChartData(run));
    var sections = groups.map(function (group) {
      var rows = group.rows;
      var overall = rows.length > 1 ? percent(rows[rows.length - 1].value, rows[0].value) : '—';
      var head = '<h4>' + escapeHtml(factObjectLabel({company_code: group.code}, run))
        + ' · 区间变化 ' + escapeHtml(overall) + '</h4>';
      var first = rows.slice(0, 20);
      var remaining = rows.slice(20);
      return '<section class="chat-inline-market-object">' + head + table(group, first, 0)
        + (remaining.length
          ? '<details class="chat-inline-market-more"><summary>展开剩余 ' + remaining.length + ' 条</summary>'
            + table(group, remaining, first.length) + '</details>'
          : '') + '</section>';
    }).join('');
    return '<section class="chat-inline-market" aria-label="' + escapeHtml(label + '行情数据') + '">'
      + '<h3>' + escapeHtml(label + '行情数据') + '</h3>'
      + '<p class="chat-inline-market-note">外部参考采样；涨跌幅按表内相邻收盘值计算。</p>'
      + indexChartHtml + sections + '</section>';
  }

  function renderToolSourceGroup(group, run) {
    var title = sourceProviderLabel(group.provider) + ' · ' + sourceToolLabel(group.toolName);
    var meta = ['状态：' + sourceStatusLabel(group.status)];
    if (group.asOf) meta.push('数据截至 ' + group.asOf);
    else meta.push('未提供数据时间');
    if (group.fetchedAt.length) meta.push('获取于 ' + group.fetchedAt.join('、'));
    var query = group.searchQuery
      ? '<p class="chat-source-meta">搜索词：' + escapeHtml(group.searchQuery) + '</p>' : '';
    return '<section class="chat-source-group" aria-label="外部数据来源">'
      + '<h3>' + escapeHtml(title) + '</h3><p class="chat-source-meta">' + escapeHtml(meta.join(' · ')) + '</p>'
      + query + renderFactTable(group.facts, run) + '</section>';
  }

  function renderWebSourceGroup(entry, facts, run) {
    var host = entry.url;
    try { host = new URL(entry.url).hostname || entry.url; } catch (_) {}
    return '<section class="chat-source-group" aria-label="网页来源 ' + escapeHtml(host) + '">'
      + '<h3>网页来源 · ' + escapeHtml(host) + '</h3>'
      + '<a class="chat-artifact-link chat-web-link" href="' + escapeHtml(entry.url) + '" target="_blank" rel="noopener noreferrer">'
      + escapeHtml(entry.title) + '</a>'
      + (entry.fetchedAt ? '<p class="chat-source-meta">获取于 ' + escapeHtml(entry.fetchedAt) + '</p>' : '')
      + (facts.length ? renderFactTable(facts, run) : '') + '</section>';
  }

  function renderUnmatchedSourceGroup(facts, run) {
    return '<section class="chat-source-group chat-source-unmatched" aria-label="来源未关联">'
      + '<h3>来源未关联</h3><p class="chat-source-meta">暂时无法确认这些数据对应哪个来源，因此不标注具体来源。</p>'
      + renderFactTable(facts, run) + '</section>';
  }

  function renderRunArtifacts(run) {
    if (!run || typeof run !== 'object') return '';
    var artifacts = Array.isArray(run.artifacts) ? run.artifacts : [];
    var tools = Array.isArray(run.tool_artifacts) ? run.tool_artifacts : [];
    var allFacts = Array.isArray(run.facts) ? run.facts : [];
    var externalFacts = allFacts.filter(isExternalFact);
    var pdfEntries = compactPdfEntries(artifacts);
    var webEntries = compactWebEntries(artifacts);
    var toolEntries = compactToolEntries(tools);
    var unmatchedFacts = [];

    externalFacts.forEach(function (fact) {
      var toolMatches = toolEntries.filter(function (entry) { return factMatchesToolGroup(fact, entry); });
      if (toolMatches.length === 1) {
        toolMatches[0].facts.push(fact);
        return;
      }
      var webMatches = webEntries.filter(function (entry) {
        return fact.source_category === 'web' && Array.isArray(fact.evidence_ids)
          && fact.evidence_ids.indexOf(entry.url) !== -1;
      });
      if (!toolMatches.length && webMatches.length === 1) {
        webMatches[0].facts = webMatches[0].facts || [];
        webMatches[0].facts.push(fact);
        return;
      }
      unmatchedFacts.push(fact);
    });

    var sourceGroups = [];
    toolEntries.forEach(function (entry) { sourceGroups.push(renderToolSourceGroup(entry, run)); });
    webEntries.forEach(function (entry) { sourceGroups.push(renderWebSourceGroup(entry, entry.facts || [], run)); });
    if (unmatchedFacts.length) sourceGroups.push(renderUnmatchedSourceGroup(unmatchedFacts, run));

    var links = [];
    pdfEntries.forEach(function (entry) { links.push(pdfEntryHtml(entry)); });
    if (!links.length && !sourceGroups.length) return '';
    var sourceCount = sourceGroups.length + pdfEntries.length;
    var summary = '证据与来源（' + sourceCount + '）';
    if (externalFacts.length) summary = '证据与来源（' + sourceCount + ' 个来源 · ' + externalFacts.length + ' 条外部数据）';
    return '<details class="chat-artifacts" aria-label="回答证据与来源">'
      + '<summary>' + escapeHtml(summary) + '</summary>'
      + '<div class="chat-artifacts-list">'
      + (sourceGroups.length ? '<div class="chat-source-groups">' + sourceGroups.join('') + '</div>' : '')
      + (links.length ? '<div class="chat-artifact-links">' + links.join('') + '</div>' : '')
      + '</div></details>';
  }

  function renderPolicy(policy) {
    if (!policy || typeof policy !== 'object') return '';
    var label = {
      report_fact: '本地财报资料', company_trend: '财报趋势对照',
      industry_benchmark: '本地同业样本', realtime_market: '行情数据',
      event_attribution: '公开信息参考', research_task: '分步研究与多来源核对'
    }[String(policy.intent || '')];
    return label ? '<details class="chat-policy"><summary>本次分析依据</summary><span>'
      + escapeHtml(label) + '</span></details>' : '';
  }

  function factSourceLabel(fact) {
    return fact && fact.source_type === 'pdf' ? 'PDF 原文' : '外部数据';
  }

  function renderFactsAndConflicts(run) {
    if (!run || typeof run !== 'object') return '';
    var parts = [];
    (Array.isArray(run.facts) ? run.facts : []).forEach(function (fact) {
      if (!fact || typeof fact !== 'object' || isExternalFact(fact)) return;
      var label = fact.source_type === 'pdf' || fact.source_category === 'local_pdf' ? 'PDF 原文' : '事实';
      var asOf = fact.as_of ? ' · 数据截至 ' + escapeHtml(fact.as_of) : '';
      parts.push('<div class="chat-fact chat-fact-' + escapeHtml(fact.verification || '') + '">'
        + escapeHtml(label + ' · ' + factMetricLabel(String(fact.metric || '')) + '：' + String(fact.value == null ? '' : fact.value) + ' ' + displayFactUnit(fact.unit)) + asOf + '</div>');
    });
    (Array.isArray(run.conflicts) ? run.conflicts : []).forEach(function (conflict) {
      if (!conflict || typeof conflict !== 'object') return;
      var labels = (Array.isArray(conflict.facts) ? conflict.facts : []).map(factSourceLabel).filter(function (value, index, all) { return all.indexOf(value) === index; });
      parts.push('<div class="chat-conflict" role="status"><strong>数据口径或时间不一致</strong>：'
        + escapeHtml(String(conflict.reason || '不同来源的数据不一致')) + '（' + escapeHtml(labels.join('、')) + '）</div>');
    });
    return parts.length ? '<div class="chat-facts-conflicts">' + parts.join('') + '</div>' : '';
  }

  function renderVerification(report) {
    if (!report || typeof report !== 'object' || report.status === 'passed') return '';
    var label = report.status === 'blocked'
      ? '没有找到可核实的公开披露，暂时无法确认这个数值。'
      : '部分数据来自外部来源，或统计口径/时间不一致；请查看对应来源。';
    return '<div class="chat-verification chat-verification-' + escapeHtml(report.status) + '" role="status">' + escapeHtml(label) + '</div>';
  }

  function researchStepLabel(step) {
    return String((step && step.label) || '研究步骤');
  }

  var RESEARCH_STEP_STATUS_LABELS = {
    pending: '待开始', running: '进行中', completed: '已完成',
    stopped: '已停止', failed: '未能完成', skipped: '已跳过',
  };

  function renderResearchPlan(plan) {
    if (!plan || typeof plan !== 'object') return '';
    var steps = Array.isArray(plan.steps) ? plan.steps : [];
    var acceptance = Array.isArray(plan.acceptance) ? plan.acceptance : [];
    if (!steps.length) return '';
    return '<details class="research-plan"><summary>研究计划（' + steps.length + ' 步）</summary>'
      + '<ol>' + steps.map(function (step) { return '<li>' + escapeHtml(researchStepLabel(step)) + '</li>'; }).join('') + '</ol>'
      + '<div class="research-acceptance"><strong>完成标准</strong>：' + escapeHtml(acceptance.join('；')) + '</div></details>';
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
    var nextStep = String(summary.resume_from_step_id || '');
    var message = nextStep ? '可从「' + escapeHtml(nextStep) + '」继续' : '还有未完成的部分';
    return '<div class="research-recovery"><span>' + message + '</span><button type="button" class="chat-run-action" data-chat-action="resume-research" data-research-run-id="' + escapeHtml(String(run.research_run_id)) + '">继续研究</button></div>';
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
    return '<article class="research-workspace-item" data-research-run-id="' + escapeHtml(String(item.run_id))
      + '" data-research-session-id="' + escapeHtml(String(item.session_id)) + '">'
      + '<div class="research-workspace-item-main"><h3>' + escapeHtml(String(item.title || '未命名研究')) + '</h3>'
      + '<p class="research-workspace-scope"><strong>范围：</strong>' + escapeHtml(scope) + '</p>'
      + '<p class="research-workspace-meta"><span>状态：' + escapeHtml(String(item.status || 'unknown')) + '</span>'
      + '<span>更新时间：' + escapeHtml(String(item.updated_at || '未记录')) + '</span><span>' + escapeHtml(evidence) + '</span></p></div>'
      + '<div class="research-workspace-actions"><button type="button" class="btn" data-research-action="open">打开回答</button>'
      + '<button type="button" class="btn" data-research-action="favorite" aria-pressed="' + (favorite ? 'true' : 'false') + '">'
      + (favorite ? '取消收藏' : '收藏') + '</button>'
      + '<button type="button" class="btn" data-research-action="export" data-research-format="markdown">导出 Markdown</button>'
      + '<button type="button" class="btn" data-research-action="export" data-research-format="json">导出 JSON</button></div></article>';
  }

  // Only safe aggregate booleans are rendered.  The API deliberately omits case,
  // prompt, and failure details, and this renderer must never infer or expose them.
  function renderResearchQuality(summary) {
    var refreshCommand = '<code>' + escapeHtml(
      'python3 scripts/run_chat_evaluation.py --fixture tests/fixtures/chat_eval_cases.json --output data/research_quality_summary.json'
    ) + '</code>';
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

  function renderExportState(status) {
    var label = { completed: '导出完成', partial: '部分完成：导出保留范围、冲突和验证状态',
      stopped: '已停止：导出保留未完成状态', failed: '导出失败' }[String(status || '')] || '正在准备导出';
    return '<div class="research-export-state" role="status">' + escapeHtml(label) + '</div>';
  }

  function renderDeleteResult() {
    return '<div class="research-delete-result" role="status">会话已删除，原始 PDF 不会被删除。</div>';
  }

  /* 复用既有 run/session 身份的追加动作：只回填该轮原始问题，不复制证据、
  不新增特权端点，也不改写或删除历史轮次。 */
  function renderRunReuseActions(run) {
    if (!run || typeof run !== 'object' || !run.id) return '';
    return '<div class="chat-run-reuse">'
      + '<button type="button" class="chat-run-action" data-chat-action="edit-reask">改写后再问</button>'
      + '<button type="button" class="chat-run-action" data-chat-action="branch-followup"'
      + ' data-chat-run-id="' + escapeHtml(String(run.id)) + '">另开会话追问</button></div>';
  }

  function renderRunStatus(run) {
    if (!run || typeof run !== 'object') return '';
    if (run.legacy_evidence_unavailable) {
      return '<div class="chat-run-status chat-run-status-legacy" role="status">'
        + '<span class="chat-run-status-label">较早的回答，原始来源明细未保存</span></div>';
    }
    var status = String(run.status || '');
    var label = {
      completed: '✅ 已完成',
      partial: '⚠️ 部分完成',
      stopped: '⏹ 已停止',
      failed: '❌ 未能完成',
      waiting_consent: '⏸ 等待你确认补充报告',
    }[status];
    if (!label) return '';

    var html = '<div class="chat-run-status chat-run-status-' + escapeHtml(status)
      + '" role="status" aria-live="polite">'
      + '<span class="chat-run-status-label">' + label + '</span>';
    if (run.knowledge_basis === 'model_knowledge_unretrieved') {
      html += '<span class="chat-run-status-note">依据通用知识回答，未查询外部资料</span>';
    }
    if (status === 'stopped' || status === 'partial' || status === 'failed') {
      html += '<button type="button" class="chat-run-action chat-run-regenerate"'
        + ' data-chat-action="regenerate">重试回答</button>';
    }
    var runId = String(run.id || '');
    if (runId) {
      html += '<span class="chat-run-diagnostic">反馈编号：<code>' + escapeHtml(runId) + '</code></span>'
        + '<button type="button" class="chat-run-action chat-run-copy-id"'
        + ' data-chat-action="copy-run-id" data-chat-run-id="' + escapeHtml(runId) + '"'
        + ' aria-label="复制反馈编号" title="复制反馈编号">复制编号</button>';
    }
    return html + '</div>';
  }

  function renderExecutionSummary(run) {
    run = run || {};
    var plan = run.execution_plan || {};
    var steps = Array.isArray(plan.steps) ? plan.steps : [];
    if (!steps.length) return '';
    var text = steps.map(function (step) { return EXECUTION_STEP_LABELS[step.kind] || '相关信息'; }).join('、');
    var source = run.source_summary || {};
    var notes = [];
    if (source.local_pdf === '未使用') notes.push('未查询本地财报');
    if (plan.source_mode === 'general_web') notes.push('结合网页资料补充');
    if (plan.source_mode === 'general_knowledge') notes.push('基于通用知识回答，未查询外部资料');
    if (plan.source_mode === 'market_recap') notes.push('A 股市场复盘');
    return '<div class="chat-execution-summary" aria-label="分析依据">本次参考资料：'
      + escapeHtml(text) + (notes.length ? ' · ' + escapeHtml(notes.join('；')) : '') + '</div>';
  }

  return {
    normalizeAssistantMarkdown: normalizeAssistantMarkdown,
    webSourceReferences: webSourceReferences,
    renderScope: renderScope,
    renderExecutionSummary: renderExecutionSummary,
    chatToolActivity: chatToolActivity,
    executionStepMessage: executionStepMessage,
    renderWorkspaceItem: renderWorkspaceItem,
    renderResearchQuality: renderResearchQuality,
    renderRunReuseActions: renderRunReuseActions,
    renderExportState: renderExportState,
    renderDeleteResult: renderDeleteResult,
    renderRunArtifacts: renderRunArtifacts,
    renderInlineMarketFacts: renderInlineMarketFacts,
    marketIndexChartData: marketIndexChartData,
    marketChartData: marketChartData,
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
