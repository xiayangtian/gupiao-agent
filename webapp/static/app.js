/* ═══════════ 财报分析工具 — 前端逻辑（原生 JS） ═══════════ */

// ═══════════════════════════════════════════════════════════════
// SECTION 0: 工具函数
// ═══════════════════════════════════════════════════════════════

const $ = (sel, ctx) => (ctx || document).querySelector(sel);
const $$ = (sel, ctx) => [...(ctx || document).querySelectorAll(sel)];

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function showError(msg) {
  const b = $('#error-banner');
  if (b) { b.textContent = `⚠ ${msg}`; b.classList.remove('hidden'); }
}

function clearError() {
  const b = $('#error-banner');
  if (b) b.classList.add('hidden');
}

// ═══════════════════════════════════════════════════════════════
// SECTION 1: 全局状态
// ═══════════════════════════════════════════════════════════════

const STATE = {
  currentPage: 'home',

  // AI / Health
  aiConfigured: false,

  // Analysis page
  pendingSearch: null,      // 首页快捷搜索 → 分析页自动查询
  selected: null,
  selectedReport: null,
  analyzingReport: null,    // { code, period } 正在分析的报告（按报告隔离，切换报告不影响）
  analysisCache: {},        // "code:period" -> { status: pending|running|done|failed, data?, error?, dims? }
  analysisDimensions: [],   // GET /api/analysis/dimensions 返回的可勾选维度
  currentReports: [],
  downloading: {},          // "code:period" -> true（财报下载中，按钮转圈）

  // History page
  historyItems: [],
  historyCollapsed: {},      // code -> true（历史记录公司分组收起）
  historySelected: null,
  historyAnalysis: null,
  historyAnalyzingReport: null,  // 历史页正在分析的报告
  historyAnalysisCache: {},      // "code:period" -> { status, data?, error? }
  chatFocusReport: null,        // {code, period, company} 历史记录跳转：聚焦该报告（提升 RAG 权重）
  historyPollId: null,

  // Chart instances
  charts: { revenue: null, ratio: null },

  // Request dedup
  reqIds: { reports: 0, analyze: 0, qa: 0, suggestions: 0 },
};

const TYPE_LABEL = { annual: '年报', semi_annual: '半年报', quarterly: '季报' };

// ═══════════════════════════════════════════════════════════════
// SECTION 2: 路由器
// ═══════════════════════════════════════════════════════════════

function initRouter() {
  window.addEventListener('hashchange', handleRoute);
  handleRoute();
}

function handleRoute() {
  const hash = window.location.hash || '#/home';
  const page = hash.replace('#/', '') || 'home';

  // Hide all pages
  $$('.page').forEach(function (p) { p.classList.add('hidden'); });

  // Show target
  var target = document.getElementById('page-' + page);
  if (target) {
    target.classList.remove('hidden');
    STATE.currentPage = page;
  }

  // Nav active
  $$('.nav-link').forEach(function (link) {
    link.classList.toggle('active', link.getAttribute('href') === hash);
  });

  // Page init
  if (page === 'home') initHomePage();
  if (page === 'analysis') initAnalysisPage();
  if (page === 'history') initHistoryPage();
  if (page === 'rag') initRagPage();
  if (page === 'chat') initChatPage();

  // Cleanup charts on leave
  destroyAllCharts();
}

// ═══════════════════════════════════════════════════════════════
// SECTION 2.1: 全局健康检查
// ═══════════════════════════════════════════════════════════════

async function loadHealth() {
  try {
    var res = await fetch('/api/health');
    var h = await res.json();
    STATE.aiConfigured = h.ai_key_configured;
    var badge = $('#ai-status');
    if (badge) {
      badge.textContent = h.ai_key_configured ? 'AI 已连接' : 'AI 未配置';
      badge.className = 'badge ' + (h.ai_key_configured ? 'badge-ok' : 'badge-warn');
    }
    return h;
  } catch (_) { return null; }
}

// ═══════════════════════════════════════════════════════════════
// SECTION 3: 首页
// ═══════════════════════════════════════════════════════════════

async function initHomePage() {
  var h = await loadHealth();
  // 本地报告 / 已完成分析
  try {
    var res = await fetch('/api/history');
    var data = await res.json();
    var items = data.items || [];
    $('#stat-reports').textContent = items.length;
    $('#stat-analyzed').textContent = items.filter(function (i) { return i.has_analysis; }).length;
  } catch (_) {
    $('#stat-reports').textContent = '—';
    $('#stat-analyzed').textContent = '—';
  }
  // RAG 知识库状态（启用时显示片段总数）
  try {
    var ragRes = await fetch('/api/rag/status');
    var rag = await ragRes.json();
    $('#stat-rag').textContent = rag.enabled ? (rag.total_chunks || 0) : '未启用';
  } catch (_) {
    $('#stat-rag').textContent = '—';
  }
  // 问答会话数
  try {
    var sessRes = await fetch('/api/chat/sessions');
    var sess = await sessRes.json();
    $('#stat-sessions').textContent = (sess.sessions || []).length;
  } catch (_) {
    $('#stat-sessions').textContent = '—';
  }
  $('#stat-ai').textContent = (h && h.ai_key_configured) ? '已连接' : '未配置';
}

// 首页快捷搜索：跳转分析页并自动查询
var homeStockInput = $('#home-stock-input');
var homeStockBtn = $('#home-stock-btn');
if (homeStockInput) {
  homeStockInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') goHomeSearch();
  });
}
if (homeStockBtn) {
  homeStockBtn.addEventListener('click', function () { goHomeSearch(); });
}
function goHomeSearch() {
  var val = homeStockInput ? homeStockInput.value.trim() : '';
  if (!val) return;
  STATE.pendingSearch = val;
  window.location.hash = '#/analysis';
}

// ═══════════════════════════════════════════════════════════════
// SECTION 4: 分析页
// ═══════════════════════════════════════════════════════════════

// ── 自动补全 ──

var debounceTimer = null;
var stockInput = $('#stock-input');
if (stockInput) {
  stockInput.addEventListener('input', function (e) {
    clearTimeout(debounceTimer);
    var q = e.target.value.trim();
    if (!q) { $('#suggestions').classList.add('hidden'); return; }
    debounceTimer = setTimeout(function () {
      fetch('/api/companies?q=' + encodeURIComponent(q))
        .then(function (r) { return r.json(); })
        .then(function (data) { renderSuggestions(data.results || []); })
        .catch(function () {});
    }, 300);
  });
}

function renderSuggestions(list) {
  var box = $('#suggestions');
  if (!list.length) { box.classList.add('hidden'); return; }
  box.innerHTML = list.map(function (s) {
    return '<div class="suggestion" data-code="' + s.code + '" data-name="' + escapeHtml(s.name) + '">'
      + '<span>' + escapeHtml(s.name) + '</span><span class="code">' + s.code + '</span></div>';
  }).join('');
  box.classList.remove('hidden');
}

var sugBox = $('#suggestions');
if (sugBox) {
  sugBox.addEventListener('click', function (e) {
    var el = e.target.closest('.suggestion');
    if (!el) return;
    selectStock(el.dataset.code, el.dataset.name);
  });
}

function selectStock(code, name) {
  STATE.selected = { code: code, name: name };
  var input = $('#stock-input');
  if (input) input.value = name + ' ' + code;
  $('#suggestions').classList.add('hidden');
  loadReports();
}

// ── 查询财报列表 ──

var searchBtn = $('#search-btn');
if (searchBtn) searchBtn.addEventListener('click', loadReports);

function initAnalysisPage() {
  clearError();
  loadHealth();
  loadAnalysisDimensions();
  // 首页快捷搜索直达：自动填入搜索框并查询
  if (STATE.pendingSearch) {
    var val = STATE.pendingSearch;
    STATE.pendingSearch = null;
    var inp = $('#stock-input');
    if (inp) inp.value = val;
    loadReports();
  }
}

async function loadReports() {
  var val = ($('#stock-input').value || '').trim();
  var code = '', name = '';
  val.split(/[\s,，]+/).forEach(function (p) {
    if (/^\d{6}$/.test(p)) code = p;
    else if (p) name = p;
  });
  if (!code) { showError('请输入 6 位股票代码（如 600900）或从下拉中选择'); return; }
  STATE.selected = { code: code, name: name || code };

  var start = ($('#year-start').value || '2023') + '-01-01';
  var end = ($('#year-end').value || '2025') + '-12-31';
  clearError();
  STATE.selectedReport = null;
  var pt = $('#preview-title');
  if (pt) pt.textContent = '';
  var pf = $('#pdf-frame');
  if (pf) pf.src = 'about:blank';
  updateAnalysisState();

  var reqId = ++STATE.reqIds.reports;
  try {
    var res = await fetch('/api/companies/' + code + '/reports?start=' + start + '&end=' + end);
    var data = await res.json();
    if (STATE.reqIds.reports !== reqId) return;
    if (!res.ok) { showError('查询失败（' + res.status + '）：' + (data.detail || '')); return; }
    STATE.selected.name = data.name || STATE.selected.name;
    var sl = $('#stock-label');
    if (sl) sl.textContent = '（' + (data.name || code) + '）';
    STATE.currentReports = data.reports || [];
    renderReports(STATE.currentReports);
    // 刷新本地已分析标记，确保「重新分析」/「查看本地分析」状态准确
    await loadHistoryItemsSilent();
    updateAnalysisState();
  } catch (err) { showError('查询出错：' + err.message); }
}

function renderReports(list) {
  var typeOrder = ['annual', 'semi_annual', 'quarterly'];
  var sorted = [].concat(list).sort(function (a, b) { return b.period.localeCompare(a.period); });
  sorted.sort(function (a, b) { return typeOrder.indexOf(a.type) - typeOrder.indexOf(b.type); });
  var rl = $('#report-list');
  if (!rl) return;
  var selCode = STATE.selected ? STATE.selected.code : '';
  rl.innerHTML = sorted.map(function (r) {
    var dlKey = selCode + ':' + r.period;
    var markHtml = r.downloaded ? '✓'
      : (STATE.downloading && STATE.downloading[dlKey])
        ? '<span class="mark-spinner"></span>'
        : '↧';
    return '<div class="report-item" data-period="' + r.period + '">'
      + '<span class="tag tag-' + r.type + '">' + (TYPE_LABEL[r.type] || escapeHtml(r.type)) + '</span>'
      + '<span class="title">' + escapeHtml(r.title || r.period) + '</span>'
      + (r.analyzed ? '<span class="mark-analyzed">已分析</span>' : '')
      + (r.analyzed
          ? '<button class="reanalyze-mini" data-period="' + r.period + '" title="重新触发 AI 分析">重新分析</button>'
          : '')
      + '<span class="mark">' + markHtml + '</span>'
      + '</div>';
  }).join('');
  $$('.report-item', rl).forEach(function (el) {
    el.addEventListener('click', function () { selectReport(el.dataset.period); });
  });
  // 行内「重新分析」小按钮：点击直接触发，不冒泡到行选中
  $$('.reanalyze-mini', rl).forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      selectReport(btn.dataset.period);
      startAnalysis();
    });
  });
}

