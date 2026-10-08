from webapp.chat_models import Scope
from datetime import datetime
from zoneinfo import ZoneInfo

from webapp.execution_executor import ExecutionExecutor, a_share_indices_handler, company_kline_handler
from webapp.execution_plan import ExecutionPlan, validate_execution_plan
from webapp.chat_time import resolve_market_window


class FakeTencent:
    def __init__(self):
        self.calls = []

    def kline(self, symbol, *, period, count, adjust):
        self.calls.append((symbol, period, count, adjust))
        return [{"date": "2026-09-20", "close": 3000.0}]


def _plan():
    return ExecutionPlan.from_dict({
        "objective": "上周 A 股复盘", "source_mode": "market_recap",
        "steps": [
            {"id": "indices", "kind": "market_indices", "required": True},
            {"id": "breadth", "kind": "market_breadth", "required": False},
            {"id": "answer", "kind": "answer", "depends_on": ["indices", "breadth"]},
        ], "acceptance": ["显示指数与数据限制"],
    })


def test_weekly_recap_uses_tencent_weekly_indices_without_rag_or_web():
    source = FakeTencent()
    window = resolve_market_window(
        "上周 A 股复盘", datetime(2026, 9, 24, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    result = ExecutionExecutor(market_indices=a_share_indices_handler(source, window)).execute(
        _plan(), "上周 A 股复盘", Scope.whole_corpus(),
    )
    assert {call[1] for call in source.calls} == {"day"}
    assert all(call[2] >= 5 for call in source.calls)
    assert result.source_summary == {"local_pdf": "未使用", "market_data": "已使用", "web": "未使用"}
    assert result.steps[0].value["window"] == "上周"
    assert result.steps[0].value["indices"]["sh000001"]["status"] == "partial"
    assert result.steps[1].status == "unavailable"


def test_market_recap_plan_accepts_available_indices_and_no_rag():
    plan = _plan()
    valid, issues = validate_execution_plan(plan, Scope.whole_corpus(), {"market_indices", "market_breadth"}, 2)
    assert valid == plan
    assert issues == ()


class FakeMarketMcp:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments, timeout):
        self.calls.append((name, arguments, timeout))
        return '{"as_of":"2026-09-23"}'


def test_market_overview_aggregates_bounded_mcp_calls_without_model_parameters():
    from webapp.execution_executor import market_overview_handler

    plan = ExecutionPlan.from_dict({
        "objective": "上周 A 股复盘", "source_mode": "market_recap",
        "steps": [{"id": "overview", "kind": "market_overview", "required": True}, {"id": "answer", "kind": "answer"}],
        "acceptance": ["as_of"],
    })
    source = FakeMarketMcp()
    window = resolve_market_window(
        "上周 A 股复盘", datetime(2026, 9, 24, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    result = ExecutionExecutor(market_overview=market_overview_handler(source, timeout=12, window=window)).execute(
        plan, "上周 A 股复盘", Scope.whole_corpus(),
    )

    assert [name for name, _, _ in source.calls] == [
        "stock_zt_pool", "stock_zt_pool", "stock_sector_fund_flow_rank",
    ]
    assert all(timeout == 12 for _, _, timeout in source.calls)
    assert result.steps[0].status == "completed"
    assert result.steps[0].value["window"] == "上周"
    assert result.steps[0].value["artifacts"][0]["window_match"] is False
    assert result.steps[0].value["artifacts"][0]["status"] == "success"


def test_market_overview_keeps_successful_artifact_when_other_calls_fail():
    from webapp.execution_executor import market_overview_handler

    class PartialMcp:
        def __init__(self):
            self.calls = 0

        def call_tool(self, name, arguments, timeout):
            self.calls += 1
            if self.calls == 1:
                return '{"data":[{"date":"2026-09-29"}]}'
            raise RuntimeError("provider private failure")

    window = resolve_market_window(
        "上周A股复盘", datetime(2026, 9, 24, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    value = market_overview_handler(PartialMcp(), timeout=12, window=window)(
        "上周A股复盘", Scope.whole_corpus(),
    )

    assert len(value["artifacts"]) == 3
    assert value["artifacts"][0]["status"] == "success"
    assert all(item["status"] == "failed" for item in value["artifacts"][1:])
    assert all(item.get("error") == "来源调用失败" for item in value["artifacts"][1:])
    assert all(item["window_match"] is False for item in value["artifacts"])
    assert all("provider private failure" not in str(item) for item in value["artifacts"])


def test_company_kline_filters_actual_bars_to_frozen_market_window():
    class MultiDayTencent:
        def __init__(self):
            self.calls = []

        def kline(self, symbol, *, period, count, adjust):
            self.calls.append((symbol, period, count, adjust))
            return [
                {"date": day, "close": index}
                for index, day in enumerate((
                    "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
                    "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24",
                ))
            ]

    source = MultiDayTencent()
    now = datetime(2026, 9, 24, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    window = resolve_market_window("近五交易日股价走势", now)
    value = company_kline_handler(source, window)(
        "近五交易日股价走势", Scope.company_only("600900", "长江电力"),
    )

    assert source.calls[0][0] == "600900"
    assert source.calls[0][1] == "day"
    assert source.calls[0][2] >= 5
    assert value["covered_dates"] == [
        "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24",
    ]
    assert value["status"] == "complete"
