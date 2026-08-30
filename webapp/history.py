"""webapp.history — 本地分析产物与已下载 PDF 的扫描/聚合

供「历史记录」页面（GET /api/history）与财报列表「已分析」标记使用。
纯函数设计（不依赖 FastAPI/磁盘以外的状态），便于单元测试。

文件命名约定（与 downloader.build_filename / AnalysisReport.save 对齐）：
    PDF:   {公司}_{代码}_{年报|半年报|季报}_{年份}.pdf
    分析:  {公司}_{代码}_{年份}_分析报告.json   （Web，safe_company 含 6 位代码）
           {公司}_{年份}_分析报告.json          （旧 CLI，无代码）
"""

import json
import logging
import os
import re
from datetime import date
from typing import Any, Dict, List, Optional

from financial_report_fetcher.report_identity import (
    derive_analysis_report_id,
    parse_report_filename,
)

logger = logging.getLogger(__name__)

ANALYSIS_SUFFIX = "_分析报告.json"
PDF_NAME_RE = re.compile(
    r"^(?P<company>.+)_(?P<code>\d{6})_(?P<type>年报|半年报|季报)_"
    r"(?P<period>\d{4}(?:-\d{2}-\d{2})?)\.pdf$"
)


def parse_analysis_filename(filename: str) -> Optional[Dict[str, Any]]:
    """从分析文件名解析 (company_label, code?, year, period?)。两种形态。

    形态1: {company}_{code}_{year_or_period}_分析报告.json  → code + year（可带精确期次）
    形态2: {company}_{year}_分析报告.json          → year（无 code）
    无法解析年份 → None。
    """
    basename = filename if not os.path.sep else os.path.basename(filename)
    if not basename.endswith(ANALYSIS_SUFFIX):
        return None
    stem = basename[: -len(ANALYSIS_SUFFIX)]
    # 尝试 {company}_{code}_{year} 三部分
    parts = stem.rsplit("_", 2)
    if len(parts) == 3 and re.fullmatch(r"\d{6}", parts[1]):
        period_value = parts[2]
        if re.fullmatch(r"\d{4}", period_value):
            period = None
        else:
            try:
                date.fromisoformat(period_value)
            except ValueError:
                period = None
            else:
                period = period_value
        if period is not None or period_value.isdigit():
            return {
                "company_label": parts[0],
                "code": parts[1],
                "year": int(period_value[:4]),
                "period": period,
            }
    # 尝试 {company}_{year} 两部分
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return {
            "company_label": parts[0],
            "code": None,
            "year": int(parts[1]),
        }
    return None


def parse_pdf_filename(filename: str) -> Optional[Dict[str, Any]]:
    """解析已下载 PDF 文件名；不匹配返回 None。

    Returns: {filename, company, code, year, type, period, period_exact, title}
    - 年报 → clickable=True, period=YYYY-12-31
    - 半年报/带完整期次的季报 → clickable=False，period 为精确报告期
    """
    basename = filename if not os.path.sep else os.path.basename(filename)
    m = PDF_NAME_RE.match(basename)
    if not m:
        return None
    report_id = parse_report_filename(basename)
    if report_id is None:
        return None
    _, period, report_type = report_id.split(":")
    year = int(period[:4])
    kind = m.group("type")
    return {
        "filename": basename,
        "company": m.group("company"),
        "code": m.group("code"),
        "year": year,
        "type": kind,
        "period": period,
        "period_exact": True,
        "clickable": report_type == "annual",
        "title": f"{m.group('company')}{year}{kind}",
    }


