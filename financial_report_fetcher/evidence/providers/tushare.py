"""Tushare Pro HTTP 财务报表适配。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

import requests

from ..models import (
    EntityScope,
    EvidenceRecord,
    SourceLocator,
    SourceType,
    VerificationState,
)


ENDPOINT_FIELDS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("income", (("revenue", "total_revenue"), ("net_profit", "n_income"))),
    ("balancesheet", (("total_assets", "total_assets"), ("total_liabilities", "total_liab"))),
    ("cashflow", (("operating_cash_flow", "n_cashflow_act"),)),
)

CONSOLIDATED_REPORT_TYPES = {"1", "2", "3", "4", "5", "11"}
PARENT_REPORT_TYPES = {"6", "7", "8", "9", "10", "12"}
ADJUSTED_REPORT_TYPES = {"3", "4", "8", "9"}
ORIGINAL_REPORT_TYPES = {"5", "10", "11", "12"}


def _ts_code(company_code: str) -> str:
    code = str(company_code).strip().split(".")[0]
    if code.startswith(("4", "8")):
        suffix = "BJ"
    elif code.startswith(("5", "6", "9")):
        suffix = "SH"
    else:
        suffix = "SZ"
    return f"{code}.{suffix}"


def _period_digits(period: str) -> str:
    digits = "".join(character for character in str(period) if character.isdigit())[:8]
    if len(digits) != 8:
        raise ValueError(f"报告期格式错误: {period}")
    return digits


def _iso_period(period: str) -> str:
    digits = _period_digits(period)
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"


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


def _scope(report_type: Any) -> EntityScope:
    normalized = str(report_type or "")
    if normalized in CONSOLIDATED_REPORT_TYPES:
        return EntityScope.CONSOLIDATED
    if normalized in PARENT_REPORT_TYPES:
        return EntityScope.PARENT
    return EntityScope.UNKNOWN


def _adjustment_state(report_type: Any) -> str:
    normalized = str(report_type or "")
    if normalized in ADJUSTED_REPORT_TYPES:
        return "adjusted"
    if normalized in ORIGINAL_REPORT_TYPES:
        return "original"
    return "reported"


def _content_hash(endpoint: str, row: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"endpoint": endpoint, "row": dict(row)},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TushareProvider:
    name = "tushare"
    parser_version = "tushare-http-v1"
    base_url = "https://api.tushare.pro"

    def __init__(self, token: str, session=None, timeout: float = 30.0):
        if not token or not token.strip():
            raise ValueError("Tushare token 不能为空")
        self._token = token.strip()
        self._session = session or requests.Session()
        self._timeout = timeout

    def _request(self, endpoint: str, field_names: tuple[str, ...], params: dict[str, str]):
        fields = ("ts_code", "end_date", "report_type", "ann_date", *field_names)
        response = self._session.post(
            self.base_url,
            json={
                "api_name": endpoint,
                "token": self._token,
                "params": params,
                "fields": ",".join(fields),
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError(f"Tushare {endpoint} 返回了非对象响应")
        if payload.get("code") != 0:
            raise RuntimeError(f"Tushare {endpoint}: {payload.get('msg') or 'unknown error'}")
        data = payload.get("data") or {}
        result_fields = data.get("fields") or []
        items = data.get("items") or []
        if not isinstance(result_fields, list) or not isinstance(items, list):
            raise RuntimeError(f"Tushare {endpoint} 返回结构无效")
        return [dict(zip(result_fields, item)) for item in items]

    def fetch(
        self,
        company_code: str,
        period: str,
        report_id: str,
    ) -> list[EvidenceRecord]:
        period_digits = _period_digits(period)
        normalized_period = _iso_period(period)
        params = {"ts_code": _ts_code(company_code), "period": period_digits}
        records: list[EvidenceRecord] = []

        for endpoint, mappings in ENDPOINT_FIELDS:
            rows = self._request(
                endpoint,
                tuple(raw_name for _, raw_name in mappings),
                params,
            )
            for row in rows:
                if _period_digits(str(row.get("end_date") or "")) != period_digits:
                    continue
                report_type = row.get("report_type")
                scope = _scope(report_type)
                state = (
                    VerificationState.UNKNOWN_SCOPE
                    if scope is EntityScope.UNKNOWN
                    else VerificationState.SINGLE_SOURCE
                )
                row_hash = _content_hash(endpoint, row)
                for fact_name, raw_name in mappings:
                    value = _decimal(row.get(raw_name))
                    if value is None:
                        continue
                    records.append(EvidenceRecord(
                        report_id=report_id,
                        entity_scope=scope,
                        fact_name=fact_name,
                        value=value,
                        unit="yuan",
                        currency="CNY",
                        period=normalized_period,
                        source_type=SourceType.STRUCTURED,
                        source_locator=SourceLocator(
                            provider=self.name,
                            section=endpoint,
                            record_id=(
                                f"{endpoint}:{params['ts_code']}:{period_digits}:"
                                f"{report_type or 'unknown'}"
                            ),
                        ),
                        extraction_confidence=0.95 if scope is not EntityScope.UNKNOWN else 0.75,
                        verification_state=state,
                        content_hash=row_hash,
                        parser_version=self.parser_version,
                        adjustment_state=_adjustment_state(report_type),
                        source_timestamp=(
                            None if row.get("ann_date") is None else str(row.get("ann_date"))
                        ),
                        raw_field_name=raw_name,
                    ))
        return records
