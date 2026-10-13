/* Accessible Chart.js line charts for chat market answers. */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.ChatMarketCharts = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var PALETTE = ['#2563eb', '#d97706', '#16a34a', '#7c3aed'];
  var LINE_DASHES = [[], [6, 3], [2, 3], [8, 3, 2, 3]];
  var POINT_STYLES = ['circle', 'rect', 'triangle', 'crossRot'];
  var charts = new Map();

  function formatPercent(value) {
    var number = Number(value);
    return Number.isFinite(number) ? (number > 0 ? '+' : '') + number.toFixed(2) + '%' : '—';
  }

  function formatClose(value) {
    var number = Number(value);
    return Number.isFinite(number)
      ? number.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '—';
  }

  function chartConfig(data) {
    var periods = Array.isArray(data && data.periods) ? data.periods : [];
    var series = Array.isArray(data && data.series) ? data.series : [];
    var stockPrice = data && data.mode === 'stock_price';
    var unit = String(data && data.unit || '元/股');
    return {
      type: 'line',
      data: {
        labels: periods,
        datasets: series.map(function (item, index) {
          var color = PALETTE[index % PALETTE.length];
          return {
            label: String(item.label || item.code || (stockPrice ? '个股' : '指数')),
            data: stockPrice
              ? (Array.isArray(item.closeValues) ? item.closeValues : [])
              : (Array.isArray(item.changePctValues) ? item.changePctValues : []),
            rawValues: Array.isArray(item.closeValues) ? item.closeValues : [],
            changePctValues: Array.isArray(item.changePctValues) ? item.changePctValues : [],
            borderColor: color,
            backgroundColor: color,
            borderWidth: 2,
            borderDash: LINE_DASHES[index % LINE_DASHES.length],
            pointStyle: POINT_STYLES[index % POINT_STYLES.length],
            pointRadius: 2,
            pointHoverRadius: 4,
            tension: 0,
            fill: false,
          };
        }),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: function (items) {
                return items && items.length ? String(items[0].label || '') : '';
              },
              label: function (context) {
                var dataset = context && context.dataset || {};
                var index = Number(context && context.dataIndex);
                var close = Array.isArray(dataset.rawValues) ? dataset.rawValues[index] : null;
                var changes = Array.isArray(dataset.changePctValues) ? dataset.changePctValues : [];
                var change = changes[index];
                return String(dataset.label || (stockPrice ? '个股' : '指数')) + '：' + formatClose(close)
                  + (stockPrice ? ' ' + unit : ' 点') + '（'
                  + (stockPrice ? '较首个采样点 ' : '区间 ') + formatPercent(change) + '）';
              },
            },
          },
        },
        scales: {
          x: {
            title: { display: true, text: '日期' },
            ticks: { maxTicksLimit: 7, maxRotation: 0, autoSkip: true },
          },
          y: {
            title: { display: true, text: stockPrice ? '收盘价（' + unit + '）' : '区间涨跌幅（%）' },
            ticks: { callback: stockPrice ? formatClose : formatPercent },
          },
        },
      },
    };
  }

  function showFallback(container) {
    if (!container || typeof container.querySelector !== 'function') return;
    var canvas = container.querySelector('canvas[data-chat-market-chart]');
    if (canvas) canvas.hidden = true;
    var plot = container.querySelector('.chat-market-chart-plot');
    if (plot && plot.classList && typeof plot.classList.add === 'function') {
      plot.classList.add('chat-market-chart-plot-fallback');
    }
    var message = container.querySelector('.chat-market-chart-fallback');
    if (!message) return;
    message.hidden = false;
    message.textContent = '图表组件不可用，原始点位表仍可查看。';
  }

  function destroy(container) {
    var chart = charts.get(container);
    if (!chart) return;
    charts.delete(container);
    if (typeof chart.destroy === 'function') chart.destroy();
  }

  function destroyAll() {
    charts.forEach(function (chart) {
      if (!chart || typeof chart.destroy !== 'function') return;
      try { chart.destroy(); } catch (_) { /* continue cleaning other chart instances */ }
    });
    charts.clear();
  }

  function mount(container, run) {
    if (!container || typeof container.querySelector !== 'function') return null;
    destroy(container);
    var canvas = container.querySelector('canvas[data-chat-market-chart]');
    if (!canvas) return null;
    canvas.hidden = false;
    var plot = container.querySelector('.chat-market-chart-plot');
    if (plot && plot.classList && typeof plot.classList.remove === 'function') {
      plot.classList.remove('chat-market-chart-plot-fallback');
    }
    var message = container.querySelector('.chat-market-chart-fallback');
    if (message) { message.hidden = true; message.textContent = ''; }
    var rendering = root && root.ChatRendering;
    var data = rendering && typeof rendering.marketChartData === 'function'
      ? rendering.marketChartData(run) : null;
    if (!data) return null;
    var ChartClass = root && root.Chart;
    if (!ChartClass || root.__chartFailed) {
      showFallback(container);
      return null;
    }
    try {
      var context = typeof canvas.getContext === 'function' ? canvas.getContext('2d') : canvas;
      var chart = new ChartClass(context || canvas, chartConfig(data));
      charts.set(container, chart);
      return chart;
    } catch (_) {
      showFallback(container);
      return null;
    }
  }

  return { chartConfig: chartConfig, destroy: destroy, destroyAll: destroyAll, mount: mount };
}));