def _read_analysis(path: str) -> Optional[Dict[str, Any]]:
    """读取分析 JSON；损坏返回 None（不影响其他条目）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        logger.warning("跳过无法解析的分析文件：%s", path)
        return None


def build_flat_history(analysis_dir: str, reports_dir: str) -> List[Dict[str, Any]]:
    """扫描本地 PDF 与分析 JSON，返回扁平列表（非按公司聚合）。

    每项包含:
        code, company, year, type, period,
        pdf_filename, has_analysis, analysis_filename,
        pdf_mtime, analysis_mtime

    排序：先按年份倒序，再按代码升序。
    """
    # 1) 收集已分析的报告
    analyzed: Dict[str, Dict[str, Any]] = {}  # key = (code, period) -> entry
    if os.path.isdir(analysis_dir):
        for fname in sorted(os.listdir(analysis_dir)):
            if not fname.endswith(ANALYSIS_SUFFIX):
                continue
            content = _read_analysis(os.path.join(analysis_dir, fname))
            if content is None:
                continue
            parsed = parse_analysis_filename(fname)
            if not parsed:
                continue
            meta = content.get("meta", {}) if isinstance(content, dict) else {}
            report_id = derive_analysis_report_id(fname, meta)
            if report_id is None:
                continue
            code, period, _report_type = report_id.split(":")
            year = int(period[:4])
            try:
                mtime = os.path.getmtime(os.path.join(analysis_dir, fname))
            except OSError:
                mtime = 0.0
            key = f"{code}:{period}"
            company = meta.get("company", parsed.get("company_label", code)) if isinstance(meta, dict) else code
            analyzed[key] = {
                "code": code,
                "company": _strip_ticker(company) if isinstance(company, str) else str(company),
                "year": year,
                "type": "",
                "period": period,
                "pdf_filename": "",
                "has_analysis": True,
                "analysis_filename": fname,
                "analysis_mtime": mtime,
                "pdf_mtime": 0.0,
            }

    # 2) 收集已下载的 PDF
    items: Dict[str, Dict[str, Any]] = dict(analyzed)  # 已分析项优先
    if os.path.isdir(reports_dir):
        for fname in sorted(os.listdir(reports_dir)):
            try:
                fpath = os.path.join(reports_dir, fname)
                if not os.path.isfile(fpath) or os.path.getsize(fpath) <= 0:
                    continue
                parsed = parse_pdf_filename(fname)
                if not parsed:
                    continue
                pdf_mtime = os.path.getmtime(fpath)
            except OSError:
                continue
            code = parsed["code"]
            period = parsed["period"]
            key = f"{code}:{period}"
            if key in items:
                # 合并 PDF 信息
                items[key]["pdf_filename"] = parsed["filename"]
                items[key]["type"] = items[key]["type"] or parsed["type"]
                items[key]["pdf_mtime"] = pdf_mtime
                if not items[key]["company"]:
                    items[key]["company"] = parsed["company"]
            else:
                items[key] = {
                    "code": code,
                    "company": parsed["company"],
                    "year": parsed["year"],
                    "type": parsed["type"],
                    "period": period,
                    "pdf_filename": parsed["filename"],
                    "has_analysis": False,
                    "analysis_filename": "",
                    "analysis_mtime": 0.0,
                    "pdf_mtime": pdf_mtime,
                }

    # 3) 排序：年份倒序、代码升序
    result = sorted(items.values(), key=lambda x: (-x["year"], x["code"]))
    return result


def _strip_ticker(text: str) -> str:
    """去掉公司名中的股票代码括号，如 '长江电力（600900）' → '长江电力'"""
    return re.sub(r"[（(]?\d{6}[)）]?$", "", text).strip()


def get_analysis_detail(analysis_dir: str, filename: str) -> Optional[Dict[str, Any]]:
    """读取单份分析报告完整内容；不存在或损坏返回 None。"""
    filepath = os.path.join(analysis_dir, os.path.basename(filename))
    if not os.path.isfile(filepath):
        return None
    return _read_analysis(filepath)


def analyzed_periods_for_code(analysis_dir: str, code: str) -> set:
    """仅扫描指定股票的分析 JSON，返回精确已分析报告期集合。"""
    periods: set = set()
    if not os.path.isdir(analysis_dir):
        return periods
    for fname in os.listdir(analysis_dir):
        if not fname.endswith(ANALYSIS_SUFFIX):
            continue
        content = _read_analysis(os.path.join(analysis_dir, fname))
        if content is None:
            continue
        parsed = parse_analysis_filename(fname)
        if not parsed:
            continue
        meta = content.get("meta", {}) if isinstance(content, dict) else {}
        report_id = derive_analysis_report_id(fname, meta)
        if report_id is None:
            continue
        report_code, period, _report_type = report_id.split(":")
        if report_code == code:
            periods.add(period)
    return periods
