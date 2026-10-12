# 实施计划：外部参考折叠与分组展示

关联设计：[ref-display](../specs/2026-10-12-ref-display.md)  
分支：`feat/external-reference-display`

## 步骤

1. 在 `tests/unit/test_chat_rendering_js.py` 先添加渲染回归：默认折叠、事实关联来源、同来源归并、不同时间/状态隔离、未关联来源、20 条阈值、文本转义及原始摘要不泄露；运行并确认新断言按预期失败。
2. 在 `webapp/static/chat_rendering.js` 基于稳定 evidence identity 分组外部 Fact 与 ToolArtifact；保留 PDF/冲突即时呈现，将结构化事实表与原有来源链接放入一个默认折叠区。
3. 给每个来源组增加 20 条默认展示及原生“查看更多”折叠；保留不同 `as_of` 和不同状态。
4. 在 `webapp/static/style.css` 增加窄屏字段卡片布局、桌面表格、可见焦点与安全换行，不引入依赖。
5. 运行渲染测试及直接相关浏览器/服务回归；启动本地 Web 应用，执行 browser QA 的 desktop/tablet/mobile 检查，确认控制台/页面错误、失败网络请求和移动端页面横向溢出。
6. `git diff --check`，更新功能目录及方案台账，验证后合入本地 `main`。

## 验收结果记录

实现前基线：`python3 -m pytest -q tests/unit/test_chat_rendering_js.py` → 28 passed。最终验证命令和结果在完成时追加。
