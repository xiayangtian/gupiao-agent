"""可替换的结构化财务数据网关。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from financial_report_fetcher.analysis_config import AnalysisConfig

from .models import EvidenceRecord


class StructuredDataProvider(Protocol):
    name: str

    def fetch(
        self,
        company_code: str,
        period: str,
        report_id: str,
    ) -> list[EvidenceRecord]:
        """返回指定报告期的结构化证据。"""


@dataclass(frozen=True)
class StructuredFetchResult:
    records: list[EvidenceRecord]
    providers_used: list[str]
    failures: dict[str, str]


class StructuredDataGateway:
    """调用所有已配置提供方，为后续交叉验证保留独立来源。"""

    def __init__(self, providers: Sequence[StructuredDataProvider]):
        self.providers = tuple(providers)

    def fetch(
        self,
        company_code: str,
        period: str,
        report_id: str,
    ) -> StructuredFetchResult:
        records: list[EvidenceRecord] = []
        providers_used: list[str] = []
        failures: dict[str, str] = {}

        for provider in self.providers:
            try:
                fetched = provider.fetch(company_code, period, report_id)
            except Exception as exc:
                failures[provider.name] = f"{type(exc).__name__}: {exc}"
                continue
            if fetched:
                records.extend(fetched)
                providers_used.append(provider.name)

        return StructuredFetchResult(
            records=records,
            providers_used=providers_used,
            failures=failures,
        )


def build_structured_gateway(
    config: AnalysisConfig,
    *,
    environ: Mapping[str, str] | None = None,
    akshare_client=None,
    tushare_session=None,
) -> StructuredDataGateway:
    """按配置顺序构造网关；Tushare 仅在 Token 存在时注册。"""
    from .providers.akshare import AkshareProvider
    from .providers.tushare import TushareProvider

    environment = os.environ if environ is None else environ
    providers: list[StructuredDataProvider] = []
    for name in config.structured_providers:
        if name == "tushare":
            token = environment.get("TUSHARE_TOKEN", "").strip()
            if token:
                providers.append(TushareProvider(token=token, session=tushare_session))
        elif name == "akshare":
            providers.append(AkshareProvider(client=akshare_client))
        else:
            raise ValueError(f"不支持的结构化财务数据提供方: {name}")
    return StructuredDataGateway(providers)