function showPdfLoading(text) {
  var box = $('#pdf-loading');
  if (!box) return;
  var label = box.querySelector ? box.querySelector('span:last-child') : null;
  if (label && text) label.textContent = text;
  box.classList.remove('hidden');
}

function hidePdfLoading() {
  var box = $('#pdf-loading');
  if (box) box.classList.add('hidden');
}

async function selectReport(period) {
  if (!STATE.selected) return;
  var rl = $('#report-list');
  if (rl) {
    $$('.report-item', rl).forEach(function (el) {
      el.classList.toggle('selected', el.dataset.period === period);
    });
  }
  STATE.selectedReport = { period: period };
  var pt = $('#preview-title');
  if (pt) pt.textContent = STATE.selected.name + ' ' + period;
  var code = STATE.selected.code;
  var key = code + ':' + period;
  var pf = $('#pdf-frame');
  if (pf) {
    pf.onload = function () { hidePdfLoading(); };
  }
  // 未下载：先下载（按钮转圈 → 打钩）再加载预览；已下载直接预览
  var local = null;
  (STATE.currentReports || []).forEach(function (r) {
    if (r.period === period && r.downloaded) local = r;
  });
  if (local) {
    hidePdfLoading();
    if (pf) pf.src = '/api/reports/' + code + '/' + period + '.pdf';
  } else {
    await downloadAndPreview(code, period, key, pf);
  }
  renderAnalysisPanel(analysisKey(code, period));
  updateAnalysisState();
}

// 下载完成：打钩 + 加载预览
function finishDownload(code, period, key, pf) {
  STATE.downloading = STATE.downloading || {};
  STATE.downloading[key] = false;
  (STATE.currentReports || []).forEach(function (r) {
    if (r.period === period) r.downloaded = true;
  });
  renderReports(STATE.currentReports);
  if (pf && !pf.src) {
    hidePdfLoading();  // 下载完成：隐藏提示并加载预览
    pf.src = '/api/reports/' + code + '/' + period + '.pdf';
  } else {
    hidePdfLoading();
  }
}

// 下载财报：转圈 → 打钩 → 预览（幂等）
async function downloadAndPreview(code, period, key, pf) {
  STATE.downloading = STATE.downloading || {};
  if (STATE.downloading[key]) return;   // 已在下载中
  STATE.downloading[key] = true;
  renderReports(STATE.currentReports);  // 立即显示转圈
  showPdfLoading('正在下载财报…');       // 预览区明显提示
  try {
    var res = await fetch('/api/reports/' + code + '/' + period + '/download', { method: 'POST' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    finishDownload(code, period, key, pf);
  } catch (err) {
    // 后端无 download 端点（旧代码/未重启）：回退 iframe 自动下载预览
    //（serve_pdf 会自动下载，iframe 加载完成即视为下载完成）
    if (pf) {
      pf.onload = function () {
        pf.onload = null;
        finishDownload(code, period, key, pf);
      };
      pf.src = '/api/reports/' + code + '/' + period + '.pdf';
    } else {
      finishDownload(code, period, key, pf);
    }
  }
}

function updateAnalysisState() {
  var isAnalyzing = STATE.analyzingReport
    && STATE.selected && STATE.selected.code === STATE.analyzingReport.code
    && STATE.selectedReport && STATE.selectedReport.period === STATE.analyzingReport.period;
  var ready = STATE.aiConfigured && !!STATE.selectedReport && !isAnalyzing;
  var ab = $('#analyze-btn'), qb = $('#qa-btn'), qi = $('#qa-input');
  var local = findLocalAnalysis(
    STATE.selected && STATE.selected.code,
    STATE.selectedReport && STATE.selectedReport.period
  );
  if (ab) {
    ab.disabled = !ready;
    // 已分析过的报告支持一键重新触发分析
    ab.textContent = local ? '重新分析' : '开始分析';
  }
  if (qb) qb.disabled = !ready;
  if (qi) qi.disabled = !ready;

  var vlb = $('#view-local-btn');
  if (vlb) vlb.classList.toggle('hidden', !local);
}

function findLocalAnalysis(code, period) {
  if (!code || !period) return null;
  var match = null;
  (STATE.historyItems || []).forEach(function (item) {
    if (item.code === code && item.period === period && item.has_analysis) {
      match = item;
    }
  });
  return match;
}

var viewLocalBtn = $('#view-local-btn');
if (viewLocalBtn) {
  viewLocalBtn.addEventListener('click', function () {
    var local = findLocalAnalysis(
      STATE.selected && STATE.selected.code,
      STATE.selectedReport && STATE.selectedReport.period
    );
    if (!local || !local.analysis_filename) return;
    loadAndShowAnalysis(local.analysis_filename, function (content) {
      renderAnalysisInDetail(
        local.company, local.code, local.period, local.year,
        content, '来源于: reports/analysis/' + local.analysis_filename
      );
    });
  });
}

// ── 分析任务 ──

var analyzeBtn = $('#analyze-btn');
if (analyzeBtn) {
  analyzeBtn.addEventListener('click', function () { startAnalysis(); });
}

async function submitAnalysis(code, period, dims) {
  var res = await fetch(
    '/api/reports/' + code + '/' + period + '/analyze',
    { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dimensions: dims }) });
  var data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'HTTP ' + res.status);
  return data.task_id;
}

function showRetryBtn(container, btnId, label, onClick) {
  if (!container) return;
  var btn = document.createElement('button');
  btn.id = btnId;
  btn.className = 'btn primary full-btn';
  btn.textContent = label;
  btn.addEventListener('click', onClick);
  container.appendChild(btn);
}

function analysisKey(code, period) { return code + ':' + period; }

function isCurrentAnalysis(code, period) {
  return STATE.selected && STATE.selected.code === code
    && STATE.selectedReport && STATE.selectedReport.period === period;
}

function renderAnalysisPanel(key) {
  var ar = $('#analyze-result');
  if (!ar) return;
  var st = STATE.analysisCache[key];
  if (!st) { ar.innerHTML = ''; return; }
  if (st.status === 'pending' || st.status === 'running') {
    var dimCount = st.dims && st.dims.length ? st.dims.length : 5;
    var hint = st.status === 'pending'
      ? '<div class="hint">任务已提交（' + dimCount + ' 个维度），等待执行…</div>'
      : '<div class="hint">分析进行中…（' + dimCount + ' 个维度，逐维度请求 AI，通常 1–' + Math.max(3, dimCount) + ' 分钟）</div>';
    ar.innerHTML = hint + '<button id="stop-analyze-btn" class="btn danger full-btn">⏹ 停止分析</button>';
    var stopBtn = $('#stop-analyze-btn');
    if (stopBtn) {
      stopBtn.addEventListener('click', function () { stopAnalysis(key, st.taskId); });
    }
  } else if (st.status === 'cancelled') {
    ar.innerHTML = '<div class="hint">⏹ 分析已停止' + (st.error ? '：' + escapeHtml(st.error) : '') + '</div>';
  } else if (st.status === 'failed') {
    ar.innerHTML = '<div class="error-card">分析失败：' + escapeHtml(st.error || '未知错误') + '</div>';
    showRetryBtn(ar, 'retry-analyze-btn', '重新分析', function () { startAnalysis(); });
  } else if (st.status === 'done') {
    ar.innerHTML = renderReport(st.data || {});
    bindDimTabs(ar);
    if (st.data && st.data.markdown_path) {
      ar.insertAdjacentHTML('afterbegin',
        '<div class="hint" style="text-align:left">✅ 已保存：' + escapeHtml(st.data.markdown_path) + '</div>');
    }
  }
}

// ── 分析维度勾选面板：从 /api/analysis/dimensions 动态渲染 ──

// 服务端不可用时的兜底默认维度（与后端内置默认一致）
var FALLBACK_DIMENSIONS = [
  { id: 'financial_summary', name: '财务摘要', default: true },
  { id: 'risk_warning', name: '风险识别', default: true },
  { id: 'business_highlights', name: '经营亮点', default: true },
  { id: 'profit_quality', name: '盈利质量', default: true },
  { id: 'cashflow', name: '现金流分析', default: true }
];

async function loadAnalysisDimensions() {
  var dimsBox = $('#dims');
  if (!dimsBox) return;
  var items = [];
  try {
    var res = await fetch('/api/analysis/dimensions');
    if (res.ok) {
      var data = await res.json();
      items = data.dimensions || [];
    }
  } catch (_) { /* 网络异常走兜底清单 */ }
  STATE.analysisDimensions = items.length ? items : FALLBACK_DIMENSIONS;
  renderDimensionCheckboxes();
}

function renderDimensionCheckboxes() {
  var dimsBox = $('#dims');
  if (!dimsBox) return;
  var items = STATE.analysisDimensions;
  if (!items || !items.length) return;
  dimsBox.innerHTML = items.map(function (d) {
    var checked = d.default ? ' checked' : '';
    var title = d.description
      ? ' title="' + escapeHtml(d.description) + '"'
      : '';
    return '<label' + title + '>'
      + '<input type="checkbox" value="' + escapeHtml(d.id) + '"' + checked + ' /> '
      + escapeHtml(d.name)
      + '</label>';
  }).join('');
  updateDimCount();
}

function updateDimCount() {
  var countEl = $('#dims-count');
  if (!countEl) return;
  var n = $$('#dims input:checked').length;
  countEl.textContent = n > 0
    ? '已选 ' + n + ' 个维度，预计 ' + n + ' 次 AI 调用'
    : '未勾选维度时将使用默认 5 个';
}

var dimsSelectAllBtn = $('#dims-select-all');
if (dimsSelectAllBtn) {
  dimsSelectAllBtn.addEventListener('click', function () {
    $$('#dims input').forEach(function (i) { i.checked = true; });
    updateDimCount();
  });
}

