(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AnalysisVisualizations = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var FLOW_TEXT = { inflow: '流入', outflow: '流出', neutral: '中性' };
  var FLOW_COLORS = { inflow: '#0f766e', outflow: '#b91c1c', neutral: '#64748b' };
  var BAR_COLOR = '#2563eb';

  function escapeMarkup(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (character) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character];
    });
  }

  function finiteRows(rows) {
    return (Array.isArray(rows) ? rows : []).filter(function (row) {
      return row && typeof row.label === 'string' && Number.isFinite(Number(row.value))
        && row.unit === '亿元';
    });
  }

  function pdfPages(ids, catalog) {
    var seen = {};
    return (Array.isArray(ids) ? ids : []).reduce(function (pages, id) {
      var record = (catalog || {})[id] || {};
      var locator = record.source_locator || {};
      var page = Number(locator.page);
      if (record.source_type === 'pdf_text' && Number.isInteger(page) && page > 0 && !seen[page]) {
        seen[page] = true;
        pages.push(page);
      }
      return pages;
    }, []);
  }

  function formatAmount(value) {
    return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(Number(value));
  }

  function evidenceHtml(row, catalog) {
    var pages = pdfPages(row.evidence_ids, catalog);
    return pages.map(function (page) {
      return '<button type="button" class="analysis-evidence-page analysis-visualization-evidence" data-evidence-page="'
        + page + '">PDF 第 ' + page + ' 页</button>';
    }).join('') || '<span class="analysis-visualization-no-evidence">—</span>';
  }

  function tableHtml(rows, catalog) {
    return '<div class="analysis-visualization-table-wrap"><table class="analysis-visualization-table">'
      + '<thead><tr><th scope="col">指标</th><th scope="col">金额（亿元）</th><th scope="col">方向</th><th scope="col">PDF 证据</th></tr></thead><tbody>'
      + rows.map(function (row) {
        var direction = FLOW_TEXT[row.direction] || FLOW_TEXT.neutral;
        return '<tr><th scope="row">' + escapeMarkup(row.label) + '</th><td class="analysis-visualization-amount">'
          + formatAmount(row.value) + '</td><td>' + direction + '</td><td>' + evidenceHtml(row, catalog) + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }

  function chartConfig(card) {
    var rows = finiteRows(card && card.rows);
    var base = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { beginAtZero: true, ticks: { callback: function (value) { return value + ' 亿'; } } } }
    };
    if (card.kind === 'balance_sheet') {
      var assets = rows.filter(function (row) { return row.metric_id === 'total_assets'; });
      var liabilities = rows.filter(function (row) { return row.metric_id === 'total_liabilities'; });
      var equity = rows.filter(function (row) { return row.metric_id === 'total_equity'; });
      return {
        type: 'bar', data: { labels: ['资产', '负债及权益'], datasets: [
          { label: (assets[0] || {}).label || '总资产', data: [assets[0] ? assets[0].value : null, null], backgroundColor: '#2563eb', stack: 'structure' },
          { label: (liabilities[0] || {}).label || '总负债', data: [null, liabilities[0] ? liabilities[0].value : null], backgroundColor: '#c2410c', stack: 'structure' },
          { label: (equity[0] || {}).label || '所有者权益', data: [null, equity[0] ? equity[0].value : null], backgroundColor: '#0f766e', stack: 'structure' }
        ] }, options: Object.assign({}, base, { plugins: { legend: { display: true } } })
      };
    }
    var colors = card.kind === 'cash_flow'
      ? rows.map(function (row) { return FLOW_COLORS[row.direction] || FLOW_COLORS.neutral; })
      : rows.map(function () { return BAR_COLOR; });
    return {
      type: 'bar', data: { labels: rows.map(function (row) { return row.label; }), datasets: [{
        label: '金额（亿元）', data: rows.map(function (row) { return row.value; }), backgroundColor: colors
      }] }, options: Object.assign({}, base, { indexAxis: 'y' })
    };
  }

  function cardHtml(card, catalog) {
    var rows = finiteRows(card.rows);
    var title = escapeMarkup(card.title || '财务结构');
    if (card.status === 'unavailable' || rows.length < 2) {
      return '<section class="analysis-visualization-card analysis-visualization-unavailable" aria-label="' + title + '">'
        + '<h3>' + title + '</h3><p>' + escapeMarkup(card.unavailable_reason || '本期可核验披露不足，暂不生成结构图。') + '</p></section>';
    }
    var partial = card.status === 'partial'
      ? '<p class="analysis-visualization-status">数据不完整，仅展示已核验披露项</p>' : '';
    return '<section class="analysis-visualization-card" aria-label="' + title + '"><h3>' + title + '</h3>' + partial
      + '<div class="analysis-visualization-chart"><canvas aria-label="' + title + '图表" role="img"></canvas></div>'
      + tableHtml(rows, catalog) + '</section>';
  }

  function legacyPrompt() {
    return '<p class="analysis-visualization-legacy-hint">重新分析后可生成结构图</p>';
  }

  function renderSlots(visualizations, sections, catalog) {
    var validSections = (Array.isArray(sections) ? sections : []).filter(function (section) {
      return section && typeof section.section_id === 'string' && section.section_id;
    });
    var cards = visualizations && visualizations.version === 1 && Array.isArray(visualizations.cards)
      ? visualizations.cards : null;
    if (!cards) {
      return validSections.map(function (section) {
        return '<div class="analysis-visualization-slot" data-visualization-topic="'
          + escapeMarkup(section.section_id) + '">' + legacyPrompt() + '</div>';
      }).join('');
    }
    return cards.filter(function (card) {
      return card && validSections.some(function (section) { return section.section_id === card.topic_id; });
    }).map(function (card) {
      return '<div class="analysis-visualization-slot" data-visualization-card-id="' + escapeMarkup(card.id)
        + '" data-visualization-topic="' + escapeMarkup(card.topic_id) + '">' + cardHtml(card, catalog) + '</div>';
    }).join('');
  }

  function mount(container, visualizations, options) {
    var charts = new Map();
    var ChartClass = typeof globalThis !== 'undefined' ? globalThis.Chart : null;
    if (!container || !container.querySelectorAll || !ChartClass || !visualizations || !Array.isArray(visualizations.cards)) return charts;
    var byId = {};
    visualizations.cards.forEach(function (card) { if (card && card.id) byId[card.id] = card; });
    container.querySelectorAll('.analysis-visualization-slot[data-visualization-card-id]').forEach(function (slot) {
      var card = byId[slot.getAttribute('data-visualization-card-id')];
      var canvas = slot.querySelector('canvas');
      if (!card || !canvas || card.status === 'unavailable') return;
      charts.set(card.id, new ChartClass(canvas, chartConfig(card)));
    });
    if (options && typeof options.onEvidencePage === 'function') {
      container.querySelectorAll('.analysis-visualization-evidence[data-evidence-page]').forEach(function (button) {
        button.addEventListener('click', function () { options.onEvidencePage(Number(button.dataset.evidencePage)); });
      });
    }
    return charts;
  }

  function destroy(charts) {
    if (!charts || typeof charts.forEach !== 'function') return;
    charts.forEach(function (chart) { if (chart && typeof chart.destroy === 'function') chart.destroy(); });
    if (typeof charts.clear === 'function') charts.clear();
  }

  return { chartConfig: chartConfig, destroy: destroy, mount: mount, renderSlots: renderSlots };
}));
