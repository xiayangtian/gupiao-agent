/* Investment news page. All provider-controlled strings enter the DOM through textContent. */
(function () {
  'use strict';

  var PAGE = '#/investment-news';
  var PAGE_SIZE = 20;
  var OPERATION_STATUS_TIMEOUT_MS = 5000;
  var statusTimers = {};
  var state = {
    initialized: false,
    busy: { collection: false, digest: false },
    kind: 'market',
    day: '',
    sort: 'importance',
    symbol: '',
    offset: 0,
    total: 0,
    watchlist: [],
    status: null,
    autocompleteTimer: null,
  };

  function byId(id) { return document.getElementById(id); }

  function shanghaiToday() {
    var parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(new Date());
    var values = {};
    parts.forEach(function (part) { values[part.type] = part.value; });
    return values.year + '-' + values.month + '-' + values.day;
  }

  function safeText(value, fallback) {
    if (typeof value === 'string' && value.trim()) return value.trim();
    return fallback || '';
  }

  function node(tag, className, text) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function externalLink(url, label, className) {
    try {
      var parsed = new URL(url);
      if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return null;
      var link = node('a', className, label);
      link.href = parsed.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      return link;
    } catch (_) {
      return null;
    }
  }

  function formatTime(value) {
    if (!value) return '发布时间未知';
    var parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '时间未知';
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    }).format(parsed);
  }

  async function requestJson(url, options) {
    var response = await fetch(url, options || {});
    var payload = {};
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) {
      var detail = payload.detail || payload.error || ('HTTP ' + response.status);
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    return payload;
  }

  function setStatus(id, message, stateName, autoHide) {
    var element = byId(id);
    if (!element) return;
    if (statusTimers[id]) {
      window.clearTimeout(statusTimers[id]);
      delete statusTimers[id];
    }
    element.textContent = message || '';
    element.classList.toggle('error', stateName === 'error');
    element.classList.toggle('success', stateName === 'success');
    if (autoHide && message) {
      statusTimers[id] = window.setTimeout(function () {
        element.textContent = '';
        element.classList.remove('error', 'success');
        delete statusTimers[id];
      }, OPERATION_STATUS_TIMEOUT_MS);
    }
  }

  function runStatusLabel(status) {
    return ({
      queued: '排队中', running: '运行中', succeeded: '成功', partial: '部分成功',
      failed: '失败', skipped: '已跳过',
    })[status] || status || '未知';
  }

  function updateActionButtons() {
    var collect = byId('news-collect');
    var digest = byId('news-generate-digest');
    var activeCollection = state.status && state.status.active_collection;
    var activeDigest = state.status && state.status.active_digest;
    if (collect) {
      collect.disabled = state.busy.collection || Boolean(activeCollection);
      collect.textContent = state.busy.collection || activeCollection ? '采集中…' : '采集资讯';
    }
    if (digest) {
      digest.disabled = state.busy.digest || Boolean(activeDigest);
      digest.textContent = state.busy.digest || activeDigest ? '生成中…' : '生成今日综述';
    }
  }

  async function loadStatus() {
    try {
      state.status = await requestJson('/api/investment-news/status');
      var messages = ['定时采集：每日 09:00–16:00 整点（Asia/Shanghai）；自动综述：09:00、16:00。'];
      if (!state.status.ai_configured) messages.push('AI 未配置，综述生成可能失败。');
      setStatus('news-status', messages.join(' '));
      var latest = state.status.latest_successful_runs || {};
      setStatus('news-last-collection-success', latest.collection ? formatTime(latest.collection.finished_at) : '暂无成功记录');
      setStatus('news-last-digest-success', latest.digest ? formatTime(latest.digest.finished_at) : '暂无成功记录');
    } catch (error) {
      setStatus('news-status', '暂时无法读取运行状态：' + error.message, 'error');
    }
    updateActionButtons();
  }

  function appendDigestSection(container, title, points) {
    if (!Array.isArray(points) || !points.length) return;
    var section = node('section', 'news-digest-section');
    section.appendChild(node('h3', '', title));
    points.forEach(function (point) {
      var article = node('article', 'news-digest-point');
      article.appendChild(node('p', '', safeText(point.text, '')));
      var citations = Array.isArray(point.citations) ? point.citations : [];
      if (citations.length) {
        var links = node('div', 'news-citations');
        citations.forEach(function (citation, index) {
          var link = externalLink(citation.url, '来源' + (index + 1) + '：' + safeText(citation.source_name, '原文'));
          if (link) links.appendChild(link);
        });
        if (point.citation_count > citations.length) {
          links.appendChild(node('span', '', '另有 ' + (point.citation_count - citations.length) + ' 条来源'));
        }
        if (links.childNodes.length) article.appendChild(links);
      }
      section.appendChild(article);
    });
    container.appendChild(section);
  }

  async function loadDigest() {
    var content = byId('news-digest');
    if (!content) return;
    try {
      var response = await requestJson('/api/investment-news/digest');
      var digest = response.digest;
      content.replaceChildren();
      var badge = byId('news-digest-badge');
      var meta = byId('news-digest-meta');
      if (!digest) {
        if (badge) { badge.textContent = '尚未生成'; badge.className = 'badge badge-info'; }
        content.appendChild(node('p', 'news-empty', '今日综述尚未生成。手动生成只读取已入库资讯，不会触发采集。'));
        return;
      }
      if (badge) {
        badge.textContent = response.stale ? '输入已变化' : '已生成';
        badge.className = 'badge ' + (response.stale ? 'badge-warn' : 'badge-ok');
      }
      if (meta) {
        meta.textContent = '生成于 ' + formatTime(digest.generated_at) + ' · 覆盖 ' + digest.input_count +
          ' 条资讯 · ' + digest.source_count + ' 个来源链接 · 数据截至 ' + formatTime(digest.data_through);
      }
      var payload = digest.payload || {};
      appendDigestSection(content, '市场主题', payload.market_themes);
      appendDigestSection(content, '政策与监管', payload.policy_regulatory);
      appendDigestSection(content, '自选股重点', payload.watchlist);
      appendDigestSection(content, '风险与不确定性', payload.risks);
      if (!content.childNodes.length) content.appendChild(node('p', 'news-empty', '本次综述没有生成带有效来源引用的要点。'));
    } catch (error) {
      setStatus('news-digest-run-status', '读取综述失败：' + error.message, 'error');
    }
  }

  function renderWatchlist() {
    var list = byId('news-watchlist');
    var count = byId('news-watchlist-count');
    var filter = byId('news-symbol-filter');
    if (!list || !filter) return;
    list.replaceChildren();
    filter.replaceChildren();
    filter.appendChild(node('option', '', '全部自选股'));
    filter.options[0].value = '';
    state.watchlist.forEach(function (item) {
      var option = node('option', '', item.name + '（' + item.code + '）');
      option.value = item.code;
      filter.appendChild(option);
      var row = node('li', 'news-watchlist-item');
      var label = node('span', 'news-watchlist-item-label', item.name + ' · ' + item.code);
      var remove = node('button', 'btn', '移除');
      remove.type = 'button';
      remove.setAttribute('aria-label', '移除' + item.name + ' ' + item.code);
      remove.addEventListener('click', function () { removeWatchlist(item.code); });
      row.append(label, remove);
      list.appendChild(row);
    });
    if (count) count.textContent = String(state.watchlist.length);
    filter.value = state.symbol;
    filter.disabled = state.kind !== 'watchlist' || state.watchlist.length === 0;
    if (!state.watchlist.length) {
      list.appendChild(node('li', 'news-empty', '尚未添加自选股；市场资讯可独立浏览。'));
    }
  }

  async function loadWatchlist() {
    try {
      var response = await requestJson('/api/investment-news/watchlist');
      state.watchlist = response.items || [];
      if (state.symbol && !state.watchlist.some(function (item) { return item.code === state.symbol; })) state.symbol = '';
      renderWatchlist();
    } catch (error) {
      setStatus('news-list-status', '读取自选股失败：' + error.message, 'error');
    }
  }

  function renderSourceLinks(item, parent) {
    var sources = Array.isArray(item.sources) ? item.sources : [];
    if (!sources.length) return;
    var details = node('details', 'news-item-sources');
    details.appendChild(node('summary', '', '来源链接（' + sources.length + '）'));
    sources.forEach(function (source) {
      var link = externalLink(source.url, safeText(source.source_name, '查看原文'));
      if (link) details.appendChild(link);
    });
    parent.appendChild(details);
  }

  function renderNews(data) {
    var list = byId('news-list');
    if (!list) return;
    list.replaceChildren();
    state.total = Number(data.total || 0);
    var items = Array.isArray(data.items) ? data.items : [];
    if (!items.length) {
      list.appendChild(node('li', 'news-empty', '该日期范围暂无已采集资讯。可单独点击“采集资讯”获取更新。'));
    }
    items.forEach(function (item) {
      var card = node('li', 'news-item');
      var title = externalLink(item.url, safeText(item.title, '（无标题）'), 'news-item-title');
      if (title) card.appendChild(title);
      else card.appendChild(node('p', 'news-item-title', safeText(item.title, '（无标题）')));

      var metadata = node('div', 'news-item-meta');
      metadata.appendChild(node('span', '', '来源：' + safeText(item.source_name, '未知来源')));
      metadata.appendChild(node('span', '', '发布时间：' + formatTime(item.published_at)));
      metadata.appendChild(node('span', '', '采集时间：' + formatTime(item.last_seen_at || item.collected_at)));
      if (Array.isArray(item.symbols) && item.symbols.length) {
        metadata.appendChild(node('span', '', '关联股票：' + item.symbols.join('、')));
      }
      card.appendChild(metadata);

      var tags = node('div', 'news-item-tags');
      (item.importance_tags || []).forEach(function (tag) { tags.appendChild(node('span', 'news-tag', tag)); });
      if (Number(item.related_count || 0) > 1) {
        tags.appendChild(node('span', 'news-tag', '同题报道 ' + item.related_count + ' 条；不代表独立佐证'));
      }
      if (tags.childNodes.length) card.appendChild(tags);
      if (item.snippet) card.appendChild(node('p', 'news-item-snippet', item.snippet));
      renderSourceLinks(item, card);
      list.appendChild(card);
    });

    var page = Math.floor(state.offset / PAGE_SIZE) + 1;
    var label = byId('news-page-label');
    var previous = byId('news-page-prev');
    var next = byId('news-page-next');
    if (label) label.textContent = '第 ' + page + ' 页 · 共 ' + state.total + ' 条';
    if (previous) previous.disabled = state.offset <= 0;
    if (next) next.disabled = state.offset + PAGE_SIZE >= state.total;
    var meta = byId('news-list-meta');
    if (meta) meta.textContent = '已采集 ' + state.total + ' 条 · 每页最多 ' + PAGE_SIZE + ' 条；页面分页不限制采集或综述输入。';
  }

  async function loadNews() {
    var query = new URLSearchParams({
      day: state.day,
      kind: state.kind,
      sort: state.sort,
      limit: String(PAGE_SIZE),
      offset: String(state.offset),
    });
    if (state.symbol && state.kind === 'watchlist') query.set('symbol', state.symbol);
    setStatus('news-list-status', '正在读取资讯…');
    try {
      var data = await requestJson('/api/investment-news?' + query.toString());
      renderNews(data);
      setStatus('news-list-status', '');
    } catch (error) {
      setStatus('news-list-status', '读取资讯失败：' + error.message + '；已有数据不会因来源故障而被清除。', 'error');
    }
  }

  async function loadAll() {
    await Promise.all([loadStatus(), loadWatchlist(), loadDigest(), loadNews()]);
  }

  async function pollRun(runId, kind) {
    for (var attempt = 0; attempt < 600; attempt += 1) {
      var response = await requestJson('/api/investment-news/runs?limit=100');
      var run = (response.items || []).find(function (item) { return item.run_id === runId; });
      if (run && ['succeeded', 'partial', 'failed', 'skipped'].indexOf(run.status) !== -1) return run;
      setStatus(kind === 'collection' ? 'news-collection-status' : 'news-digest-run-status',
        (kind === 'collection' ? '正在采集资讯…' : '正在生成综述…') + '（' + (attempt + 1) + '）');
      await new Promise(function (resolve) { window.setTimeout(resolve, 1000); });
    }
    throw new Error('任务仍在运行，可稍后刷新查看状态');
  }

  async function runOperation(kind) {
    var isCollection = kind === 'collection';
    var statusId = isCollection ? 'news-collection-status' : 'news-digest-run-status';
    var button = byId(isCollection ? 'news-collect' : 'news-generate-digest');
    state.busy[kind] = true;
    if (button) button.disabled = true;
    setStatus(statusId, isCollection ? '正在提交采集任务…' : '正在提交综述任务…');
    try {
      var response = await requestJson('/api/investment-news/' + (isCollection ? 'collect' : 'digest'), { method: 'POST' });
      var run = await pollRun(response.run_id, kind);
      var stateText = runStatusLabel(run.status);
      if (run.error) stateText += '：' + run.error;
      setStatus(statusId, stateText, run.status === 'failed' ? 'error' : (run.status === 'succeeded' ? 'success' : ''),
        ['succeeded', 'partial', 'failed', 'skipped'].indexOf(run.status) !== -1);
      await loadStatus();
      if (isCollection) await Promise.all([loadNews(), loadDigest(), loadWatchlist()]);
      else await loadDigest();
    } catch (error) {
      setStatus(statusId, '任务失败：' + error.message, 'error', true);
    } finally {
      state.busy[kind] = false;
      updateActionButtons();
    }
  }

  async function addWatchlist(event) {
    event.preventDefault();
    var input = byId('news-stock-input');
    var query = input ? input.value.trim() : '';
    if (!query) return;
    var code = query;
    if (!/^\d{6}$/.test(query)) {
      try {
        var suggestions = await requestJson('/api/companies?q=' + encodeURIComponent(query));
        var match = (suggestions.results || []).find(function (item) {
          return item.name === query || item.code === query;
        }) || (suggestions.results || [])[0];
        if (!match) throw new Error('没有找到匹配的 A 股代码');
        code = match.code;
      } catch (error) {
        setStatus('news-list-status', error.message, 'error');
        return;
      }
    }
    try {
      var item = await requestJson('/api/investment-news/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code }),
      });
      if (input) input.value = '';
      state.symbol = '';
      setStatus('news-list-status', '已添加 ' + item.name + '（' + item.code + '）', 'success');
      await Promise.all([loadWatchlist(), loadNews()]);
    } catch (error) {
      setStatus('news-list-status', '添加失败：' + error.message, 'error');
    }
  }

  async function removeWatchlist(code) {
    try {
      await requestJson('/api/investment-news/watchlist/' + encodeURIComponent(code), { method: 'DELETE' });
      if (state.symbol === code) state.symbol = '';
      setStatus('news-list-status', '已从共享自选股移除。', 'success');
      await Promise.all([loadWatchlist(), loadNews()]);
    } catch (error) {
      setStatus('news-list-status', '移除失败：' + error.message, 'error');
    }
  }

  function bindEvents() {
    if (state.initialized) return;
    state.initialized = true;
    var collect = byId('news-collect');
    var digest = byId('news-generate-digest');
    if (collect) collect.addEventListener('click', function () { runOperation('collection'); });
    if (digest) digest.addEventListener('click', function () { runOperation('digest'); });
    var form = byId('news-watchlist-form');
    if (form) form.addEventListener('submit', addWatchlist);
    var date = byId('news-day');
    if (date) date.addEventListener('change', function () { state.day = date.value || shanghaiToday(); state.offset = 0; loadNews(); });
    var sort = byId('news-sort');
    if (sort) sort.addEventListener('change', function () { state.sort = sort.value; state.offset = 0; loadNews(); });
    var symbol = byId('news-symbol-filter');
    if (symbol) symbol.addEventListener('change', function () { state.symbol = symbol.value; state.offset = 0; loadNews(); });
    document.querySelectorAll('[data-news-kind]').forEach(function (button) {
      button.addEventListener('click', function () {
        state.kind = button.dataset.newsKind;
        state.offset = 0;
        document.querySelectorAll('[data-news-kind]').forEach(function (tab) {
          var active = tab.dataset.newsKind === state.kind;
          tab.classList.toggle('active', active);
          tab.setAttribute('aria-pressed', String(active));
        });
        var filter = byId('news-symbol-filter');
        if (filter) filter.disabled = state.kind !== 'watchlist' || state.watchlist.length === 0;
        loadNews();
      });
    });
    var previous = byId('news-page-prev');
    if (previous) previous.addEventListener('click', function () { state.offset = Math.max(0, state.offset - PAGE_SIZE); loadNews(); });
    var next = byId('news-page-next');
    if (next) next.addEventListener('click', function () { if (state.offset + PAGE_SIZE < state.total) { state.offset += PAGE_SIZE; loadNews(); } });
    var input = byId('news-stock-input');
    if (input) input.addEventListener('input', function () {
      window.clearTimeout(state.autocompleteTimer);
      var value = input.value.trim();
      if (value.length < 1) return;
      state.autocompleteTimer = window.setTimeout(async function () {
        try {
          var response = await requestJson('/api/companies?q=' + encodeURIComponent(value));
          var datalist = byId('news-stock-suggestions');
          if (!datalist) return;
          datalist.replaceChildren();
          (response.results || []).forEach(function (item) {
            var option = node('option', '', item.name);
            option.value = item.code;
            datalist.appendChild(option);
          });
        } catch (_) { /* suggestions are optional; manual code entry remains available */ }
      }, 250);
    });
  }

  window.initInvestmentNewsPage = function () {
    var page = byId('page-investment-news');
    if (!page || !window.location.hash.startsWith(PAGE)) return;
    var date = byId('news-day');
    if (date && !date.value) date.value = shanghaiToday();
    state.day = date ? date.value : shanghaiToday();
    bindEvents();
    renderWatchlist();
    loadAll();
  };
}());