var dimsClearBtn = $('#dims-clear');
if (dimsClearBtn) {
  dimsClearBtn.addEventListener('click', function () {
    $$('#dims input').forEach(function (i) { i.checked = false; });
    updateDimCount();
  });
}

// 停止分析：请求后端取消任务（当前维度 LLM 调用完成后生效）
async function stopAnalysis(key, taskId) {
  if (!taskId) return;
  try {
    await fetch('/api/tasks/' + taskId + '/cancel', { method: 'POST' });
  } catch (_) { /* 忽略网络错误，轮询会揭示终态 */ }
  var ar = $('#analyze-result');
  if (ar) {
    ar.innerHTML = '<div class="hint">正在停止分析…（当前维度完成后生效）</div>';
  }
}

async function startAnalysis() {
  var code = STATE.selected && STATE.selected.code;
  var period = STATE.selectedReport && STATE.selectedReport.period;
  if (!code || !period) return;
  var key = analysisKey(code, period);
  // 该报告已在分析中则不重复发起
  if (STATE.analyzingReport && STATE.analyzingReport.code === code
      && STATE.analyzingReport.period === period) return;
  STATE.analyzingReport = { code: code, period: period };
  var dims = $$('#dims input:checked').map(function (i) { return i.value; });
  STATE.analysisCache[key] = { status: 'pending', dims: dims };
  renderAnalysisPanel(key);
  updateAnalysisState();
  try {
    var taskId = await submitAnalysis(code, period, dims);
    STATE.analysisCache[key] = { status: 'running', taskId: taskId };
    if (isCurrentAnalysis(code, period)) renderAnalysisPanel(key);
    pollTask(taskId, code, period);
  } catch (err) {
    STATE.analyzingReport = null;
    STATE.analysisCache[key] = { status: 'failed', error: err.message };
    renderAnalysisPanel(key);
    updateAnalysisState();
  }
}

async function pollTask(taskId, code, period) {
  var key = analysisKey(code, period);
  try {
    var res = await fetch('/api/tasks/' + taskId);
    var t = await res.json();
  } catch (_) {
    setTimeout(function () { pollTask(taskId, code, period); }, 2000);
    return;
  }
  if (t.status === 'running' || t.status === 'pending') {
    // 仅当用户仍停留在此报告时更新进度
    if (isCurrentAnalysis(code, period)) renderAnalysisPanel(key);
    setTimeout(function () { pollTask(taskId, code, period); }, 2000);
    return;
  }
  // 终态：写入按报告隔离的缓存；切走再切回也能看到结果/失败
  if (STATE.analyzingReport && STATE.analyzingReport.code === code
      && STATE.analyzingReport.period === period) {
    STATE.analyzingReport = null;
  }
  if (t.status === 'failed') {
    STATE.analysisCache[key] = { status: 'failed', error: t.error || '未知错误' };
  } else if (t.status === 'cancelled') {
    STATE.analysisCache[key] = { status: 'cancelled', error: t.error || '分析已停止' };
  } else {
    STATE.analysisCache[key] = { status: 'done', data: t.result || {} };
  }
  if (isCurrentAnalysis(code, period)) {
    renderAnalysisPanel(key);
    updateAnalysisState();
  }
  // Refresh history items so the new analysis appears
  await loadHistoryItemsSilent();
}

// ── 渲染分析结果 ──

// ── 分析维度 Tab 切换：一次只展示一个维度，顶部按钮手动切换 ──

// 维度配色/图标：财务摘要(蓝)、风险识别(红)、经营亮点(绿)
var DIM_STYLE = {
  financial_summary:   { icon: '📊', tab: 'dim-tab-financial',   panel: 'dim-panel-financial' },
  risk_warning:        { icon: '⚠️', tab: 'dim-tab-risk',        panel: 'dim-panel-risk' },
  business_highlights: { icon: '💡', tab: 'dim-tab-highlight',   panel: 'dim-panel-highlight' },
};

function dimStyle(id) {
  return DIM_STYLE[id] || { icon: '📄', tab: 'dim-tab-generic', panel: 'dim-panel-generic' };
}

// 渲染单个维度卡片内容：空内容展示"暂无对应数据"占位并把原因说清楚，
// 避免整块空白，也避免用户误以为"报告里没数据"——原因区分模型空返回/思考过程/截断等
function renderDimensionContent(d) {
  var content = (d.content || '').trim();
  if (!content) {
    var reason = d.error || '该维度未生成内容，可点击"重新分析"重试';
    return '<div class="empty-card">'
      + '<div class="empty-title">📭 暂无对应数据</div>'
      + '<div class="empty-reason">' + escapeHtml(reason) + '</div>'
      + '</div>';
  }
  if (d.error) {
    return '<span class="error-card">' + escapeHtml(d.error) + '</span>'
      + renderMarkdown(d.content);
  }
  return renderMarkdown(d.content);
}

function renderDimensionTabs(dims) {
  if (!dims || !dims.length) return '<div class="hint">无分析结果</div>';
  var tabsHtml = dims.map(function (d, i) {
    var st = dimStyle(d.id);
    return '<button type="button" class="dim-tab ' + st.tab + (i === 0 ? ' active' : '') + '"'
      + ' data-index="' + i + '">' + st.icon + ' ' + escapeHtml(d.name || d.id || ('维度 ' + (i + 1))) + '</button>';
  }).join('');
  var panelsHtml = dims.map(function (d, i) {
    var st = dimStyle(d.id);
    return '<div class="dim-panel ' + st.panel + (i === 0 ? ' active' : '') + '" data-index="' + i + '">'
      + '<div class="dim-card">' + renderDimensionContent(d) + '</div></div>';
  }).join('');
  return '<div class="dim-tabs">' + tabsHtml + '</div>'
    + '<div class="dim-panels">' + panelsHtml + '</div>';
}

function bindDimTabs(container) {
  // 事件委托，容器常驻只绑定一次（innerHTML 更新不影响）
  if (!container || container.dataset.dimTabsBound) return;
  container.dataset.dimTabsBound = '1';
  container.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest ? e.target.closest('.dim-tab') : null;
    if (!btn) return;
    var idx = btn.dataset.index;
    $$('.dim-tab', container).forEach(function (t) {
      t.classList.toggle('active', t.dataset.index === idx);
    });
    $$('.dim-panel', container).forEach(function (p) {
      p.classList.toggle('active', p.dataset.index === idx);
    });
  });
}

function renderReport(report) {
  return renderDimensionTabs(report.dimensions || []);
}

// ── Markdown 渲染 ──

// ── Markdown 渲染（markdown-it：支持表格 / 代码块 / 链接 / 列表等）──

var mdRenderer = null;
if (window.markdownit) {
  mdRenderer = window.markdownit({
    html: false,     // 输入中的 HTML 一律转义，防 XSS
    linkify: true,   // 自动识别裸链接
  });
}

function renderMarkdown(text) {
  if (!mdRenderer) {
    // 兜底：库未加载时显示原文
    return '<div class="hint">（Markdown 渲染库未加载，以下为原文）</div>'
      + '<pre>' + escapeHtml(String(text)) + '</pre>';
  }
  return mdRenderer.render(String(text));
}

// ── 问答 ──

var qaBtn = $('#qa-btn'), qaInput = $('#qa-input');
if (qaBtn) qaBtn.addEventListener('click', function () { sendAnalysisQA(); });
if (qaInput) qaInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') sendAnalysisQA(); });

async function sendAnalysisQA() {
  var inp = $('#qa-input');
  var q = inp.value.trim();
  if (!q || !STATE.selectedReport || !STATE.selected) return;
  inp.value = '';
  appendQa('#qa-history', 'user', q);
  try {
    var res = await fetch(
      '/api/reports/' + STATE.selected.code + '/' + STATE.selectedReport.period + '/chat',
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q }) });
    var data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'HTTP ' + res.status);
    appendQa('#qa-history', 'assistant', data.answer);
    // RAG 命中时后端会返回 citations，渲染在答案下方
    appendCitations('#qa-history', data.citations || []);
  } catch (err) {
    appendQa('#qa-history', 'assistant', '⚠️ ' + err.message);
  }
}

