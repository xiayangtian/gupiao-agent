from webapp.chat_models import Scope
from webapp.execution_executor import ExecutionExecutor, a_share_indices_handler
from webapp.execution_plan import ExecutionPlan, validate_execution_plan


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
    result = ExecutionExecutor(market_indices=a_share_indices_handler(source)).execute(
        _plan(), "上周 A 股复盘", Scope.whole_corpus(),
    )
    assert {call[1] for call in source.calls} == {"week"}
    assert result.source_summary == {"local_pdf": "未使用", "market_data": "已使用", "web": "未使用"}
    assert result.steps[0].value["window"] == "week"
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
    result = ExecutionExecutor(market_overview=market_overview_handler(source, timeout=12)).execute(
        plan, "上周 A 股复盘", Scope.whole_corpus(),
    )

    assert [name for name, _, _ in source.calls] == [
        "index_prices", "stock_zt_pool", "stock_zt_pool", "stock_sector_fund_flow_rank",
    ]
    assert all(timeout == 12 for _, _, timeout in source.calls)
    assert result.steps[0].status == "completed"
    assert result.steps[0].value["window"] == "week"
