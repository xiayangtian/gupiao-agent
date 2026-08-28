"""
数据模型模块

定义所有共享的枚举、Pydantic 配置模型和运行时数据模型。
"""

from enum import Enum
from dataclasses import dataclass
from datetime import date
from typing import Optional, List

from pydantic import BaseModel, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# 枚举类型
# ─────────────────────────────────────────────────────────────────────────────

class ReportType(str, Enum):
    """财报类型枚举"""
    ANNUAL = "annual"           # 年报
    SEMI_ANNUAL = "semi_annual" # 半年报
    QUARTERLY = "quarterly"     # 季报


class DownloadStatus(str, Enum):
    """单个文件下载状态枚举"""
    SUCCESS = "success"   # 下载成功
    SKIPPED = "skipped"   # 文件已存在，跳过
    FAILED = "failed"     # 下载失败


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic 配置模型
# ─────────────────────────────────────────────────────────────────────────────

class CompanyConfig(BaseModel):
    """单家公司配置，ticker 和 name 至少提供其中一个"""

    ticker: Optional[str] = None  # 股票代码，1~9 位数字或字母，如 "600519"
    name: Optional[str] = None    # 公司名称，如 "贵州茅台"

    @model_validator(mode="after")
    def at_least_one_identifier(self) -> "CompanyConfig":
        """校验：ticker 和 name 至少需要提供一个"""
        if not self.ticker and not self.name:
            raise ValueError("每个公司必须提供 ticker 或 name 其中之一")
        return self


class AppConfig(BaseModel):
    """应用全局配置，对应配置文件的顶层结构"""

    storage_dir: str                           # 本地存储目录路径
    companies: List[CompanyConfig]             # 目标公司列表，1~50 家
    report_types: List[ReportType] = [ReportType.ANNUAL]  # 财报类型，默认仅年报
    start_date: Optional[date] = None          # 时间范围起始日期（可选）
    end_date: Optional[date] = None            # 时间范围截止日期（可选）
    max_count: Optional[int] = None            # 每家公司最多拉取数量，1~10000（可选）

    @field_validator("companies")
    @classmethod
    def validate_companies_count(cls, v: list) -> list:
        """校验公司列表数量在 1 至 50 之间"""
        if not (1 <= len(v) <= 50):
            raise ValueError("companies 字段需包含 1 至 50 家公司")
        return v

    @field_validator("max_count")
    @classmethod
    def validate_max_count(cls, v: Optional[int]) -> Optional[int]:
        """校验 max_count 在 1 至 10000 之间（未配置时允许为 None）"""
        if v is not None and not (1 <= v <= 10000):
            raise ValueError("max_count 需为 1 至 10000 之间的正整数")
        return v

    @model_validator(mode="after")
    def validate_date_range(self) -> "AppConfig":
        """
        校验日期范围的一致性：
        - start_date 和 end_date 必须同时指定或同时不指定
        - start_date 不得晚于 end_date
        """
        has_start = self.start_date is not None
        has_end = self.end_date is not None

        # 仅指定了一端，报错并说明缺少哪个字段
        if has_start ^ has_end:
            missing = "end_date" if has_start else "start_date"
            raise ValueError(
                f"start_date 和 end_date 必须同时指定，缺少字段: {missing}"
            )

        # 两端均已指定时，验证顺序
        if has_start and has_end and self.start_date > self.end_date:
            raise ValueError(
                f"start_date ({self.start_date}) 不能晚于 end_date ({self.end_date})"
            )

        return self


# ─────────────────────────────────────────────────────────────────────────────
# 运行时数据模型（dataclass）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DateRange:
    """表示一个闭区间日期范围 [start, end]"""

    start: date  # 区间起始日期（含）
    end: date    # 区间截止日期（含）

    def contains(self, d: date) -> bool:
        """判断日期 d 是否在该闭区间内"""
        return self.start <= d <= self.end


@dataclass
class ReportMeta:
    """单份财报的元信息，由 Fetcher 从数据源获取"""

    company_id: str          # 公司标识：优先使用 ticker，其次使用 name
    report_type: ReportType  # 财报类型（年报 / 半年报 / 季报）
    period: date             # 报告期，通常为年末或半年末日期
    download_url: str        # 财报 PDF 文件的下载地址
    title: str               # 财报标题（如 "贵州茅台2023年年度报告"）
    company_name: str = ""   # 公司名称（如 "长江电力"），用于文件名展示


@dataclass
class DownloadSummary:
    """一批财报下载任务完成后的汇总统计"""

    success: int = 0  # 成功下载的文件数量
    skipped: int = 0  # 因已存在而跳过的文件数量
    failed: int = 0   # 下载失败的文件数量

    @property
    def total(self) -> int:
        """全部处理的文件总数（成功 + 跳过 + 失败）"""
        return self.success + self.skipped + self.failed