function appendQa(sel, role, content) {
  var box = $(sel);
  if (!box) return;
  var div = document.createElement('div');
  div.className = 'qa-msg ' + role;
  div.innerHTML = role === 'user' ? escapeHtml(content) : renderMarkdown(content);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// ═══════════════════════════════════════════════════════════════
// SECTION 5: 历史页
// ═══════════════════════════════════════════════════════════════

async function initHistoryPage() {
  var list = $('#history-list');
  if (list) list.innerHTML = '<p class="hint">正在读取本地数据…</p>';
  await loadHistoryItems();
  bindHistoryFilters();
}

async function loadHistoryItems() {
  try {
    var res = await fetch('/api/history');
    var data = await res.json();
    STATE.historyItems = data.items || [];
  } catch (_) {
    STATE.historyItems = [];
  }
  renderHistoryList(STATE.historyItems);
  populateYearFilter();
}

async function loadHistoryItemsSilent() {
  try {
    var res = await fetch('/api/history');
    var data = await res.json();
    STATE.historyItems = data.items || [];
  } catch (_) {}
}

function renderHistoryList(items) {
  var box = $('#history-list');
  if (!box) return;
  if (!items.length) {
    box.innerHTML = '<p class="hint">暂无本地财报数据<br>请先通过分析页下载并分析报告</p>';
    updateHistoryCount(0);
    return;
  }
  // 按公司（代码）分组为二级目录
  var groups = {};
  items.forEach(function (item) {
    var key = item.code || item.company || '其他';
    (groups[key] = groups[key] || []).push(item);
  });
  var codes = Object.keys(groups).sort(function (a, b) {
    return (groups[b].length - groups[a].length) || String(a).localeCompare(String(b));
  });

  function historyItemHtml(item) {
    var selected = STATE.historySelected && STATE.historySelected.code === item.code
      && STATE.historySelected.period === item.period;
    return '<div class="history-item' + (selected ? ' selected' : '') + '"'
      + ' data-code="' + escapeHtml(item.code) + '"'
      + ' data-period="' + escapeHtml(item.period) + '">'
      + '<span class="h-type h-type-' + (item.type || '年报') + '">' + (item.type || '年报') + '</span>'
      + '<span class="h-company" title="' + escapeHtml(item.company) + '">' + escapeHtml(item.company) + '</span>'
      + '<span class="h-year">' + (item.year || '—') + '</span>'
      + '<span class="h-status ' + (item.has_analysis ? 'h-status-analyzed' : 'h-status-pending') + '">'
      + (item.has_analysis ? '已分析' : '未分析') + '</span>'
      + '</div>';
  }

  box.innerHTML = codes.map(function (code) {
    var g = groups[code];
    var collapsed = STATE.historyCollapsed[code];
    var company = g[0].company || code;
    return '<div class="history-group" data-group="' + escapeHtml(code) + '">'
      + '<div class="history-group-head" title="点击展开/收起">'
      + '<span class="history-group-folder">' + (collapsed ? '📁' : '📂') + '</span>'
      + '<span class="history-group-company">' + escapeHtml(company) + '</span>'
      + '<span class="history-group-code">' + escapeHtml(code) + '</span>'
      + '<span class="history-group-count">（' + g.length + '）</span>'
      + '</div>'
      + (collapsed ? '' : '<div class="history-group-body">' + g.map(historyItemHtml).join('') + '</div>')
      + '</div>';
  }).join('');

  bindHistoryList();
  updateHistoryCount(items.length);
}

// 历史列表事件委托：分组展开/收起 + 报告选中 + 重新分析（只绑定一次）
function bindHistoryList() {
  var box = $('#history-list');
  if (!box || box.dataset.bound) return;
  box.dataset.bound = '1';
  box.addEventListener('click', function (e) {
    var head = e.target.closest ? e.target.closest('.history-group-head') : null;
    if (head) {
      var group = head.parentNode;
      if (!group) return;
      var code = group.dataset.group;
      STATE.historyCollapsed[code] = !STATE.historyCollapsed[code];
      var current = STATE.historyFiltered || STATE.historyItems;
      renderHistoryList(current);
      return;
    }
    var item = e.target.closest ? e.target.closest('.history-item') : null;
    if (item) {
      selectHistoryItem(item.dataset.code, item.dataset.period);
    }
  });
}



function updateHistoryCount(n) {
  var el = $('#history-count');
  if (el) el.textContent = n + ' 条';
  var htotal = $('#history-total');
  if (htotal) htotal.textContent = '（共 ' + n + ' 条）';
}

// ── 筛选 ──

function bindHistoryFilters() {
  var search = $('#history-search');
  var typeFilter = $('#history-type-filter');
  var yearFilter = $('#history-year-filter');

  var filterFn = function () { filterHistoryItems(); };
  if (search) search.addEventListener('input', filterFn);
  if (typeFilter) typeFilter.addEventListener('change', filterFn);
  if (yearFilter) yearFilter.addEventListener('change', filterFn);
}

function populateYearFilter() {
  var yearFilter = $('#history-year-filter');
  if (!yearFilter) return;
  var years = {};
  STATE.historyItems.forEach(function (item) {
    if (item.year) years[item.year] = true;
  });
  var sorted = Object.keys(years).sort(function (a, b) { return b - a; });
  var currentVal = yearFilter.value;
  yearFilter.innerHTML = '<option value="">全部年份</option>'
    + sorted.map(function (y) { return '<option value="' + y + '">' + y + '</option>'; }).join('');
  if (currentVal) yearFilter.value = currentVal;
}

function filterHistoryItems() {
  var search = ($('#history-search').value || '').trim().toLowerCase();
  var typeFilter = $('#history-type-filter').value;
  var yearFilter = $('#history-year-filter').value;

  var filtered = STATE.historyItems.filter(function (item) {
    if (search && item.company.toLowerCase().indexOf(search) === -1
        && item.code.indexOf(search) === -1
        && String(item.year).indexOf(search) === -1) {
      return false;
    }
    if (typeFilter && item.type !== typeFilter) return false;
    if (yearFilter && String(item.year) !== yearFilter) return false;
    return true;
  });

  STATE.historyFiltered = filtered;
  renderHistoryList(filtered);
}

// ── 选中历史项 ──

async function selectHistoryItem(code, period) {
  var item = null;
  for (var i = 0; i < STATE.historyItems.length; i++) {
    if (STATE.historyItems[i].code === code && STATE.historyItems[i].period === period) {
      item = STATE.historyItems[i];
      break;
    }
  }
  if (!item) return;

  STATE.historySelected = item;
  STATE.historyFiltered = null;          // 选中后恢复全量列表（分组状态保留）
  renderHistoryList(STATE.historyItems); // re-render to update selected
  // 详情面板右上角「重新分析」：仅已分析的报告可重新触发
  var reBtn = $('#history-reanalyze-btn');
  if (reBtn) reBtn.classList.toggle('hidden', !item.has_analysis);

  // Update detail panel header
  var title = $('#history-detail-title');
  if (title) title.textContent = item.company + ' · ' + (item.year || '') + (item.type || '');
  var badge = $('#history-detail-badge');
  if (badge) {
    badge.textContent = item.has_analysis ? '已分析' : '未分析';
    badge.className = 'badge ' + (item.has_analysis ? 'badge-purple' : 'badge-warn');
  }

  var detail = $('#history-detail');
  if (!detail) return;

  // 分析状态按报告隔离：切回分析中/已完成的报告时展示对应状态
  var cacheKey = analysisKey(code, period);
  var cached = STATE.historyAnalysisCache[cacheKey];
  if (cached && (cached.status === 'pending' || cached.status === 'running')) {
    detail.innerHTML = '<p class="hint">分析进行中…（逐维度请求 AI，通常 1–3 分钟）</p>';
    return;
  }
  if (cached && cached.status === 'failed') {
    detail.innerHTML = '<div class="error-card">分析失败：' + escapeHtml(cached.error || '未知错误') + '</div>';
    showRetryBtn(detail, 'history-retry-btn', '重试分析', function () {
      startHistoryAnalysis({ code: code, period: period });
    });
    return;
  }
  if (cached && cached.status === 'done' && cached.data) {
    renderAnalysisInDetail(
      item.company, item.code, item.period, item.year,
      cached.data, '来源：reports/analysis/' + (item.analysis_filename || '')
    );
    return;
  }

  if (item.has_analysis && item.analysis_filename) {
    await loadAndShowAnalysis(item.analysis_filename, function (content) {
      renderAnalysisInDetail(
        item.company, item.code, item.period, item.year,
        content, '来源：reports/analysis/' + item.analysis_filename
      );
    });
  } else {
    showHistoryNoAnalysis(item);
  }
}

function showHistoryNoAnalysis(item) {
  var detail = $('#history-detail');
  if (!detail) return;
  detail.innerHTML = ''
    + '<div class="history-detail-empty">'
    + '<div class="empty-icon">📄</div>'
    + '<p>此报告尚未分析</p>'
    + '<p style="font-size:12px;color:var(--text-muted)">'
    + escapeHtml(item.company) + ' · ' + (item.year || '') + (item.type || '') + '</p>'
    + '<button id="history-analyze-btn" class="btn primary">开始 AI 分析</button>'
    + '<p style="font-size:11px;color:var(--text-muted)">注意：分析需要调用 AI 接口，消耗 Token</p>'
    + '</div>';

  var btn = $('#history-analyze-btn');
  if (btn) {
    btn.addEventListener('click', function () {
      startHistoryAnalysis(item);
    });
  }
}

async function loadAndShowAnalysis(filename, callback) {
  try {
    var res = await fetch('/api/history/' + encodeURIComponent(filename));
    if (res.ok) {
      var content = await res.json();
      callback(content);
    } else {
      var detail = $('#history-detail');
      if (detail) detail.innerHTML = '<p class="hint">无法读取分析文件</p>';
    }
  } catch (_) {
    var detail2 = $('#history-detail');
    if (detail2) detail2.innerHTML = '<p class="hint">读取分析文件失败</p>';
  }
}

function renderAnalysisInDetail(company, code, period, year, content, source) {
  var detail = $('#history-detail');
  if (!detail) return;

  var html = '';
  // Source footer
  if (source) {
    html += '<p style="font-size:12px;color:var(--text-muted);margin-bottom:12px;text-align:right">'
      + escapeHtml(source) + '</p>';
  }
  // Dimensions（Tab 切换，一次展示一个维度）
  html += renderDimensionTabs(content.dimensions || []);

  // Meta info
  var meta = content.meta || {};
  html += '<div style="margin-top:16px;padding:10px 0;border-top:1px solid var(--border);'
    + 'font-size:12px;color:var(--text-muted);display:flex;gap:16px;flex-wrap:wrap">';
  if (meta.model) html += '<span>模型: ' + escapeHtml(meta.model) + '</span>';
  if (meta.timestamp) html += '<span>时间: ' + escapeHtml(meta.timestamp) + '</span>';
  if (meta.total_tokens) html += '<span>Token: ' + meta.total_tokens + '</span>';
  html += '</div>';

  // Chat section
  html += '<div class="qa-box" style="margin-top:16px">'
    + '<h3>针对此报告提问</h3>'
    + '<button id="h-qa-goto-btn" class="btn primary full-btn">💬 去智能问答提问（聚焦本报告）</button>'
    + '<p class="hint" style="font-size:12px;margin-top:6px">跳转到智能问答，检索将优先本报告内容，可结合实时数据追问</p>'
    + '</div>';

  detail.innerHTML = html;

  // 维度 Tab 切换（事件委托，容器常驻只绑定一次）
  bindDimTabs(detail);

  // 跳转到智能问答（聚焦本报告，提升 RAG 检索权重）
  var hQaGoto = $('#h-qa-goto-btn');
  if (hQaGoto) {
    hQaGoto.addEventListener('click', function () {
      STATE.chatFocusReport = { code: code, period: period, company: company || '' };
      // 聚焦跳转：自动开启新会话，避免聚焦检索混入当前会话上下文
      newChatSession();
      window.location.hash = '#/chat';
    });
  }
}

// ── 历史页触发 AI 分析 ──

var historyReanalyzeBtn = $('#history-reanalyze-btn');
if (historyReanalyzeBtn) {
  historyReanalyzeBtn.addEventListener('click', function () {
    if (STATE.historySelected) startHistoryAnalysis(STATE.historySelected);
  });
}

async function startHistoryAnalysis(item) {
  var code = item.code, period = item.period;
  var key = analysisKey(code, period);
  // 该报告已在分析中则不重复发起
  if (STATE.historyAnalyzingReport && STATE.historyAnalyzingReport.code === code
      && STATE.historyAnalyzingReport.period === period) return;
  STATE.historyAnalyzingReport = { code: code, period: period };
  STATE.historyAnalysisCache[key] = { status: 'pending' };

  var detail = $('#history-detail');
  if (detail) detail.innerHTML = '<p class="hint">任务已提交，等待执行…</p>';

  try {
    var res = await fetch(
      '/api/reports/' + code + '/' + period + '/analyze',
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dimensions: ['financial_summary', 'risk_warning', 'business_highlights'] }) });
    var data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'HTTP ' + res.status);
    STATE.historyAnalysisCache[key] = { status: 'running' };
    pollHistoryTask(data.task_id, item);
  } catch (err) {
    STATE.historyAnalyzingReport = null;
    STATE.historyAnalysisCache[key] = { status: 'failed', error: err.message };
    if (detail) {
      detail.innerHTML = '<div class="error-card">' + escapeHtml(err.message) + '</div>';
      showRetryBtn(detail, 'history-retry-btn', '重试分析', function () { startHistoryAnalysis(item); });
    }
  }
}

