"""结构化财务数据提供方与故障切换契约。"""

from decimal import Decimal

import pytest

from financial_report_fetcher.analysis_config import AnalysisConfig
from financial_report_fetcher.evidence.models import (
    EntityScope,
    EvidenceRecord,
    SourceLocator,
    SourceType,
    VerificationState,
)
from financial_report_fetcher.evidence.providers.akshare import AkshareProvider
from financial_report_fetcher.evidence.providers.tushare import TushareProvider
from financial_report_fetcher.evidence.structured import (
    StructuredDataGateway,
    build_structured_gateway,
)


def _sample_record(provider: str = "akshare") -> EvidenceRecord:
    return EvidenceRecord(
        report_id="600900:2025-12-31:annual",
        entity_scope=EntityScope.CONSOLIDATED,
        fact_name="revenue",
        value=Decimal("100"),
        unit="yuan",
        currency="CNY",
        period="2025-12-31",
        source_type=SourceType.STRUCTURED,
        source_locator=SourceLocator(provider=provider, record_id="income:2025-12-31"),
        extraction_confidence=0.95,
        verification_state=VerificationState.SINGLE_SOURCE,
        content_hash="hash",
        parser_version=f"{provider}-v1",
    )


class FakeProvider:
    def __init__(self, name, records=None, error=None):
        self.name = name
        self.records = records or []
        self.error = error

    def fetch(self, company_code, period, report_id):
        if self.error:
            raise self.error
        return list(self.records)


def test_gateway_keeps_successful_records_and_reports_each_provider_failure():
    """单一数据源超时不能让其他来源的有效证据丢失。"""
    record = _sample_record()
    gateway = StructuredDataGateway([
        FakeProvider("tushare", error=TimeoutError("timeout")),
        FakeProvider("akshare", records=[record]),
    ])

    result = gateway.fetch("600900", "2025-12-31", record.report_id)

    assert result.records == [record]
    assert result.providers_used == ["akshare"]
    assert result.failures == {"tushare": "TimeoutError: timeout"}


def test_gateway_queries_all_successful_providers_for_cross_validation():
    """多个可用来源都必须保留，不能在首个成功结果后提前返回。"""
    tushare_record = _sample_record("tushare")
    akshare_record = _sample_record("akshare")

    result = StructuredDataGateway([
        FakeProvider("tushare", records=[tushare_record]),
        FakeProvider("akshare", records=[akshare_record]),
    ]).fetch("600900", "2025-12-31", tushare_record.report_id)

    assert result.records == [tushare_record, akshare_record]
    assert result.providers_used == ["tushare", "akshare"]
    assert result.failures == {}


class FakeAkshare:
    def __init__(self, statements):
        self.statements = statements
        self.calls = []

    def stock_financial_report_sina(self, stock, symbol):
        self.calls.append((stock, symbol))
        return self.statements[symbol]


def test_akshare_maps_three_statements_and_preserves_raw_provenance():
    """三大报表的关键字段必须带原始列名、主体、币种和更新时间。"""
    client = FakeAkshare({
        "利润表": [{
            "报告日": "20251231", "营业总收入": "1,000.50", "净利润": 90,
            "币种": "CNY", "类型": "合并期末", "更新日期": "2026-03-20T10:00:00",
        }],
        "资产负债表": [{
            "报告日": "20251231", "资产总计": 5000, "负债合计": 2200,
            "币种": "CNY", "类型": "合并期末", "更新日期": "2026-03-20T10:00:00",
        }],
        "现金流量表": [{
            "报告日": "20251231", "经营活动产生的现金流量净额": 130,
            "币种": "CNY", "类型": "合并期末", "更新日期": "2026-03-20T10:00:00",
        }],
    })

    records = AkshareProvider(client=client).fetch(
        "600900", "2025-12-31", "600900:2025-12-31:annual"
    )

    assert client.calls == [
        ("sh600900", "利润表"),
        ("sh600900", "资产负债表"),
        ("sh600900", "现金流量表"),
    ]
    assert {record.fact_name for record in records} == {
        "revenue", "net_profit", "total_assets", "total_liabilities",
        "operating_cash_flow",
    }
    revenue = next(record for record in records if record.fact_name == "revenue")
    assert revenue.value == Decimal("1000.50")
    assert revenue.entity_scope is EntityScope.CONSOLIDATED
    assert revenue.raw_field_name == "营业总收入"
    assert revenue.source_timestamp == "2026-03-20T10:00:00"
    assert revenue.source_locator.provider == "akshare"
    assert revenue.source_locator.section == "利润表"


def test_akshare_does_not_guess_unknown_statement_scope():
    """缺少明确报表类型时必须保留 unknown，不能默认当作合并口径。"""
    client = FakeAkshare({
        "利润表": [{"报告日": "20251231", "营业总收入": 100, "类型": ""}],
        "资产负债表": [],
        "现金流量表": [],
    })

    records = AkshareProvider(client=client).fetch("000001", "2025-12-31", "r1")

    assert records[0].entity_scope is EntityScope.UNKNOWN
    assert records[0].verification_state is VerificationState.UNKNOWN_SCOPE


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(self.payloads.pop(0))


def _tushare_payload(fields, row):
    return {"code": 0, "msg": None, "data": {"fields": fields, "items": [row]}}


def test_tushare_http_adapter_maps_report_type_and_financial_fields():
    """Tushare 原始 report_type 必须映射为主体和调整状态并保留。"""
    session = FakeSession([
        _tushare_payload(
            ["ts_code", "end_date", "report_type", "ann_date", "total_revenue", "n_income"],
            ["600900.SH", "20251231", "1", "20260320", 1000, 90],
        ),
        _tushare_payload(
            ["ts_code", "end_date", "report_type", "ann_date", "total_assets", "total_liab"],
            ["600900.SH", "20251231", "6", "20260320", 5000, 2200],
        ),
        _tushare_payload(
            ["ts_code", "end_date", "report_type", "ann_date", "n_cashflow_act"],
            ["600900.SH", "20251231", "4", "20260320", 130],
        ),
    ])
    provider = TushareProvider(token="secret", session=session, timeout=12)

    records = provider.fetch("600900", "2025-12-31", "r1")

    assert [call["json"]["api_name"] for call in session.calls] == [
        "income", "balancesheet", "cashflow"
    ]
    assert all(call["json"]["params"] == {
        "ts_code": "600900.SH", "period": "20251231"
    } for call in session.calls)
    assert next(r for r in records if r.fact_name == "revenue").entity_scope is EntityScope.CONSOLIDATED
    assets = next(r for r in records if r.fact_name == "total_assets")
    assert assets.entity_scope is EntityScope.PARENT
    cash = next(r for r in records if r.fact_name == "operating_cash_flow")
    assert cash.adjustment_state == "adjusted"


def test_tushare_api_error_is_visible_to_gateway():
    """接口业务错误不能静默变成空数据，否则无法触发故障记录。"""
    session = FakeSession([{"code": -2001, "msg": "permission denied", "data": None}])

    with pytest.raises(RuntimeError, match="permission denied"):
        TushareProvider(token="secret", session=session).fetch("600900", "2025-12-31", "r1")


def test_gateway_builder_skips_tushare_without_token(monkeypatch):
    """未配置 Token 时不应注册一个必然失败的 Tushare 提供方。"""
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)

    gateway = build_structured_gateway(AnalysisConfig(), akshare_client=FakeAkshare({
        "利润表": [], "资产负债表": [], "现金流量表": [],
    }))

    assert [provider.name for provider in gateway.providers] == ["akshare"]
