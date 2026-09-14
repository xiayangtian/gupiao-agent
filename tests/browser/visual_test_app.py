"""浏览器验收专用启动器：真实 FastAPI 应用，但关闭 RAG 摄取与外部行情请求。

验收目标是前端可视化生命周期与可信问答的端到端契约，与真实 RAG 检索无关。
真实 PDF 摄取会在应用关闭时长时间等待后台线程，使验收结果依赖开发者本地的
``reports/`` 内容；外部行情接口则让测试依赖网络。两者都在这里移除，应用本身
仍是 ``webapp.server:app``。

可信问答回归（tests/browser/test_chat_trust_flow.py）需要一个可控的
``rag_qa.answer_stream`` 来产出确定性的 Scope/PDF 证据/网页证据/停止运行，
避免消耗模型配额或依赖真实网络。因此这里注入：

- ``_FakeRagStore``：提供固定本地报告身份，供 Scope 解析与同业样本判定；
- ``_FakeStockIndex`` / ``_FakeStockMcp``：固定公司名与行业分类，杜绝联网；
- ``_FakeRagQA``：确定性 ``answer_stream``；问题以「停止」结尾时不出 done，
  让服务端按 ``stopped`` 持久化，而不是伪装完整；
- 固定 reports/analysis fixture（含第 40 页来源 PDF），并重定向
  ``REPORTS_DIR``/``ANALYSIS_DIR``，保证 PDF 跳页 URL 指向真实存在的本地 PDF。

每个进程启动都创建独立临时目录与 ``ChatStore``，避免跨测试/历史运行残留。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile

import requests


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webapp.server as server  # noqa: E402  （必须先定位仓库根目录再导入）
from webapp.chat_store import ChatStore  # noqa: E402


FIXTURE_REPORT_ID = "601288:2026-06-30:semi_annual"
FIXTURE_PEER_REPORT_ID = "600036:2026-06-30:semi_annual"
FIXTURE_PDF_FILENAME = "农业银行_601288_半年报_2026.pdf"

# 非空的最小 PDF 占位文件：历史 PDF 路由只校验文件存在与非空，浏览器测试不渲染其内容。
_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"% trusted-chat-browser fixture\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
    b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
)


class _OfflineDatasource:
    """外部行情查询立即失败，使 PDF 端点快速返回错误而不是联网下载。"""

    def fetch_reports(self, **kwargs):
        raise requests.exceptions.RequestException("浏览器验收不访问外部数据源")


class _FakeRagStore:
    """提供固定本地报告身份：目标报告 + 一家同行业 peer，供 Scope 解析使用。"""

    def list_report_ids(self):
        return [FIXTURE_REPORT_ID, FIXTURE_PEER_REPORT_ID]


class _FakeStockIndex:
    """固定公司名映射；``company_name`` 决定 Scope 首部的公司展示。"""

    _names = {"601288": "农业银行", "600036": "招商银行"}
    is_ready = True

    def company_name(self, code):
        return self._names.get(code)


class _FakeStockMcp:
    """固定行业分类；``get_stock_basic_info`` 只返回「银行业」，绝不联网。"""

    def call_tool(self, name, arguments=None, timeout=None):
        if name == "get_stock_basic_info":
            return json.dumps({"industry": "银行业"})
        return json.dumps({})


class _FakeRagQA:
    """可控 RAG 问答替身：产出确定性的 Scope 证据/网页来源/停止运行事件。

    约定：问题以「停止」结尾时不产出 ``done``，仅产出部分 ``delta`` 后返回，
    服务端因此按 ``stopped`` 持久化（绝不伪装完整）。其余问题产出 completed
    运行，携带第 40 页 PDF 引用与一个网页来源。
    """

    def answer_stream(
        self,
        question,
        history=None,
        filters=None,
        tools=None,
        priority_report_id=None,
        scope=None,
    ):
        question = str(question or "").strip()
        if question.endswith("停止"):
            yield {"type": "delta", "text": "经营活动现金流量净额为 -621.69 亿元"}
            yield {"type": "delta", "text": "（最后一步尚未完成）"}
            return

        yield {"type": "delta", "text": "经营活动现金流量净额为 -621.69 亿元"}
        yield {
            "type": "done",
            "answer": "经营活动现金流量净额为 -621.69 亿元，主要受客户贷款及垫款净增加影响。",
            "citations": [
                {
                    "source": "pdf",
                    "report_id": FIXTURE_REPORT_ID,
                    "section": "现金流量表",
                    "page": 40,
                    "snippet": "经营活动产生的现金流量净额 -621.69 亿元",
                }
            ],
            "web_sources": [
                {
                    "url": "https://www.abchina.com/cn/announcement/2026-semi",
                    "title": "农业银行 2026 年半年度报告",
                    "published_date": "2026-08-31",
                    "content": "农业银行发布 2026 年半年度报告",
                }
            ],
            "tools_used": [],
            "retrieval_report_ids": [FIXTURE_REPORT_ID],
            "retrieval_degraded": False,
            "model": "browser-acceptance-fake",
        }


def build_app():
    """返回关闭 RAG 摄取/外部数据源、注入可控 fake RAG 的实时应用。"""
    tmp_dir = tempfile.mkdtemp(prefix="trusted-chat-browser-")
    atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)
    reports_dir = os.path.join(tmp_dir, "reports")
    analysis_dir = os.path.join(reports_dir, "analysis")
    os.makedirs(analysis_dir, exist_ok=True)
    with open(os.path.join(reports_dir, FIXTURE_PDF_FILENAME), "wb") as handle:
        handle.write(_MINIMAL_PDF)

    server.rag_service = None                       # 不触发真实 RAG 摄取
    server.rag_store = _FakeRagStore()              # 固定本地报告身份
    server.rag_qa = _FakeRagQA()                    # 可控 answer_stream
    server.stock_index = _FakeStockIndex()          # 固定公司名
    server.stock_mcp = _FakeStockMcp()              # 固定行业分类
    server.ai_client.api_key = "browser-acceptance"  # _require_ai 通过；fake 不真正调模型
    server.datasource = _OfflineDatasource()
    server.REPORTS_DIR = reports_dir
    server.ANALYSIS_DIR = analysis_dir
    server.chat_store = ChatStore(os.path.join(tmp_dir, "chat_sessions.json"))
    return server.app


def main() -> None:
    import uvicorn

    uvicorn.run(build_app(), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