async function pollHistoryTask(taskId, item) {
  var code = item.code, period = item.period;
  var key = analysisKey(code, period);
  var isSelected = function () {
    return STATE.historySelected && STATE.historySelected.code === code
      && STATE.historySelected.period === period;
  };
  try {
    var res = await fetch('/api/tasks/' + taskId);
    var t = await res.json();
  } catch (_) {
    STATE.historyPollId = setTimeout(function () { pollHistoryTask(taskId, item); }, 2000);
    return;
  }
  if (t.status === 'running' || t.status === 'pending') {
    // 仅当用户仍停留在此报告时更新详情
    if (isSelected()) {
      var detail = $('#history-detail');
      if (detail) detail.innerHTML = '<p class="hint">分析进行中…（逐维度请求 AI，通常 1–3 分钟）</p>';
    }
    STATE.historyPollId = setTimeout(function () { pollHistoryTask(taskId, item); }, 2000);
    return;
  }
  // 终态：写入按报告隔离的缓存
  if (STATE.historyAnalyzingReport && STATE.historyAnalyzingReport.code === code
      && STATE.historyAnalyzingReport.period === period) {
    STATE.historyAnalyzingReport = null;
  }
  if (t.status === 'failed') {
    STATE.historyAnalysisCache[key] = { status: 'failed', error: t.error || '未知错误' };
    if (isSelected()) {
      var detail2 = $('#history-detail');
      if (detail2) {
        detail2.innerHTML = '<div class="error-card">分析失败：' + escapeHtml(t.error || '未知错误') + '</div>';
        showRetryBtn(detail2, 'history-retry-btn', '重试分析', function () { startHistoryAnalysis(item); });
      }
    }
    return;
  }
  // Success: refresh history list and show result
  await loadHistoryItemsSilent();
  var updatedItem = null;
  for (var i = 0; i < STATE.historyItems.length; i++) {
    if (STATE.historyItems[i].code === code && STATE.historyItems[i].period === period) {
      updatedItem = STATE.historyItems[i];
      break;
    }
  }
  STATE.historySelected = (updatedItem || item);
  if (updatedItem && updatedItem.has_analysis && updatedItem.analysis_filename) {
    await loadAndShowAnalysis(updatedItem.analysis_filename, function (content) {
      renderAnalysisInDetail(
        updatedItem.company, updatedItem.code, updatedItem.period, updatedItem.year,
        content, '来源：reports/analysis/' + updatedItem.analysis_filename
      );
    });
  }
  renderHistoryList(STATE.historyItems);
}

// ── 历史页对话 ──

async function sendHistoryQA(code, period) {
  var inp = $('#h-qa-input');
  if (!inp) return;
  var q = inp.value.trim();
  if (!q || !code || !period) return;
  inp.value = '';
  appendQa('#h-qa-history', 'user', q);
  try {
    var res = await fetch(
      '/api/reports/' + code + '/' + period + '/chat',
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q }) });
    var data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'HTTP ' + res.status);
    appendQa('#h-qa-history', 'assistant', data.answer);
    // RAG 命中时后端会返回 citations，渲染在答案下方
    appendCitations('#h-qa-history', data.citations || []);
  } catch (err) {
    appendQa('#h-qa-history', 'assistant', '⚠️ ' + err.message);
  }
}

// ═══════════════════════════════════════════════════════════════
// SECTION 6: Chart helpers (optional, no-op if Chart not loaded)
// ═══════════════════════════════════════════════════════════════

function destroyAllCharts() {
  if (STATE.charts.revenue) { STATE.charts.revenue.destroy(); STATE.charts.revenue = null; }
  if (STATE.charts.ratio)   { STATE.charts.ratio.destroy();   STATE.charts.ratio = null; }
}

// ═══════════════════════════════════════════════════════════════
// SECTION 7: RAG 知识库
// ═══════════════════════════════════════════════════════════════

var RAG_FILTERS = { all: '全部', annual: '年报', semi_annual: '半年报', quarterly: '季报' };
var ragState = { items: [], filter: 'all', loading: false };

async function initRagPage() {
  var statusBar = $('#rag-status-bar');
  if (statusBar) statusBar.innerHTML = '<p class="hint">正在读取 RAG 状态…</p>';
  loadMcpStatus();
  await loadRagFiles();
}

// ── MCP 状态检查：熔断状态 + 诊断 ──

async function loadMcpStatus() {
  var box = $('#mcp-status-bar');
  if (!box) return;
  try {
    var res = await fetch('/api/mcp/status');
    var st = await res.json();
    renderMcpStatus(box, st);
  } catch (_) {
    box.innerHTML = '<div class="mcp-status-card mcp-status-unknown">⚙️ MCP 状态不可用</div>';
  }
}

function circuitLabel(circuit) {
  if (circuit === 'open') return '🔴 熔断中';
  if (circuit === 'half_open') return '🟡 探测中';
  return '🟢 正常';
}

function renderMcpStatus(box, st) {
  var cls = st.circuit === 'open' ? 'mcp-status-bad'
    : st.circuit === 'half_open' ? 'mcp-status-warn' : 'mcp-status-ok';
  var diag = st.diagnose || {};
  var diagText = diag.message || '尚未执行检测';
  var injected = st.tools_injected ? '已注入' : '未注入';
  box.innerHTML = '<div class="mcp-status-card ' + cls + '">'
    + '<div class="mcp-status-row">'
    + '<span>' + circuitLabel(st.circuit) + ' MCP 工具 · ' + injected + '</span>'
    + '<span class="mcp-status-meta">连续失败 ' + st.consecutive_failures + '/' + st.failure_threshold
    + ' · 成功 ' + st.success_calls + '/' + st.total_calls + '</span>'
    + '</div>'
    + '<div class="mcp-status-diagnose" title="' + escapeHtml(diagText) + '">🔎 ' + escapeHtml(diagText) + '</div>'
    + '<button id="mcp-diagnose-btn" class="btn dim-tool-btn">运行检测</button>'
    + '</div>';
  var btn = $('#mcp-diagnose-btn');
  if (btn) {
    btn.addEventListener('click', async function () {
      btn.disabled = true;
      btn.textContent = '检测中…';
      try {
        var res = await fetch('/api/mcp/diagnose', { method: 'POST' });
        var d = await res.json();
        renderMcpStatus(box, Object.assign({}, st, { diagnose: d }));
      } catch (e) {
        btn.textContent = '检测失败';
      }
    });
  }
}

async function loadRagFiles() {
  if (ragState.loading) return;
  ragState.loading = true;
  try {
    var res = await fetch('/api/rag/files');
    var data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'HTTP ' + res.status);
    renderRagPage(data);
  } catch (err) {
    var box = $('#rag-files');
    if (box) box.innerHTML = '<p class="hint">⚠️ ' + escapeHtml(err.message) + '</p>';
  } finally {
    ragState.loading = false;
  }
}

function renderRagPage(data) {
  ragState.items = data.items || [];
  if (!data.enabled) {
    $('#rag-status-bar').innerHTML =
      '<p class="hint">RAG 未启用：请在 config.yaml 中配置 rag.enabled: true 并重启服务。</p>';
    $('#rag-toolbar').style.display = 'none';
    $('#rag-files').innerHTML = '';
    return;
  }
  var stats = data.stats || {};
  $('#rag-status-bar').innerHTML =
    '<span class="badge badge-ok">已加入 ' + stats.added + '</span> '
    + '<span class="badge badge-warn">未加入 ' + stats.not_added + '</span> '
    + '<span class="badge">总片段 ' + stats.total_chunks + '</span>';
  $('#rag-toolbar').style.display = '';

  var filters = $('#rag-filters');
  filters.innerHTML = Object.keys(RAG_FILTERS).map(function (key) {
    var active = ragState.filter === key ? ' active' : '';
    return '<button class="tab-btn' + active + '" data-filter="' + key + '">'
      + RAG_FILTERS[key] + '</button>';
  }).join('');
  filters.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      ragState.filter = btn.dataset.filter;
      renderRagList();
      filters.querySelectorAll('.tab-btn').forEach(function (b) {
        b.classList.toggle('active', b === btn);
      });
    });
  });

  renderRagList();
}

function renderRagList() {
  var box = $('#rag-files');
  if (!box) return;
  var items = ragState.items;
  if (ragState.filter !== 'all') {
    items = items.filter(function (it) { return it.type === ragState.filter; });
  }
  if (!items.length) {
    box.innerHTML = '<p class="hint">当前分类下没有文件</p>';
    return;
  }
  box.innerHTML = items.map(function (it) {
    var badge = it.added
      ? '<span class="badge badge-ok">已加入</span>'
      : '<span class="badge badge-warn">未加入</span>';
    var srcLabel = it.source === 'analysis' ? 'AI 分析报告' : 'PDF 原文';
    var action = it.added
      ? '<button class="btn btn-sm" data-action="delete" data-rid="' + escapeHtml(it.report_id)
        + '" data-source="' + it.source + '">删除索引</button>'
      : '<button class="btn btn-sm primary" data-action="ingest" data-rid="' + escapeHtml(it.report_id)
        + '" data-source="' + it.source + '">加入</button>';
    return '<div class="rag-file-row">'
      + '<div class="rag-file-info">'
      + '<div class="rag-file-name">' + escapeHtml(it.filename) + '</div>'
      + '<div class="rag-file-meta">' + escapeHtml(it.company) + '（' + it.code + '）'
      + ' · ' + it.year + '年 · ' + escapeHtml(it.type_label) + ' · ' + srcLabel
      + ' · ' + it.chunk_count + ' chunks</div>'
      + '</div>'
      + '<div class="rag-file-actions">' + badge + action + '</div>'
      + '</div>';
  }).join('');

  box.querySelectorAll('button[data-action]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var rid = btn.dataset.rid, source = btn.dataset.source;
      if (btn.dataset.action === 'ingest') {
        ingestOneFile(rid, source, btn);
      } else {
        deleteIndex(rid, source, btn);
      }
    });
  });
}

