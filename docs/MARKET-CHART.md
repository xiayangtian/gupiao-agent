# 行情图表

聊天行情图表只使用本轮 `AnswerRun` 中的合格市场 Facts，不额外请求数据；既有行情事实表与来源证据始终保留。

- **多指数行情图**：至少两个指数有两个以上共同日期时，以首个共同日期为 0% 对比区间涨跌；tooltip 提供收盘点位和变化。冲突/混周期序列不参与，缺少 Chart.js 时保留表格。
- **个股价格图**：同一证券至少两个有效收盘价采样、且 Facts 与本轮范围一致时，纵轴使用原始收盘价；tooltip 显示相对首个采样点的涨跌幅。冲突、单点或无效数据不生成图表。
- **上下文追问与搜索词溯源**：上一轮图表/来源展示追问仅依据有界的最近运行上下文回答，不触发检索或外部来源；需要新证据的问题仍按当前意图执行 WebSearch，来源详情显示脱敏后的实际搜索词。

## 设计与计划

- [多指数行情图设计](superpowers/specs/2026-10-12-multi-index-market-chart-design.md) · [实施计划](superpowers/plans/2026-10-12-multi-index-market-chart.md)
- [个股价格图设计](superpowers/specs/2026-10-12-individual-stock-price-chart-design.md) · [实施计划](superpowers/plans/2026-10-12-individual-stock-price-chart.md)
- [上下文追问与搜索词溯源设计](superpowers/specs/2026-10-12-context-aware-followup-search-audit-design.md) · [实施计划](superpowers/plans/2026-10-12-context-aware-followup-search-audit.md)

## 验证入口

- `tests/unit/test_chat_rendering_js.py`
- `tests/unit/test_chat_market_charts_js.py`
- `tests/unit/test_chat_followups.py`
- `tests/unit/test_server_api.py`
- `tests/browser/test_chat_trust_flow.py`
