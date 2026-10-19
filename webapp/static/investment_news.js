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
    digestDay: '',
    availableDigestDays: [],
    marketDay: 'today',
    sort: 'importance',
    symbol: '',
    rangeDays: 1,
    offset: 0,
    total: 0,
    watchlist: [],
    status: null,
    autocompleteTimer: null,
    observedRunStatuses: null,
    scheduledPollTimer: null,
    scheduledPollInFlight: false,
    scheduledPollUntil: 0,
    visibilityListenerBound: false,
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

  function shiftDay(value, offset) {
    var parts = value.split('-').map(Number);
    return new Date(Date.UTC(parts[0], parts[1] - 1, parts[2] + offset)).toISOString().slice(0, 10);
  }

  function isShanghaiMonday(day) {
    var parts = day.split('-').map(Number);
    return new Date(Date.UTC(parts[0], parts[1] - 1, parts[2])).getUTCDay() === 1;
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
      digest.textContent = state.busy.digest || activeDigest ? '生成中…' :
        (isShanghaiMonday(shanghaiToday()) ? '生成周一综述（含周末）' : '生成今日综述');
    }
  }

  async function loadStatus() {
    try {
      state.status = await requestJson('/api/investment-news/status');
      if (state.observedRunStatuses === null) state.observedRunStatuses = runStatusSnapshot(state.status.recent_runs);
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

  function runStatusSnapshot(runs) {
    var snapshot = {};
    (Array.isArray(runs) ? runs : []).forEach(function (run) {
      if (run && run.run_id) snapshot[run.run_id] = run.status;
    });
    return snapshot;
  }

  function isAutomatedRun(run) {
    return run && ['scheduled', 'startup_catchup'].indexOf(run.trigger) !== -1;
  }

  function hasActiveRuns(status) {
    return Boolean(status && (status.active_collection || status.active_digest));
  }

  function applyLatestCollectionDay() {
    var latestDay = state.status && state.status.latest_news_day;
    var today = shanghaiToday();
    if (latestDay === today) state.marketDay = 'today';
    else if (latestDay === shiftDay(today, -1)) state.marketDay = 'yesterday';
    updateDateShortcuts();
  }

  async function refreshForAutomatedRuns(runs) {
    var collectionRuns = runs.filter(function (run) { return run.kind === 'collection'; });
    var digestRuns = runs.filter(function (run) { return run.kind === 'digest'; });
    var digestRefreshed = false;
    if (collectionRuns.some(function (run) { return ['succeeded', 'partial'].indexOf(run.status) !== -1; })) {
      applyLatestCollectionDay();
      digestRefreshed = state.digestDay === shanghaiToday();
      await Promise.all([loadNews(), digestRefreshed ? loadDigest() : Promise.resolve()]);
      setStatus('news-collection-status', '定时采集完成，资讯已更新。', 'success', true);
    } else if (collectionRuns.some(function (run) { return run.status === 'failed'; })) {
      var failedCollection = collectionRuns.find(function (run) { return run.status === 'failed'; });
      setStatus('news-collection-status', '定时采集失败：' + (failedCollection.error || '请查看运行记录。'), 'error', true);
    }
    var successfulDigests = digestRuns.filter(function (run) {
      return run.status === 'succeeded' && run.result && run.result.day;
    });
    successfulDigests.sort(function (a, b) {
      return Date.parse(a.finished_at || '') - Date.parse(b.finished_at || '');
    });
    if (successfulDigests.length) {
      var latestDigestRun = successfulDigests[successfulDigests.length - 1];
      var latestDigestDay = latestDigestRun.result.day;
      var dayChanged = latestDigestDay !== state.digestDay;
      await loadDigestDays(latestDigestDay);
      if (!digestRefreshed || dayChanged) await loadDigest();
      digestRefreshed = true;
      setStatus('news-digest-run-status', '定时综述已更新并切换至 ' + latestDigestDay + '。', 'success', true);
    }
    var failedDigestRuns = digestRuns.filter(function (run) { return run.status === 'failed'; });
    failedDigestRuns.sort(function (a, b) {
      return Date.parse(a.finished_at || '') - Date.parse(b.finished_at || '');
    });
    if (failedDigestRuns.length) {
      var failedDigestRun = failedDigestRuns[failedDigestRuns.length - 1];
      setStatus('news-digest-run-status', '定时综述失败：' + (failedDigestRun.error || '请查看运行记录。'), 'error', true);
    }
  }

  async function pollAutomatedUpdates(force) {
    if (!window.location.hash.startsWith(PAGE) || document.visibilityState !== 'visible' || state.scheduledPollInFlight) return;
    var inScheduleWindow = window.InvestmentNewsPolling &&
      window.InvestmentNewsPolling.isWithinScheduleWindow(new Date());
    if (!force && !inScheduleWindow && !hasActiveRuns(state.status) && Date.now() > state.scheduledPollUntil) return;
    state.scheduledPollInFlight = true;
    var previous = state.observedRunStatuses || {};
    try {
      await loadStatus();
      var runs = state.status && Array.isArray(state.status.recent_runs) ? state.status.recent_runs : [];
      var changedRuns = runs.filter(function (run) {
        return isAutomatedRun(run) && ['succeeded', 'partial', 'failed', 'skipped'].indexOf(run.status) !== -1 &&
          previous[run.run_id] !== run.status;
      });
      state.observedRunStatuses = runStatusSnapshot(runs);
      if (hasActiveRuns(state.status)) {
        if (state.status.active_collection && isAutomatedRun(state.status.active_collection)) {
          setStatus('news-collection-status', '定时采集运行中…');
        }
        if (state.status.active_digest && isAutomatedRun(state.status.active_digest)) {
          setStatus('news-digest-run-status', '定时综述运行中…');
        }
      }
      if (changedRuns.length) {
        state.scheduledPollUntil = Date.now() + 5 * 60 * 1000;
        await refreshForAutomatedRuns(changedRuns);
      }
    } catch (error) {
      setStatus('news-status', '自动更新检查失败：' + error.message, 'error');
    } finally {
      state.scheduledPollInFlight = false;
    }
  }

  function startAutomatedUpdatePolling() {
    if (state.scheduledPollTimer === null) {
      var interval = window.InvestmentNewsPolling ? window.InvestmentNewsPolling.intervalMs : 30000;
      state.scheduledPollTimer = window.setInterval(function () { pollAutomatedUpdates(false); }, interval);
    }
    if (!state.visibilityListenerBound) {
      state.visibilityListenerBound = true;
      document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') pollAutomatedUpdates(true);
      });
    }
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

  async function loadDigestDays(preferredDay) {
    var selector = byId('news-digest-day');
    if (!selector) return;
    try {
      var response = await requestJson('/api/investment-news/digest/days');
      state.availableDigestDays = Array.isArray(response.days) ? response.days.filter(function (day) {
        return typeof day === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(day);
      }) : [];
      selector.replaceChildren();
      state.availableDigestDays.forEach(function (day) {
        var label = day === shanghaiToday() ? day + '（今天）' : day;
        var option = node('option', '', label);
        option.value = day;
        selector.appendChild(option);
      });
      selector.disabled = state.availableDigestDays.length === 0;
      var selected = state.availableDigestDays.indexOf(preferredDay) !== -1 ? preferredDay :
        state.availableDigestDays.indexOf(state.digestDay) !== -1 ? state.digestDay :
          (state.availableDigestDays[0] || '');
      selector.value = selected;
      state.digestDay = selected;
    } catch (error) {
      state.availableDigestDays = [];
      state.digestDay = '';
      selector.replaceChildren();
      selector.disabled = true;
      setStatus('news-status', '读取已有综述日期失败：' + error.message, 'error');
    }
  }

  async function loadDigest() {
    var content = byId('news-digest');
    if (!content) return;
    content.replaceChildren();
    var digestTitle = byId('news-digest-title');
    var badge = byId('news-digest-badge');
    var meta = byId('news-digest-meta');
    if (!state.digestDay) {
      if (digestTitle) digestTitle.textContent = 'AI 综述';
      if (badge) { badge.textContent = '暂无可用日期'; badge.className = 'badge badge-info'; }
      if (meta) meta.textContent = '日期筛选仅显示已生成综述的日期。';
      content.appendChild(node('p', 'news-empty', '尚无已生成的综述。生成后可在日期列表中查看。'));
      return;
    }
    try {
      var response = await requestJson('/api/investment-news/digest?day=' + encodeURIComponent(state.digestDay));
      var digest = response.digest;
      if (!digest) {
        if (digestTitle) digestTitle.textContent = state.digestDay === shanghaiToday() ? '今日 AI 综述' : state.digestDay + ' AI 综述';
        if (badge) { badge.textContent = '尚未生成'; badge.className = 'badge badge-info'; }
        content.appendChild(node('p', 'news-empty', state.digestDay + ' 的综述尚未生成。手动生成只读取已入库资讯，不会触发采集。'));
        return;
      }
      if (badge) {
        badge.textContent = response.stale ? '输入已变化' : '已生成';
        badge.className = 'badge ' + (response.stale ? 'badge-warn' : 'badge-ok');
      }
      var payload = digest.payload || {};
      var hasCoverage = Boolean(payload.coverage_start_day && payload.coverage_end_day);
      var coverageStart = payload.coverage_start_day || state.digestDay;
      var coverageEnd = payload.coverage_end_day || state.digestDay;
      var includesWeekend = hasCoverage && coverageStart < coverageEnd;
      var legacyMonday = isShanghaiMonday(state.digestDay) && !hasCoverage;
      if (digestTitle) {
        var todayTitle = state.digestDay === shanghaiToday() ? '今日 AI 综述' : state.digestDay + ' AI 综述';
        digestTitle.textContent = includesWeekend ? todayTitle + '（含周末）' :
          (legacyMonday ? todayTitle + '（需重生成以纳入周末）' : todayTitle);
      }
      var coverageLabel = coverageStart === coverageEnd ? '' : ' · 覆盖 ' + coverageStart + ' 至 ' + coverageEnd;
      if (legacyMonday) coverageLabel += ' · 旧版单日综述尚未纳入周末';
      if (meta) {
        meta.textContent = '生成于 ' + formatTime(digest.generated_at) + ' · 覆盖 ' + digest.input_count +
          ' 条资讯 · ' + digest.source_count + ' 个来源链接' + coverageLabel + ' · 数据截至 ' + formatTime(digest.data_through);
      }
      if (Array.isArray(payload.weekend_review) && payload.weekend_review.length) {
        appendDigestSection(content, '周末重点回顾', payload.weekend_review);
      } else if (isShanghaiMonday(state.digestDay)) {
        var weekendSection = node('section', 'news-digest-section');
        weekendSection.appendChild(node('h3', '', '周末重点回顾'));
        var weekendCount = Number(payload.weekend_input_count || 0);
        var weekendMessage = legacyMonday
          ? '当前为旧版单日综述，尚未纳入周末；请重新生成以查看周末分析。'
          : weekendCount
            ? '周末资讯已入库，但未生成带有效来源引用的周末要点。'
            : '周六、周日暂无已入库资讯，无法分析周末情况。';
        weekendSection.appendChild(node('p', 'news-empty', weekendMessage));
        content.appendChild(weekendSection);
      }
      appendDigestSection(content, '市场主题', payload.market_themes);
      appendDigestSection(content, '政策与监管', payload.policy_regulatory);
      appendDigestSection(content, '自选股重点', payload.watchlist);
      appendDigestSection(content, '风险与不确定性', payload.risks);
      if (!content.childNodes.length) content.appendChild(node('p', 'news-empty', '本次综述没有生成带有效来源引用的要点。'));
    } catch (error) {
      setStatus('news-digest-run-status', '读取综述失败：' + error.message, 'error');
    }
  }

  function updateSymbolFilterAvailability() {
    var filter = byId('news-symbol-filter');
    var label = byId('news-symbol-filter-label');
    var help = byId('news-symbol-filter-help');
    if (!filter) return;
    var watchlistMode = state.kind === 'watchlist';
    filter.disabled = !watchlistMode || state.watchlist.length === 0;
    if (label) label.textContent = watchlistMode ? '股票筛选' : '股票筛选（市场资讯不可用）';
    var field = filter.closest('.news-control-field');
    if (field) field.classList.toggle('news-control-field--unavailable', filter.disabled);
    filter.setAttribute('aria-label', watchlistMode ? '按自选股筛选' : '股票筛选，仅自选股资讯可用');
    if (help) {
      help.textContent = !watchlistMode
        ? '市场资讯没有个股关联，切换至“自选股资讯”后可按股票筛选。'
        : state.watchlist.length
          ? '可按当前共享自选股筛选。'
          : '添加共享自选股后可按股票筛选。';
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
    updateSymbolFilterAvailability();
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
    if (meta) {
      var rangeLabel = data.start_day && data.end_day ? ' · 日期：' + data.start_day + ' 至 ' + data.end_day : '';
      meta.textContent = '已采集 ' + state.total + ' 条' + rangeLabel + ' · 每页最多 ' + PAGE_SIZE + ' 条；页面分页不限制采集或综述输入。';
    }
  }

  function updateDateShortcuts() {
    var mondayToday = isShanghaiMonday(shanghaiToday());
    var marketToday = document.querySelector('[data-news-window="market-today"]');
    var watchlistToday = document.querySelector('[data-news-window="watchlist-today"]');
    if (marketToday) marketToday.textContent = mondayToday ? '周末+今天' : '今天';
    if (watchlistToday) watchlistToday.textContent = mondayToday ? '周末+今日' : '今日';
    var marketControls = byId('news-market-day-controls');
    var watchlistControls = byId('news-watchlist-day-controls');
    if (marketControls) marketControls.classList.toggle('hidden', state.kind !== 'market');
    if (watchlistControls) watchlistControls.classList.toggle('hidden', state.kind !== 'watchlist');
    document.querySelectorAll('[data-news-window]').forEach(function (button) {
      var windowName = button.dataset.newsWindow;
      var pressed = windowName === 'market-' + state.marketDay ||
        (windowName === 'watchlist-today' && state.rangeDays === 1) ||
        (windowName === 'watchlist-' + state.rangeDays && state.rangeDays > 1);
      button.setAttribute('aria-pressed', String(pressed));
    });
  }

  async function loadNews() {
    var query = new URLSearchParams({
      kind: state.kind,
      sort: state.sort,
      limit: String(PAGE_SIZE),
      offset: String(state.offset),
    });
    var today = shanghaiToday();
    var todaySelected = state.kind === 'market' ? state.marketDay === 'today' : state.rangeDays === 1;
    if (state.kind === 'watchlist' && state.rangeDays > 1) {
      query.set('start_day', shiftDay(today, 1 - state.rangeDays));
      query.set('end_day', today);
    } else if (todaySelected && isShanghaiMonday(today)) {
      query.set('start_day', shiftDay(today, -2));
      query.set('end_day', today);
    } else {
      query.set('day', state.kind === 'market' && state.marketDay === 'yesterday' ? shiftDay(today, -1) : today);
    }
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
    await Promise.all([loadStatus(), loadWatchlist(), loadNews()]);
    await loadDigestDays();
    await loadDigest();
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
      if (!isCollection && run.result && run.result.day) {
        state.digestDay = run.result.day;
      }
      setStatus(statusId, stateText, run.status === 'failed' ? 'error' : (run.status === 'succeeded' ? 'success' : ''),
        ['succeeded', 'partial', 'failed', 'skipped'].indexOf(run.status) !== -1);
      await loadStatus();
      if (isCollection && ['succeeded', 'partial'].indexOf(run.status) !== -1 && state.status && state.status.latest_news_day) {
        applyLatestCollectionDay();
        state.offset = 0;
      }
      if (isCollection) await Promise.all([loadNews(), loadDigest(), loadWatchlist()]);
      else {
        await loadDigestDays(run.result && run.result.day);
        await loadDigest();
      }
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
    var digestDate = byId('news-digest-day');
    if (digestDate) digestDate.addEventListener('change', function () {
      state.digestDay = digestDate.value || '';
      loadDigest();
    });
    document.querySelectorAll('[data-news-window]').forEach(function (button) {
      button.addEventListener('click', function () {
        var windowName = button.dataset.newsWindow;
        if (windowName === 'market-today' || windowName === 'market-yesterday') {
          state.marketDay = windowName.slice('market-'.length);
        } else {
          state.rangeDays = windowName === 'watchlist-today' ? 1 : Number(windowName.slice('watchlist-'.length));
        }
        updateDateShortcuts();
        state.offset = 0;
        loadNews();
      });
    });
    var sort = byId('news-sort');
    if (sort) sort.addEventListener('change', function () { state.sort = sort.value; state.offset = 0; loadNews(); });
    var symbol = byId('news-symbol-filter');
    if (symbol) symbol.addEventListener('change', function () { state.symbol = symbol.value; state.offset = 0; loadNews(); });
    document.querySelectorAll('[data-news-kind]').forEach(function (button) {
      button.addEventListener('click', function () {
        state.kind = button.dataset.newsKind;
        state.offset = 0;
        updateDateShortcuts();
        document.querySelectorAll('[data-news-kind]').forEach(function (tab) {
          var active = tab.dataset.newsKind === state.kind;
          tab.classList.toggle('active', active);
          tab.setAttribute('aria-pressed', String(active));
        });
        updateSymbolFilterAvailability();
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
    state.digestDay = '';
    state.availableDigestDays = [];
    bindEvents();
    updateDateShortcuts();
    renderWatchlist();
    loadAll().finally(startAutomatedUpdatePolling);
  };
}());