async function ingestOneFile(reportId, source, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '加入中…'; }
  try {
    var res = await fetch('/api/rag/ingest/one', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ report_id: reportId, source: source }),
    });
    var data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'HTTP ' + res.status);
    await pollRagTask(data.task_id);
    await loadRagFiles();
  } catch (err) {
    alert('加入失败：' + err.message);
    await loadRagFiles();
  }
}

async function deleteIndex(reportId, source, btn) {
  if (!confirm('确定删除该文件的 RAG 索引？将恢复为未加入状态。')) return;
  if (btn) { btn.disabled = true; }
  try {
    var res = await fetch('/api/rag/index/' + encodeURIComponent(reportId) + '/' + source, {
      method: 'DELETE',
    });
    if (!res.ok) {
      var errData = await res.json();
      throw new Error(errData.detail || 'HTTP ' + res.status);
    }
    await loadRagFiles();
  } catch (err) {
    alert('删除失败：' + err.message);
    await loadRagFiles();
  }
}

async function pollRagTask(taskId) {
  for (var i = 0; i < 600; i++) {           // 最多等 60s
    await new Promise(function (r) { setTimeout(r, 100); });
    var res = await fetch('/api/tasks/' + taskId);
    var t = await res.json();
    if (t.status === 'done') return;
    if (t.status === 'failed') throw new Error(t.error || '任务失败');
  }
  throw new Error('任务超时');
}

// 一键全部加入
var ragIngestAllBtn = $('#rag-ingest-all-btn');
if (ragIngestAllBtn) {
  ragIngestAllBtn.addEventListener('click', async function () {
    if (!confirm('将全部未加入文件加入 RAG 索引，确定？')) return;
    ragIngestAllBtn.disabled = true;
    ragIngestAllBtn.textContent = '加入中…';
    try {
      var res = await fetch('/api/rag/ingest', { method: 'POST' });
      var data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'HTTP ' + res.status);
      await pollRagTask(data.task_id);
      await loadRagFiles();
    } catch (err) {
      alert('一键加入失败：' + err.message);
    } finally {
      ragIngestAllBtn.disabled = false;
      ragIngestAllBtn.textContent = '一键全部加入';
    }
  });
}


// ═══════════════════════════════════════════════════════════════
// SECTION 8: 智能问答（通用入口 + 单报告引用展示）
// ═══════════════════════════════════════════════════════════════

var chatSessionId = null;   // 当前会话 id（null = 新会话）
var chatSessions = [];       // 历史会话列表
// 进行中的流式请求：sessionKey -> { reader, stopped, answerText, hasContent }
// 支持「在不同会话中同时发起请求」——每个会话独立一个流，互不阻塞
var chatStreams = {};

function chatStreamKey() {
  return chatSessionId || '__new__';
}

function isCurrentChatStream(key) {
  return key === chatStreamKey();
}

function renderChatFocusBar() {
  var bar = $('#chat-focus-bar');
  if (!bar) return;
  var fr = STATE.chatFocusReport;
  if (!fr || !fr.code) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  var label = $('#chat-focus-label');
  if (label) {
    label.textContent = '聚焦报告：' + (fr.company || '') + '（' + fr.code + ' · ' + fr.period + '）——检索优先本报告';
  }
}

var chatFocusClearBtn = $('#chat-focus-clear');
if (chatFocusClearBtn) {
  chatFocusClearBtn.addEventListener('click', function () {
    STATE.chatFocusReport = null;
    renderChatFocusBar();
  });
}

async function initChatPage() {
  var input = $('#chat-input');
  if (!input) return;

  // 进入页面时刷新 AI 状态，按配置启用/禁用输入
  await loadHealth();
  var ready = !!STATE.aiConfigured;
  var box = $('#chat-history');
  if (box && !ready) {
    box.innerHTML = '<p class="hint">AI 未配置：请在 config.yaml 中配置 openai api_key，并确认 RAG 已启用。</p>';
  }

  input.disabled = !ready;
  var sendBtn = $('#chat-send-btn');
  if (sendBtn) sendBtn.disabled = !ready;

  renderChatFocusBar();

  // 首次进入：加载历史会话并选中最近一个；只加载一次避免路由切换重复请求
  if (!input.dataset.chatLoaded) {
    input.dataset.chatLoaded = '1';
    await loadChatSessions();
  }

  // 事件只绑定一次，避免路由反复进入重复触发
  if (input.dataset.chatBound) return;
  input.dataset.chatBound = '1';
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') sendChatQA();
  });
  if (sendBtn) {
    sendBtn.addEventListener('click', function () { sendChatQA(); });
  }
  var newBtn = $('#chat-new-btn');
  if (newBtn) {
    newBtn.addEventListener('click', function () { newChatSession(); });
  }
  bindChatSessionList();
}

// ── 历史会话：列表 / 新建 / 切换 ──

async function loadChatSessions() {
  try {
    var res = await fetch('/api/chat/sessions');
    if (res.ok) {
      var data = await res.json();
      chatSessions = data.sessions || [];
    }
  } catch (_) {
    chatSessions = [];
  }
  renderChatSessionList();
  // 首次进入：默认选中最近会话；无会话则开启新会话。
  // 从历史详情聚焦跳转时保持"新会话"，不自动选中最近会话，
  // 让聚焦检索从干净上下文开始（用户手动切到历史会话聚焦也允许）
  if (chatSessionId === null) {
    if (STATE.chatFocusReport && STATE.chatFocusReport.code) {
      newChatSession();
    } else if (chatSessions.length) {
      await openChatSession(chatSessions[0].id);
    } else {
      newChatSession();
    }
  }
}

function renderChatSessionList() {
  var listBox = $('#chat-session-list');
  if (!listBox) return;
  if (!chatSessions.length) {
    listBox.innerHTML = '<p class="hint">暂无历史会话，点击「新会话」开始提问</p>';
    return;
  }
  listBox.innerHTML = chatSessions.map(function (s) {
    var active = s.id === chatSessionId ? ' active' : '';
    var title = escapeHtml(s.title || '新会话');
    var time = s.updated_at ? new Date(s.updated_at).toLocaleString() : '';
    return '<div class="chat-session-item' + active + '" data-sid="' + escapeHtml(s.id) + '" title="' + escapeHtml(time) + '">'
      + '<div class="chat-session-main">'
      + '<div class="chat-session-title">' + title + '</div>'
      + '<div class="chat-session-meta">' + s.message_count + ' 条消息</div>'
      + '</div>'
      + '<div class="chat-session-actions">'
      + '<button class="chat-session-btn chat-rename-btn" data-sid="' + escapeHtml(s.id) + '" title="重命名会话">✎</button>'
      + '<button class="chat-session-btn chat-delete-btn" data-sid="' + escapeHtml(s.id) + '" title="删除会话">🗑</button>'
      + '</div>'
      + '</div>';
  }).join('');
}

function bindChatSessionList() {
  var listBox = $('#chat-session-list');
  if (!listBox || listBox.dataset.bound) return;
  listBox.dataset.bound = '1';
  listBox.addEventListener('click', function (e) {
    var renameBtn = e.target.closest ? e.target.closest('.chat-rename-btn') : null;
    if (renameBtn) { renameChatSession(renameBtn.dataset.sid); return; }
    var delBtn = e.target.closest ? e.target.closest('.chat-delete-btn') : null;
    if (delBtn) { deleteChatSession(delBtn.dataset.sid); return; }
    var item = e.target.closest ? e.target.closest('.chat-session-item') : null;
    if (item && item.dataset.sid) openChatSession(item.dataset.sid);
  });
}

