"""AKShare 新浪财经三大报表适配。"""

from __future__ import annotations

import hashlib
import importlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from ..models import (
    EntityScope,
    EvidenceRecord,
    SourceLocator,
    SourceType,
    VerificationState,
)


STATEMENT_FIELDS: tuple[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], ...] = (
    (
        "利润表",
        (
            ("revenue", ("营业总收入", "营业收入")),
            ("net_profit", ("净利润",)),
            ("parent_net_profit", ("归属于母公司所有者的净利润", "归属于母公司的净利润")),
            ("operating_profit", ("营业利润",)),
            ("total_profit", ("利润总额",)),
            ("net_interest_income", ("净利息收入",)),
            ("fee_income", ("手续费及佣金净收入", "手续费及佣金收入")),
            ("investment_income", ("投资收益",)),
            ("fair_value_gain", (
                "公允价值变动收益/(损失)", "公允价值变动收益", "公允价值变动损益"
            )),
            ("credit_impairment_loss", ("信用减值损失", "资产减值损失")),
            ("business_overhead", ("业务及管理费用",)),
            ("basic_eps", ("基本每股收益",)),
            ("diluted_eps", ("稀释每股收益",)),
            ("comprehensive_income", ("综合收益总额",)),
        ),
    ),
    (
        "资产负债表",
        (
            ("total_assets", ("资产总计",)),
            ("total_liabilities", ("负债合计",)),
            ("parent_equity", (
                "归属于母公司股东的权益", "归属于母公司股东权益合计",
                "归属于母公司所有者权益合计", "股东权益合计",
            )),
            ("minority_interest", ("少数股东权益",)),
            ("share_capital", ("股本", "实收资本")),
            ("capital_reserve", ("资本公积",)),
            ("retained_earnings", ("未分配利润",)),
            ("monetary_funds", ("货币资金", "现金及存放中央银行款项")),
            ("customer_deposits", ("客户存款(吸收存款)", "客户存款")),
            ("loans_and_advances", ("发放贷款及垫款净额",)),
            ("trading_assets", ("交易性金融资产",)),
            ("inventory", ("存货",)),
            ("fixed_assets", ("固定资产净额", "固定资产合计", "固定资产")),
            ("construction_in_progress", ("在建工程",)),
            ("goodwill", ("商誉",)),
        ),
    ),
    (
        "现金流量表",
        (
            ("operating_cash_flow", ("经营活动产生的现金流量净额",)),
            ("investing_cash_flow", ("投资活动产生的现金流量净额",)),
            ("financing_cash_flow", ("筹资活动产生的现金流量净额",)),
            ("capex_paid", ("购建固定资产、无形资产和其他长期资产支付的现金",)),
            ("dividends_paid", ("分配股利、利润或偿付利息支付的现金",)),
            ("cash_net_increase", ("现金及现金等价物净增加额",)),
            ("cash_end_balance", ("期末现金及现金等价物余额", "现金的期末余额")),
        ),
    ),
)


def _market_code(company_code: str) -> str:
    code = str(company_code).strip().split(".")[0]
    if code.startswith(("4", "8")):
        return f"bj{code}"
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    return f"sz{code}"


def _period_key(value: Any) -> str:
    return "".join(character for character in str(value) if character.isdigit())[:8]


def _iso_period(period: str) -> str:
    digits = _period_key(period)
    if len(digits) != 8:
        raise ValueError(f"报告期格式错误: {period}")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if hasattr(value, "to_dict"):
        converted = value.to_dict(orient="records")
    else:
        converted = value
    if converted is None:
        return []
    if not isinstance(converted, Iterable) or isinstance(converted, (str, bytes, Mapping)):
        raise TypeError("AKShare 财务报表结果必须是 DataFrame 或记录列表")
    return [row for row in converted if isinstance(row, Mapping)]


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "<na>"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _scope(value: Any) -> EntityScope:
    text = str(value or "")
    if "母公司" in text:
        return EntityScope.PARENT
    if "合并" in text:
        return EntityScope.CONSOLIDATED
    return EntityScope.UNKNOWN


def _adjustment_state(value: Any) -> str:
    text = str(value or "")
    if "调整前" in text:
        return "original"
    if "调整" in text:
        return "adjusted"
    return "reported"


def _content_hash(statement: str, row: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"statement": statement, "row": dict(row)},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AkshareProvider:
    name = "akshare"
    parser_version = "akshare-sina-v1"

    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = importlib.import_module("akshare")
        return self._client

    def fetch(
        self,
        company_code: str,
        period: str,
        report_id: str,
    ) -> list[EvidenceRecord]:
        target_period = _period_key(period)
        normalized_period = _iso_period(period)
        market_code = _market_code(company_code)
        records: list[EvidenceRecord] = []

        for statement, mappings in STATEMENT_FIELDS:
            response = self.client.stock_financial_report_sina(
                stock=market_code,
                symbol=statement,
            )
            for row in _rows(response):
                if _period_key(row.get("报告日")) != target_period:
                    continue
                scope = _scope(row.get("类型"))
                state = (
                    VerificationState.UNKNOWN_SCOPE
                    if scope is EntityScope.UNKNOWN
                    else VerificationState.SINGLE_SOURCE
                )
                row_hash = _content_hash(statement, row)
                for fact_name, raw_names in mappings:
                    raw_name = next(
                        (name for name in raw_names if _decimal(row.get(name)) is not None),
                        None,
                    )
                    if raw_name is None:
                        continue
                    records.append(EvidenceRecord(
                        report_id=report_id,
                        entity_scope=scope,
                        fact_name=fact_name,
                        value=_decimal(row.get(raw_name)),
                        unit="yuan",
                        currency=str(row.get("币种") or "CNY"),
                        period=normalized_period,
                        source_type=SourceType.STRUCTURED,
                        source_locator=SourceLocator(
                            provider=self.name,
                            section=statement,
                            record_id=f"{statement}:{target_period}:{scope.value}",
                        ),
                        extraction_confidence=0.90 if scope is not EntityScope.UNKNOWN else 0.75,
                        verification_state=state,
                        content_hash=row_hash,
                        parser_version=self.parser_version,
                        adjustment_state=_adjustment_state(row.get("类型")),
                        source_timestamp=(
                            None if row.get("更新日期") is None else str(row.get("更新日期"))
                        ),
                        raw_field_name=raw_name,
                    ))
        return records
