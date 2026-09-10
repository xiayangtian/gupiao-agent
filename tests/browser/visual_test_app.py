"""浏览器验收专用启动器：真实 FastAPI 应用，但关闭 RAG 摄取与外部行情请求。

验收目标是前端可视化生命周期，与 RAG 检索无关。真实 PDF 摄取会在应用关闭时
长时间等待后台线程，使验收结果依赖开发者本地的 ``reports/`` 内容；外部行情接口
则让测试依赖网络。两者都在这里移除，应用本身仍是 ``webapp.server:app``。
"""

from __future__ import annotations

import os
import sys

import requests


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webapp.server as server  # noqa: E402  （必须先定位仓库根目录再导入）


class _OfflineDatasource:
    """外部行情查询立即失败，使 PDF 端点快速返回错误而不是联网下载。"""

    def fetch_reports(self, **kwargs):
        raise requests.exceptions.RequestException("浏览器验收不访问外部数据源")


def build_app():
    """返回关闭 RAG 摄取与外部数据源的实时应用。"""
    server.rag_store = None
    server.rag_service = None
    server.rag_qa = None
    server.datasource = _OfflineDatasource()
    return server.app


def main() -> None:
    import uvicorn

    uvicorn.run(build_app(), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
