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

实现前基线：`python3 -m pytest -q tests/unit/test_chat_rendering_js.py` → 28 passed。

## 实施与验证记录

- 外部事实改为默认折叠并按 provider/tool/as_of/status 分组；用来源调用 ID 与 evidence IDs 匹配事实，未匹配项明确独立分组。冲突和本地 PDF 事实仍即时可见。
- 每组用语义化表格显示对象、指标、期间、数值/单位；每组初始 20 条，其余原生折叠“查看更多”；不渲染原始参数或结果摘要。窄屏表格换行布局，不增加页面横向宽度。
- `python3 -m pytest -q tests/unit/test_chat_rendering_js.py tests/unit/test_design_registry.py` → 43 passed；`node --check webapp/static/chat_rendering.js`、`git diff --check` 通过。
- Browser 单场景 `test_chat_source_status_and_time_are_visible_in_browser`（网页来源失败场景）→ 1 passed；布局浏览器回归 desktop/tablet/mobile → 3 passed。
- Browser QA：`http://127.0.0.1:8127`，桌面/平板/手机；无 console/page errors，无失败请求，只有既有 `favicon.ico` 404。用 25 条事实的本地渲染 fixture 检查默认折叠、20+5 展开、来源合组及浮点格式；页面宽度在 1440/834/390px 均无横向溢出，手机表格自身宽度 272px。
- 已知非本次回归：`tests/browser/test_chat_source_runtime.py` 的另两个终态断言在基线 `main` `43db423` 也失败；`test_chat_history_reopens_with_scope_and_pdf_page_link` 的来源标题编码断言在相同基线上也失败。当前版本的网页来源分组场景通过；未扩大范围修改上述既有问题。
