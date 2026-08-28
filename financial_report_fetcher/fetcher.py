"""
financial_report_fetcher.fetcher

Fetcher 模块：负责根据配置查找目标公司财报元数据，应用日期、类型、数量过滤，
并返回符合条件的 ReportMeta 迭代器。
"""

import datetime
import logging
from typing import Any, Dict, List, Iterator, Optional

from financial_report_fetcher.models import (
    AppConfig,
    CompanyConfig,
    DateRange,
    ReportMeta,
    ReportType,
)

# 模块级日志记录器
logger = logging.getLogger(__name__)


class ReportFetcher:
    """
    财报拉取器。

    根据 AppConfig 中的配置过滤目标公司财报并返回元信息列表。

    参数
    ----
    known_companies : Dict[str, Any]
        可匹配的公司数据库，字典的 key 为公司标识（ticker 或 name）。
        默认为空字典（即无任何已知公司）。
        设计为可注入，便于测试中使用 mock 数据。
    reports_db : Dict[str, List[ReportMeta]]
        公司财报数据库，key 为公司标识（ticker 或 name），
        value 为该公司的 ReportMeta 列表。
        默认为空字典，便于测试中注入 mock 数据。
    """

    def __init__(
        self,
        known_companies: Optional[Dict[str, Any]] = None,
        reports_db: Optional[Dict[str, List[ReportMeta]]] = None,
    ) -> None:
        # 已知公司数据库，key 为 ticker 或公司名称
        self.known_companies: Dict[str, Any] = (
            known_companies if known_companies is not None else {}
        )
        # 公司财报数据库，key 为公司标识，value 为财报元信息列表
        self.reports_db: Dict[str, List[ReportMeta]] = (
            reports_db if reports_db is not None else {}
        )

    def _resolve_company(self, company: CompanyConfig) -> Optional[str]:
        """
        解析公司标识；优先使用 ticker，其次使用 name。

        匹配逻辑：
        1. 若 ticker 非空且存在于已知公司数据库中，返回该 ticker。
        2. 若 name 非空且存在于已知公司数据库中，返回该 name。
        3. 若均无法匹配，记录包含原始标识内容的 WARN 日志，返回 None。

        参数
        ----
        company : CompanyConfig
            待解析的公司配置，至少包含 ticker 或 name 之一。

        返回
        ----
        Optional[str]
            成功匹配时返回公司标识字符串（ticker 优先于 name），
            无法匹配时返回 None。
        """
        # 优先尝试 ticker 匹配
        if company.ticker and company.ticker in self.known_companies:
            return company.ticker

        # 其次尝试 name 匹配
        if company.name and company.name in self.known_companies:
            return company.name

        # 无法匹配：构造原始标识字符串，用于 WARN 日志
        raw_identifier = company.ticker or company.name
        logger.warning("无法匹配公司标识 \"%s\"，已跳过", raw_identifier)
        return None

    def _apply_filters(
        self,
        reports: List[ReportMeta],
        date_range: DateRange,
        report_types: List[ReportType],
        max_count: Optional[int],
    ) -> List[ReportMeta]:
        """
        对财报列表应用过滤、排序和截取。

        处理顺序：
        1. 按时间范围（DateRange 闭区间）过滤 period
        2. 按 report_types 精确匹配过滤财报类型
        3. 按报告期降序排序（最新财报排在前面）
        4. 按 max_count 截取（max_count 为 None 时默认截取 1 条）

        参数
        ----
        reports : List[ReportMeta]
            待过滤的财报元信息列表。
        date_range : DateRange
            闭区间日期范围 [start, end]，报告期必须在此区间内。
        report_types : List[ReportType]
            允许的财报类型列表，精确匹配。
        max_count : Optional[int]
            最多截取的条数；为 None 时默认取 1 条（需求 4.3）。

        返回
        ----
        List[ReportMeta]
            经过滤、排序、截取后的财报列表。
        """
        # 步骤 1：按时间范围过滤，仅保留报告期在闭区间 [start, end] 内的财报
        filtered = [r for r in reports if date_range.contains(r.period)]

        # 步骤 2：按财报类型精确匹配过滤
        filtered = [r for r in filtered if r.report_type in report_types]

        # 步骤 3：按报告期降序排序（最新报告期排在最前）
        filtered.sort(key=lambda r: r.period, reverse=True)

        # 步骤 4：截取指定数量，max_count 为 None 时默认取 1 条
        limit = max_count if max_count is not None else 1
        return filtered[:limit]

    def fetch(self, config: AppConfig) -> Iterator[ReportMeta]:
        """
        根据配置拉取财报元信息列表。

        处理逻辑：
        1. 若 config 中未指定 start_date/end_date，自动设为上一自然年度
           （{today.year-1}-01-01 至 {today.year-1}-12-31），并记录 INFO 日志。
        2. 遍历 config.companies，调用 _resolve_company 解析公司标识；
           无法匹配的公司记录 WARN 日志并跳过。
        3. 从 reports_db 中取出对应公司的财报列表，调用 _apply_filters 过滤。
        4. 依次 yield 过滤后的 ReportMeta 条目。

        参数
        ----
        config : AppConfig
            应用全局配置，包含公司列表、时间范围、财报类型和数量上限。

        返回
        ----
        Iterator[ReportMeta]
            经过滤、排序、截取后的财报元信息迭代器。
        """
        # 步骤 1：确定生效的时间范围
        if config.start_date is None or config.end_date is None:
            # 未配置时间范围，自动使用运行时当前年份的上一自然年度
            today = datetime.date.today()
            prev_year = today.year - 1
            effective_start = datetime.date(prev_year, 1, 1)
            effective_end = datetime.date(prev_year, 12, 31)
            # 需求 4.4：INFO 日志记录实际使用的 start_date 和 end_date 日期值
            logger.info(
                "默认时间范围已应用：start_date=%s, end_date=%s",
                effective_start,
                effective_end,
            )
        else:
            # 已配置时间范围，直接使用配置值
            effective_start = config.start_date
            effective_end = config.end_date

        # 构造 DateRange 闭区间对象
        date_range = DateRange(start=effective_start, end=effective_end)

        # 步骤 2 & 3：遍历目标公司，解析标识并过滤财报
        for company in config.companies:
            # 解析公司标识；无法匹配时返回 None 并已记录 WARN 日志
            company_id = self._resolve_company(company)
            if company_id is None:
                # 跳过无法匹配的公司，继续处理下一家
                continue

            # 从财报数据库中取出该公司的财报列表（不存在则为空列表）
            company_reports: List[ReportMeta] = self.reports_db.get(company_id, [])

            # 应用日期范围、财报类型和数量过滤
            filtered = self._apply_filters(
                reports=company_reports,
                date_range=date_range,
                report_types=config.report_types,
                max_count=config.max_count,
            )

            # 逐条 yield 过滤后的财报元信息
            yield from filtered
