"""Isolated P2 acceptance app: local deterministic planner/model/market, no external calls."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import visual_test_app  # noqa: E402
import webapp.server as server  # noqa: E402
from financial_report_fetcher.rag.qa import RagQA  # noqa: E402
from webapp.execution_plan import ExecutionPlan  # noqa: E402
from webapp.execution_planner import PlanningResult  # noqa: E402


class _NoStore:
    def query(self, *_args, **_kwargs):
        raise AssertionError("P2 fixture must not query the real vector store")


class _AnswerAI:
    def chat_stream(self, *, messages, system=None, tools=None, **_kwargs):
        assert tools is None
        if "市盈率" in messages[-1]["content"]:
            answer = "市盈率表示价格与每股收益的比值。"
        else:
            context = "\n".join(str(item.get("content", "")) for item in messages)
            if "stock_zt_pool" in context:
                assert "请求上限 50 条" in context and "总量未知" in context
                answer = "仅展示所请求窗口内的数据；涨跌停池返回上限为 50 条、总量未知，行业资金流不等于全市场资金流。"
            else:
                answer = "仅展示所请求窗口内的实际行情日期，不补造交易日。"
        yield {"type": "done", "answer": answer, "model": "p2-local-fixture", "usage": {}}


class _FixedPlanner:
    def __init__(self, _json_planner):
        pass

    def plan(self, question, *_args, **_kwargs):
        if "复盘" in question:
            kinds = [("indices", "market_indices"), ("overview", "market_overview")]
            mode = "market_recap"
        else:
            kinds = [("kline", "market_kline")]
            mode = "external_market"
        return PlanningResult("validated", ExecutionPlan.from_dict({
            "objective": "P2 隔离验收", "source_mode": mode,
            "steps": [{"id": ident, "kind": kind} for ident, kind in kinds] +
                     [{"id": "answer", "kind": "answer", "depends_on": [ident for ident, _ in kinds]}],
            "acceptance": ["来源日期与覆盖可复核"],
        }))


class _OfflineWeb:
    def __init__(self, *, timeout):
        self.available = False


class _FixtureIndex(visual_test_app._FakeStockIndex):
    def start(self):
        return None

    def wait_ready(self, timeout=5.0):
        return True

    def is_valid_code(self, code):
        return isinstance(code, str) and code in {"600519", "601288", "600036"}

    def search(self, text, limit=10):
        return [{"code": "600519", "name": "贵州茅台"}] if "600519" in text else []

    def company_name(self, code):
        return "贵州茅台" if code == "600519" else super().company_name(code)


def build_app():
    app = visual_test_app.build_app()
    server.stock_index = _FixtureIndex()
    server.ExecutionPlanner = _FixedPlanner
    server.rag_qa = RagQA(_NoStore(), _AnswerAI())
    server.TavilyWebSearch = _OfflineWeb
    cfg = server.RagConfig.load()
    cfg.mcp_tools = True
    cfg.mcp_max_tool_calls = 9
    server.RagConfig.load = staticmethod(lambda: cfg)
    server._mcp_tool_defs_cache = [
        {"function": {"name": "stock_zt_pool"}},
        {"function": {"name": "stock_sector_fund_flow_rank"}},
    ]
    server._build_chat_tool_defs = lambda _cfg: server._mcp_tool_defs_cache
    server._build_chat_tool_executor = lambda *_args, **_kwargs: lambda *_a, **_kw: ""
    server.mcp_breaker = type("Breaker", (), {"allow": lambda self: True})()
    calls: list[str] = []

    def kline(symbol, *, period, count, adjust):
        calls.append("kline:" + symbol)
        return [{"date": "2026-09-24", "close": 10.5}]

    def mcp(name, arguments, *, timeout, retry):
        calls.append("mcp:" + name)
        rows = [{"date": "2026-09-24", "symbol": f"600{i:03d}"} for i in range(50)] if name == "stock_zt_pool" else [{"date": "2026-09-24", "sector": "银行"}]
        return {"as_of": "2026-09-24", "data": rows}

    server.tencent_quote.kline = kline
    server.market_data_mcp.call_tool = mcp
    app.state.p2_fixture_calls = calls

    @app.get("/_fixture/p2-calls")
    def fixture_calls():
        return list(calls)

    return app


def main():
    import uvicorn
    uvicorn.run(build_app(), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
