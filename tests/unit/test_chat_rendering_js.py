"""智能问答 Markdown 与网页来源展示的前端回归测试。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CHAT_RENDERING_JS = ROOT / "webapp" / "static" / "chat_rendering.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="前端渲染回归测试需要 Node.js")


def _run_node(source: str) -> dict:
    completed = subprocess.run(
        [NODE, "-e", source], cwd=ROOT, check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout)


def test_chat_renderer_normalizes_html_sup_citations_to_markdown_labels():
    """模型返回 HTML/转义 HTML 上标时，聊天内容应显示为可读的 [n]。"""
    result = _run_node(
        f"""
        let rendering = {{}};
        try {{ rendering = require({json.dumps(str(CHAT_RENDERING_JS))}); }} catch (_) {{}}
        const normalize = rendering.normalizeAssistantMarkdown || (value => value);
        console.log(JSON.stringify({{
          plain: normalize('结论<sup>1</sup> 来自工具'),
          escaped: normalize('结论\\\\<sup>2\\\\</sup> 来自网页')
        }}));
        """
    )

    assert result == {"plain": "结论[1] 来自工具", "escaped": "结论[2] 来自网页"}


def test_chat_renderer_keeps_only_web_source_citation_fields():
    """网页来源的展示数据不得携带用于模型核验的长摘要正文。"""
    result = _run_node(
        f"""
        let rendering = {{}};
        try {{ rendering = require({json.dumps(str(CHAT_RENDERING_JS))}); }} catch (_) {{}}
        const references = rendering.webSourceReferences || (items => items);
        console.log(JSON.stringify(references([{{
          title: '新浪财经', url: 'https://finance.sina.com.cn/a',
          content: '这段摘要只供模型核验，不能展示给用户。', published_date: '2026-09-04'
        }}])));
        """
    )

    assert result == [{
        "title": "新浪财经", "url": "https://finance.sina.com.cn/a", "published_date": "2026-09-04",
    }]