// 重命名历史会话（prompt 输入新标题）
async function renameChatSession(sid) {
  var s = chatSessions.find(function (x) { return x.id === sid; });
  var cur = s ? (s.title || '新会话') : '';
  var title = prompt('重命名会话', cur);
  if (title === null) return;
  title = title.trim();
  if (!title) return;
  try {
    var res = await fetch('/api/chat/sessions/' + encodeURIComponent(sid), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    await loadChatSessions();
  } catch (_) { /* 失败保持原状 */ }
}

// 删除历史会话（confirm 确认后删除；当前会话被删则回到最近会话）
async function deleteChatSession(sid) {
  if (!confirm('确定删除该历史会话？删除后不可恢复。')) return;
  try {
    var res = await fetch('/api/chat/sessions/' + encodeURIComponent(sid), { method: 'DELETE' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var st = chatStreams[sid];
    if (st && st.reader) { try { await st.reader.cancel(); } catch (_) {} }
    delete chatStreams[sid];
    if (chatSessionId === sid) {
      chatSessionId = null;
      var box = $('#chat-history');
      if (box) box.innerHTML = '';
    }
    await loadChatSessions();
  } catch (_) { /* 失败保持原状 */ }
}

async function openChatSession(sid) {
  chatSessionId = sid;
  renderChatSessionList();
  var box = $('#chat-history');
  if (box) box.innerHTML = '';
  updateChatSendBtn();
  // 该会话若有进行中的流：显示「生成中」+ 已累积部分（流在后台继续）
  var st = chatStreams[sid];
  if (st && box) {
    var hint = document.createElement('div');
    hint.className = 'chat-msg assistant thinking';
    hint.innerHTML = '<span class="chat-thinking-dots"><span></span><span></span><span></span></span>'
      + '<span class="chat-thinking-text">该会话正在生成中…</span>';
    box.appendChild(hint);
    if (st.answerText) {
      var partial = document.createElement('div');
      partial.className = 'chat-msg assistant';
      partial.innerHTML = renderMarkdown(st.answerText);
      box.appendChild(partial);
    }
  }
  try {
    var res = await fetch('/api/chat/sessions/' + sid);
    if (res.ok) {
      var data = await res.json();
      (data.messages || []).forEach(function (m) {
        if (m && m.role && m.content) appendChatMsg('#chat-history', m.role, m.content);
      });
    }
  } catch (_) { /* 加载失败保持空会话 */ }
  scrollChatToBottom();
}

function newChatSession() {
  // 优先锚定历史中「未对话过的新会话」（空会话），避免反复创建导致堆积
  var empty = chatSessions.find(function (s) { return s.message_count === 0; });
  if (empty) {
    openChatSession(empty.id);
    return;
  }
  // 无空会话：创建新空会话（历史列表新增一条）
  chatSessionId = null;
  var box = $('#chat-history');
  if (box) box.innerHTML = '';
  renderChatSessionList();
  updateChatSendBtn();
  fetch('/api/chat/sessions', { method: 'POST' })
    .then(function (res) { return res.ok ? res.json() : null; })
    .then(function (d) {
      if (d && d.session_id) {
        chatSessionId = d.session_id;
        renderChatSessionList();  // 历史列表新增一条
      }
    })
    .catch(function () { /* 创建失败：保持无会话状态，提问时后端兜底复用 */ });
  var input = $('#chat-input');
  if (input) input.focus();
}

function scrollChatToBottom() {
  var box = $('#chat-history');
  if (box) box.scrollTop = box.scrollHeight;
}

// 按当前会话状态刷新发送/停止按钮
function updateChatSendBtn() {
  var sendBtn = $('#chat-send-btn');
  if (!sendBtn) return;
  var streaming = !!chatStreams[chatStreamKey()];
  sendBtn.textContent = streaming ? '⏹ 停止' : '发送';
  sendBtn.classList.toggle('danger', streaming);
  sendBtn.onclick = streaming ? function () { stopChatStream(); } : null;
  sendBtn.disabled = !STATE.aiConfigured;
}

// ── 流式问答（SSE）+ 思考状态 ──

function appendThinkingBubble(sel) {
  var box = $(sel);
  if (!box) return null;
  var div = document.createElement('div');
  div.className = 'chat-msg assistant thinking';
  div.innerHTML = '<span class="chat-thinking-dots"><span></span><span></span><span></span></span>'
    + '<span class="chat-thinking-text">思考中，正在检索知识库并处理…</span>';
  box.appendChild(div);
  scrollChatToBottom();
  return div;
}

function setThinkingText(thinkingEl, text) {
  if (!thinkingEl) return;
  var txtEl = thinkingEl.querySelector ? thinkingEl.querySelector('.chat-thinking-text') : null;
  if (txtEl) txtEl.textContent = text;
}

function parseSseFrame(frame) {
  var event = '', dataLines = [];
  frame.split('\n').forEach(function (line) {
    if (line.indexOf('event:') === 0) event = line.slice(6).trim();
    else if (line.indexOf('data:') === 0) dataLines.push(line.slice(5).trim());
  });
  if (!dataLines.length) return null;
  var data;
  try { data = JSON.parse(dataLines.join('\n')); } catch (_) { return null; }
  return { event: event, data: data };
}

// 停止当前会话的流式生成（中断 SSE；后端会把已生成部分保存进历史）
function stopChatStream() {
  var st = chatStreams[chatStreamKey()];
  if (st) {
    st.stopped = true;
    if (st.reader) {
      try { st.reader.cancel(); } catch (_) { /* 忽略取消异常 */ }
    }
  }
}

// ── 暂存草稿：当前会话流式进行中，输入框新消息先暂存，待对话结束后提交 ──
var chatDrafts = [];   // 暂存消息文本列表（多条合并为一条提交）

function renderDraftBar() {
  var bar = $('#chat-draft-bar');
  if (!bar) return;
  if (!chatDrafts.length) {
    bar.classList.add('hidden');
    bar.innerHTML = '';
    return;
  }
  bar.classList.remove('hidden');
  bar.innerHTML = '<div class="chat-draft-items">'
    + chatDrafts.map(function (d, i) {
        return '<div class="chat-draft-item">'
          + '<span class="chat-draft-text">' + escapeHtml(d) + '</span>'
          + '<button class="chat-draft-remove" data-i="' + i + '" title="删除">×</button>'
          + '</div>';
      }).join('')
    + '</div>'
    + '<div class="chat-draft-actions">'
    + '<span class="chat-draft-hint">已暂存 ' + chatDrafts.length + ' 条，提交时合并为一条消息</span>'
    + '<button id="chat-draft-submit" class="btn primary chat-draft-submit" title="打断当前回答并提交暂存内容">提交暂存</button>'
    + '</div>';
  bindDraftBar();
}

function bindDraftBar() {
  var bar = $('#chat-draft-bar');
  if (!bar || bar.dataset.bound) return;
  bar.dataset.bound = '1';
  bar.addEventListener('click', function (e) {
    var rm = e.target.closest ? e.target.closest('.chat-draft-remove') : null;
    if (rm && rm.dataset.i !== undefined) {
      var idx = parseInt(rm.dataset.i, 10);
      if (!isNaN(idx)) removeDraft(idx);
      return;
    }
    var submit = e.target.closest ? e.target.closest('#chat-draft-submit') : null;
    if (submit) stopAndSubmitDrafts();
  });
}

function removeDraft(idx) {
  if (idx >= 0 && idx < chatDrafts.length) {
    chatDrafts.splice(idx, 1);
    renderDraftBar();
  }
}

// 打断当前对话并提交暂存内容（多条合并为一条消息）
async function stopAndSubmitDrafts() {
  var key = chatStreamKey();
  var st = chatStreams[key];
  if (st) {
    st.draftsTaken = true;   // 草稿由本流程接管，避免流结束时自动提交重复
    stopChatStream();        // 打断当前流（后端保存已生成部分）
  }
  // 等待当前流真正结束（最多 10 秒）
  var deadline = Date.now() + 10000;
  while (chatStreams[key] && Date.now() < deadline) {
    await new Promise(function (r) { setTimeout(r, 100); });
  }
  var texts = chatDrafts.slice();
  chatDrafts = [];
  renderDraftBar();
  if (!texts.length) return;
  var merged = texts.join('\n\n');
  var input = $('#chat-input');
  if (input) input.value = '';
  await submitQuestion(merged, chatStreamKey());
}

async function sendChatQA() {
  var input = $('#chat-input');
  if (!input) return;
  var q = input.value.trim();
  if (!q) return;
  var key = chatStreamKey();
  if (chatStreams[key]) {
    // 当前会话有流在跑：进入暂存区，不打断当前对话
    chatDrafts.push(q);
    input.value = '';
    renderDraftBar();
    return;
  }
  input.value = '';
  await submitQuestion(q, key);
}

async function submitQuestion(q, key) {
  appendChatMsg('#chat-history', 'user', q);
  renderDraftBar();   // 流开始：草稿按钮切换为「打断并提交」
  var sendBtn = $('#chat-send-btn');
  if (sendBtn) {
    sendBtn.textContent = '⏹ 停止';
    sendBtn.disabled = false;
    sendBtn.classList.add('danger');
    sendBtn.onclick = function () { stopChatStream(); };
  }

  // 思考状态：模型首个内容前提示「正在处理中」；推理/工具调用均有可见中间态
  var st = { reader: null, stopped: false, answerText: '', hasContent: false,
             finishedNormally: false, draftsTaken: false, toolStepCount: 0 };
  chatStreams[key] = st;
  var thinkingEl = appendThinkingBubble('#chat-history');
  var reasoningEl = null;   // 推理过程区块（模型思考中，灰色流式）
  var reasoningText = '';
  var toolStepsEl = null;   // 工具调用步骤区块（⏳ 调用中 → ✅ 已获取）
  var assistantEl = null;

  try {
    var body = { question: q, session_id: chatSessionId };
    if (STATE.chatFocusReport && STATE.chatFocusReport.code && STATE.chatFocusReport.period) {
      body.focus_report = { code: STATE.chatFocusReport.code, period: STATE.chatFocusReport.period };
    }
    var res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) {
      var errData = null;
      try { errData = await res.json(); } catch (_) {}
      throw new Error(errData && errData.detail ? errData.detail : 'HTTP ' + res.status);
    }

    var reader = res.body.getReader();
    st.reader = reader;
    var decoder = new TextDecoder();
    var buffer = '';
    var done = false;

    function handleFrame(frame) {
      var parsed = parseSseFrame(frame);
      if (!parsed) return;
      if (parsed.event === 'session') {
        // 新会话在首帧获得真实 session_id：把流归属迁移到该 id
        var newSid = parsed.data.session_id;
        if (key === '__new__' && newSid) {
          delete chatStreams['__new__'];
          key = newSid;
          chatStreams[key] = st;
          chatSessionId = newSid;
        }
      } else if (parsed.event === 'delta') {
        var text = parsed.data.text || '';
        var reasoning = parsed.data.reasoning || '';
        if (reasoning && !text) {
          // 模型思考阶段：流式展示推理进度，避免用户以为中断
          st.hasThinking = true;
          reasoningText += reasoning;
          if (!isCurrentChatStream(key)) return;  // 已切走：后台累积
          if (!reasoningEl) {
            reasoningEl = document.createElement('div');
            reasoningEl.className = 'chat-msg assistant reasoning';
            reasoningEl.innerHTML = '<div class="chat-reasoning-head">🧠 模型思考中…</div>'
              + '<div class="chat-reasoning-body"></div>';
            // 中间态始终插在最终内容（assistantEl）之前，避免回答在上思考在下
            if (assistantEl && assistantEl.parentNode) {
              assistantEl.parentNode.insertBefore(reasoningEl, assistantEl);
            } else if (thinkingEl && thinkingEl.parentNode) {
              thinkingEl.parentNode.insertBefore(reasoningEl, thinkingEl.nextSibling);
            } else {
              $('#chat-history').appendChild(reasoningEl);
            }
          }
          var rBody = reasoningEl.querySelector ? reasoningEl.querySelector('.chat-reasoning-body') : null;
          if (rBody) {
            rBody.textContent = reasoningText.length > 600
              ? reasoningText.slice(-600) + ' …'
              : reasoningText;
          }
          scrollChatToBottom();
        }
        if (text) {
          st.hasContent = true;
          st.answerText += text;
          if (!isCurrentChatStream(key)) return;  // 已切到别的会话：后台累积
          if (!assistantEl) {
            // 首个内容增量：移除思考气泡与推理块，切换为流式文本容器
            if (thinkingEl && thinkingEl.parentNode) thinkingEl.parentNode.removeChild(thinkingEl);
            thinkingEl = null;
            if (reasoningEl && reasoningEl.parentNode) reasoningEl.parentNode.removeChild(reasoningEl);
            reasoningEl = null;
            assistantEl = document.createElement('div');
            assistantEl.className = 'chat-msg assistant streaming';
            assistantEl.textContent = '';
            $('#chat-history').appendChild(assistantEl);
          }
          assistantEl.textContent = st.answerText;
          scrollChatToBottom();
        }
      } else if (parsed.event === 'tool_call') {
        // MCP 工具调用：更新思考文案 + 添加工具步骤（⏳ 调用中）
        var toolName = parsed.data.name || '';
        if (isCurrentChatStream(key)) {
          if (thinkingEl) setThinkingText(thinkingEl, '正在调用工具「' + toolName + '」…');
          if (!toolStepsEl) {
            toolStepsEl = document.createElement('div');
            toolStepsEl.className = 'chat-tool-steps';
            // 工具步骤同样保持在最终内容之前
            if (assistantEl && assistantEl.parentNode) {
              assistantEl.parentNode.insertBefore(toolStepsEl, assistantEl);
            } else if (reasoningEl && reasoningEl.parentNode) {
              reasoningEl.parentNode.insertBefore(toolStepsEl, reasoningEl.nextSibling);
            } else if (thinkingEl && thinkingEl.parentNode) {
              thinkingEl.parentNode.insertBefore(toolStepsEl, thinkingEl.nextSibling);
            } else {
              $('#chat-history').appendChild(toolStepsEl);
            }
          }
          st.toolStepCount += 1;
          var step = document.createElement('div');
          step.className = 'chat-tool-step running';
          step.dataset.name = toolName;
          step.innerHTML = '<span class="chat-tool-step-icon">⏳</span> 正在调用「' + escapeHtml(toolName) + '」…';
          toolStepsEl.appendChild(step);
          scrollChatToBottom();
        }
      } else if (parsed.event === 'tool_result') {
        // 工具返回：对应步骤标记为已获取，保持中间态可见
        var rName = parsed.data.name || '';
        if (isCurrentChatStream(key) && toolStepsEl) {
          var ok = parsed.data.ok !== false;
          var steps = toolStepsEl.children;
          for (var si = steps.length - 1; si >= 0; si--) {
            if (steps[si].dataset && steps[si].dataset.name === rName
                && steps[si].className.indexOf('running') >= 0) {
              steps[si].className = ok ? 'chat-tool-step done' : 'chat-tool-step failed';
              steps[si].innerHTML = '<span class="chat-tool-step-icon">' + (ok ? '✅' : '❌') + '</span> '
                + (ok ? '已获取「' + escapeHtml(rName) + '」数据'
                     : '获取「' + escapeHtml(rName) + '」失败');
              break;
            }
          }
          if (thinkingEl) setThinkingText(thinkingEl, ok ? '已获取工具数据，正在整理回答…' : '工具获取失败，继续基于已有信息回答…');
          scrollChatToBottom();
        }
      } else if (parsed.event === 'done') {
        done = true;
        st.finishedNormally = true;
        st.answerText = parsed.data.answer || st.answerText;
        if (parsed.data.session_id) chatSessionId = parsed.data.session_id;
        if (isCurrentChatStream(key)) {
          if (assistantEl && assistantEl.parentNode) assistantEl.parentNode.removeChild(assistantEl);
          if (thinkingEl && thinkingEl.parentNode) thinkingEl.parentNode.removeChild(thinkingEl);
          thinkingEl = null;
          if (reasoningEl && reasoningEl.parentNode) reasoningEl.parentNode.removeChild(reasoningEl);
          reasoningEl = null;
          appendChatMsg('#chat-history', 'assistant', st.answerText);
          appendCitations('#chat-history', parsed.data.citations || []);
          // MCP 工具徽章
          if (parsed.data.tools_used && parsed.data.tools_used.length) {
            var badge = document.createElement('div');
            badge.className = 'chat-tools-badge';
            badge.textContent = '🔧 已参考 MCP 实时数据';
            $('#chat-history').appendChild(badge);
          }
          scrollChatToBottom();
        }
        // 刷新会话列表（新会话或更新时间变化），并选中当前会话
        loadChatSessions().then(function () {
          if (chatSessionId) {
            renderChatSessionList();
            var activeItem = document.querySelector('.chat-session-item[data-sid="' + chatSessionId + '"]');
            if (activeItem) activeItem.classList.add('active');
          }
        });
      } else if (parsed.event === 'error') {
        throw new Error(parsed.data.error || '流式响应出错');
      }
    }

    while (true) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      var frames = buffer.split('\n\n');
      buffer = frames.pop();
      for (var i = 0; i < frames.length; i++) handleFrame(frames[i]);
    }
    st.reader = null;
    if (buffer.trim()) handleFrame(buffer.trim());
    if (!done) {
      // 未收到 done：用户主动停止或连接中断
      var stoppedText = st.stopped ? '⏹ 已停止生成' : '⚠️ 连接中断，已显示部分内容';
      if (isCurrentChatStream(key)) {
        if (st.hasContent && assistantEl) {
          if (assistantEl.parentNode) assistantEl.parentNode.removeChild(assistantEl);
          if (thinkingEl && thinkingEl.parentNode) thinkingEl.parentNode.removeChild(thinkingEl);
          thinkingEl = null;
          appendChatMsg('#chat-history', 'assistant', st.answerText + '\n\n' + stoppedText);
        } else {
          if (thinkingEl && thinkingEl.parentNode) thinkingEl.parentNode.removeChild(thinkingEl);
          thinkingEl = null;
          appendChatMsg('#chat-history', 'assistant', stoppedText);
        }
        scrollChatToBottom();
      }
      // 部分回答已由后端保存进会话历史，刷新列表
      loadChatSessions();
    }
  } catch (err) {
    if (st.stopped) {
      // 用户主动停止：不报错，保留已生成部分
      if (isCurrentChatStream(key)) {
        if (thinkingEl && thinkingEl.parentNode) thinkingEl.parentNode.removeChild(thinkingEl);
        if (assistantEl && assistantEl.parentNode) assistantEl.parentNode.removeChild(assistantEl);
        if (st.answerText) appendChatMsg('#chat-history', 'assistant', st.answerText + '\n\n⏹ 已停止生成');
        else appendChatMsg('#chat-history', 'assistant', '⏹ 已停止');
        scrollChatToBottom();
      }
      loadChatSessions();
    } else {
      if (isCurrentChatStream(key)) {
        if (thinkingEl && thinkingEl.parentNode) thinkingEl.parentNode.removeChild(thinkingEl);
        if (assistantEl && assistantEl.parentNode) assistantEl.parentNode.removeChild(assistantEl);
        appendChatMsg('#chat-history', 'assistant', '⚠️ ' + err.message);
        scrollChatToBottom();
      }
    }
  } finally {
    delete chatStreams[key];
    updateChatSendBtn();
    renderDraftBar();
    // 对话正常结束后：暂存内容自动合并提交（用户打断/中断时不自动提交）
    if (st.finishedNormally && !st.draftsTaken && chatDrafts.length) {
      autoSubmitDrafts();
    }
  }
}

// 自动提交全部暂存内容（合并为一条消息）
async function autoSubmitDrafts() {
  var texts = chatDrafts.slice();
  chatDrafts = [];
  renderDraftBar();
  if (!texts.length) return;
  var input = $('#chat-input');
  if (input) input.value = '';
  await submitQuestion(texts.join('\n\n'), chatStreamKey());
}

function appendChatMsg(sel, role, content) {
  var box = $(sel);
  if (!box) return;
  var div = document.createElement('div');
  div.className = 'chat-msg ' + role;
  div.innerHTML = role === 'user' ? escapeHtml(content) : renderMarkdown(content);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// ── 引用卡片：智能问答页 / 分析页 / 历史页复用 ──

function citationSourceLabel(source) {
  if (source === 'analysis') return 'AI 分析报告';
  if (source === 'pdf') return 'PDF 原文';
  return source || '来源';
}

// 启发式缩写摘要：优先提取含财务关键信号的句子（数字/增长/利润/现金流等），
// 总长控制在 80 字内，让折叠预览能看出具体内容而非机械截断。
function summarizeSnippet(text) {
  var s = String(text || '').trim();
  if (!s) return '';
  var sentences = s.match(/[^。；!?！？\n]+[。；!?！？]?/g) || [];
  var signals = /(增长|下降|下滑|同比|环比|亿元|万元|净利|营收|利润|毛利|负债|现金流|周转|占比|％|%|突破|新高|盈利|亏损|分红|回购|研发)/;
  // 优先取含财务关键信号的句子；无信号句时退回开头两句
  var signalSentences = sentences.filter(function (sen) { return signals.test(sen); });
  var picked = (signalSentences.length ? signalSentences : sentences.slice(0, 2))
    .map(function (sen) { return sen.trim(); })
    .filter(Boolean);
  var summary = picked.join('').slice(0, 80).trim();
  if (!summary) summary = s.slice(0, 60).trim();
  return summary.length < s.length ? summary + '…' : summary;
}

function renderCitationCards(citations) {
  var items = citations || [];
  if (!items.length) return '';
  return items.map(function (c, idx) {
    var meta = '';
    if (c.section) meta += '<span class="citation-meta">' + escapeHtml(c.section) + '</span>';
    if (c.page !== undefined && c.page !== null && c.page !== '') {
      meta += '<span class="citation-meta">第 ' + escapeHtml(String(c.page)) + ' 页</span>';
    }
    var snippet = c.snippet ? String(c.snippet) : '';
    // 默认折叠：展示来源/章节/页码 + 语义缩写摘要，点击展开全文
    var preview = summarizeSnippet(snippet);
    var srcClass = c.source === 'analysis' ? 'src-analysis' : 'src-pdf';
    return '<details class="citation-card">'
      + '<summary class="citation-summary">'
      + '<span class="citation-index">[' + (idx + 1) + ']</span>'
      + '<span class="citation-source ' + srcClass + '">' + escapeHtml(citationSourceLabel(c.source)) + '</span>'
      + meta
      + '<span class="citation-preview">' + escapeHtml(preview) + '</span>'
      + '</summary>'
      + '<div class="citation-snippet">' + escapeHtml(snippet) + '</div>'
      + '</details>';
  }).join('');
}

function appendCitations(sel, citations) {
  var box = $(sel);
  if (!box || !citations || !citations.length) return;
  var div = document.createElement('div');
  div.className = 'citation-list';
  div.innerHTML = '<div class="citation-head">📎 引用来源</div>' + renderCitationCards(citations);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// ═══════════════════════════════════════════════════════════════
// 启动
// ═══════════════════════════════════════════════════════════════

// 路由器必须立即初始化，不能等 loadHealth 异步返回
initRouter();
// 后台异步检查 AI 健康状态
loadHealth();
