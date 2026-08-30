"""财报身份与本地文件名的统一规则。"""

import os
import re
from datetime import date
from typing import Any, Dict, Optional, Union

from financial_report_fetcher.models import ReportMeta, ReportType


_REPORT_TYPE_NAMES = {
    ReportType.ANNUAL: "年报",
    ReportType.SEMI_ANNUAL: "半年报",
    ReportType.QUARTERLY: "季报",
}

_NAMED_REPORT_FILENAME_RE = re.compile(
    r"^(?P<company>.+)_(?P<code>[^_]+)_(?P<type>年报|半年报|季报)_"
    r"(?P<period>\d{4}(?:-\d{2}-\d{2})?)\.pdf$"
)
_UNNAMED_REPORT_FILENAME_RE = re.compile(
    r"^(?P<code>[^_]+)_(?P<type>年报|半年报|季报)_"
    r"(?P<period>\d{4}(?:-\d{2}-\d{2})?)\.pdf$"
)
_ANALYSIS_FILENAME_RE = re.compile(
    r"^.+_(?P<code>\d{6})_(?P<period>\d{4}(?:-\d{2}-\d{2})?)_分析报告\.json$"
)


def build_report_id(
    code: str,
    period: Union[str, date],
    report_type: Optional[Union[ReportType, str]] = None,
) -> str:
    """根据代码、报告期和类型生成唯一的 RAG 报告身份。"""
    period_date = date.fromisoformat(period) if isinstance(period, str) else period
    resolved = report_type or (
        ReportType.SEMI_ANNUAL if period_date.month == 6
        else ReportType.QUARTERLY if period_date.month in (3, 9)
        else ReportType.ANNUAL
    )
    value = resolved.value if isinstance(resolved, ReportType) else str(resolved)
    return f"{code}:{period_date.isoformat()}:{value}"


def build_report_filename(report: ReportMeta) -> str:
    """生成下载文件名；季度文件保留完整报告期以区分 Q1/Q3。"""
    type_name = _REPORT_TYPE_NAMES[report.report_type]
    period = (
        report.period.isoformat()
        if report.report_type == ReportType.QUARTERLY
        else str(report.period.year)
    )
    prefix = report.company_name if report.company_name else report.company_id
    if report.company_name:
        return f"{prefix}_{report.company_id}_{type_name}_{period}.pdf"
    return f"{prefix}_{type_name}_{period}.pdf"


def parse_report_filename(path: str) -> Optional[str]:
    """从下载文件名解析报告身份；不可确定的旧季报返回 ``None``。"""
    basename = os.path.basename(path)
    match = (
        _NAMED_REPORT_FILENAME_RE.match(basename)
        or _UNNAMED_REPORT_FILENAME_RE.match(basename)
    )
    if not match:
        return None

    code = match.group("code")
    type_name = match.group("type")
    value = match.group("period")
    if type_name == "季报":
        if len(value) == 4:
            return None
        try:
            period = date.fromisoformat(value)
        except ValueError:
            return None
        return build_report_id(code, period, ReportType.QUARTERLY)

    year = int(value[:4])
    if type_name == "半年报":
        return build_report_id(code, date(year, 6, 30), ReportType.SEMI_ANNUAL)
    return build_report_id(code, date(year, 12, 31), ReportType.ANNUAL)


def derive_analysis_report_id(
    path: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """从分析 JSON 文件名和 meta 推导身份；歧义旧季报严格拒绝猜测。"""
    metadata = meta if isinstance(meta, dict) else {}
    source_file = metadata.get("source_file")
    if isinstance(source_file, str) and source_file:
        source_report_id = parse_report_filename(source_file)
        if source_report_id is not None:
            return source_report_id
        source_match = (
            _NAMED_REPORT_FILENAME_RE.match(os.path.basename(source_file))
            or _UNNAMED_REPORT_FILENAME_RE.match(os.path.basename(source_file))
        )
        if (
            source_match
            and source_match.group("type") == "季报"
            and len(source_match.group("period")) == 4
        ):
            return None

    match = _ANALYSIS_FILENAME_RE.match(os.path.basename(path))
    if match is None:
        return None
    code = match.group("code")

    meta_period = metadata.get("period")
    if isinstance(meta_period, str):
        try:
            date.fromisoformat(meta_period)
        except ValueError:
            pass
        else:
            return build_report_id(code, meta_period)

    filename_period = match.group("period")
    if len(filename_period) == 4:
        filename_period = f"{filename_period}-12-31"
    return build_report_id(code, filename_period)
