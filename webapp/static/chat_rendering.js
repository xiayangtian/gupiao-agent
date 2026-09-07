/* 智能问答展示辅助：将模型输出规范成安全、简洁的 UI 数据。 */
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

  return {
    normalizeAssistantMarkdown: normalizeAssistantMarkdown,
    webSourceReferences: webSourceReferences,
  };
}));
