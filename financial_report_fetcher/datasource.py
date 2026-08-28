"""
financial_report_fetcher.datasource

数据源模块：封装巨潮资讯网（CNINFO）API，提供：
- 公司标识解析（ticker/name → stockCode + orgId）
- 财报元信息查询（按代码、类型、日期范围）
- PDF 下载 URL 生成
"""

import datetime
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from functools import lru_cache

import requests

from financial_report_fetcher.models import ReportMeta, ReportType

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# 常量
# ═════════════════════════════════════════════════════════════════════════════

CNINFO_STOCK_JSON = "http://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_PDF_BASE = "http://static.cninfo.com.cn/"

# 非主要报告的关键词（英文版、摘要版等）
_NON_PRIMARY_KEYWORDS = [
    "英文版",
    "摘要",
    "English",
    "abstract",
]


def _is_non_primary_report(title: str) -> bool:
    """判断是否为非正式中文报告（英文版、摘要版等）"""
    return any(kw in title for kw in _NON_PRIMARY_KEYWORDS)

# CNINFO category 参数映射
_CATEGORY_MAP: Dict[str, str] = {
    ReportType.ANNUAL: "category_ndbg_szsh",
    ReportType.SEMI_ANNUAL: "category_bndbg_szsh",
    ReportType.QUARTERLY: ["category_yjdbg_szsh", "category_sjdbg_szsh"],
}


@dataclass
class _AnnouncementEntry:
    """CNINFO 公告查询返回的单条记录（内部使用）"""

    stock_code: str
    org_id: str
    title: str
    announcement_time: datetime.datetime
    adjunct_url: str
    adjunct_type: str


class CNINFODatasource:
    """
    巨潮资讯网数据源。

    负责将 CNINFO 的 HTTP API 封装为代码内部使用的数据接口：
    1. build_known_companies() — 构建公司名称/代码 → orgId 的映射表
    2. fetch_reports() — 查询指定公司的年报/半年报/季报元信息
    3. resolve_company() — 按 ticker 或 name 匹配公司
    """

    def __init__(self, timeout: int = 30) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "http://www.cninfo.com.cn/",
            }
        )
        self._timeout = timeout
        # 股票代码 → (orgId, name) 的本地缓存
        self._stock_map: Optional[Dict[str, Tuple[str, str]]] = None
        # 公司名称 → 股票代码 的反向索引
        self._name_index: Optional[Dict[str, str]] = None

    # ─────────────────────────────────────────────────────────────────────
    # 公开接口
    # ─────────────────────────────────────────────────────────────────────

    def build_known_companies(self) -> Dict[str, Any]:
        """
        从 CNINFO 拉取全量 A 股上市公司列表，构建 known_companies 字典。

        返回
        ----
        Dict[str, Any]
            key 为 ticker（如 "600519"）或公司简称（如 "贵州茅台"），
            value 为包含 orgId 等信息的字典。
            可直接注入 ReportFetcher(known_companies=...)。
        """
        self._ensure_stock_map()
        known: Dict[str, Any] = {}
        for code, (org_id, name) in self._stock_map.items():
            known[code] = {"org_id": org_id, "name": name}
            known[name] = {"org_id": org_id, "code": code}
        return known

    def resolve_company(self, ticker: Optional[str], name: Optional[str]) -> Optional[str]:
        """
        按 ticker 或 name 解析公司标识。

        优先返回 ticker；次用 name 查反向索引返回对应 ticker。

        参数
        ----
        ticker : Optional[str]
            股票代码
        name : Optional[str]
            公司名称

        返回
        ----
        Optional[str]
            匹配到的股票代码，无法匹配返回 None。
        """
        self._ensure_stock_map()
        if ticker and ticker in self._stock_map:
            return ticker
        if name and self._name_index and name in self._name_index:
            return self._name_index[name]
        return None

    def fetch_reports(
        self,
        stock_code: str,
        report_types: List[ReportType],
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[ReportMeta]:
        """
        查询指定公司的财报公告。

        参数
        ----
        stock_code : str
            6 位股票代码（如 "600519"）
        report_types : List[ReportType]
            目标财报类型列表
        start_date : date
            报告期时间范围起始
        end_date : date
            报告期时间范围截止

        返回
        ----
        List[ReportMeta]
            匹配的财报元信息列表。
        """
        self._ensure_stock_map()

        if stock_code not in self._stock_map:
            logger.warning("未找到股票代码 %s，跳过查询", stock_code)
            return []

        org_id, company_name = self._stock_map[stock_code]
        all_entries: List[ReportMeta] = []

        # 公告查询范围需要比报告期范围更宽：
        # 年报通常在次年 3~4 月发布，半年报/季报也可能延迟，
        # 因此 seDate 的 end 向后扩展一年以确保不漏
        cninfo_end = end_date.replace(year=end_date.year + 1)

        for rt in report_types:
            categories = _CATEGORY_MAP.get(rt)
            if categories is None:
                continue

            cat_list = categories if isinstance(categories, list) else [categories]

            for cat in cat_list:
                try:
                    entries = self._query_announcements(
                        stock_code=stock_code,
                        org_id=org_id,
                        category=cat,
                        start_date=start_date,
                        end_date=cninfo_end,
                    )
                except Exception:
                    logger.exception("查询 %s 的 %s 公告失败", stock_code, cat)
                    continue

                for e in entries:
                    # 跳过英文版和摘要版，仅保留正式中文报告
                    title = e.title
                    if _is_non_primary_report(title):
                        continue

                    # 从标题推断报告期
                    period = self._infer_period(title, e.announcement_time)

                    download_url = CNINFO_PDF_BASE + e.adjunct_url

                    report_meta = ReportMeta(
                        company_id=stock_code,
                        company_name=company_name,
                        report_type=rt,
                        period=period,
                        download_url=download_url,
                        title=title,
                    )
                    all_entries.append(report_meta)

        return all_entries

    # ─────────────────────────────────────────────────────────────────────
    # 内部实现
    # ─────────────────────────────────────────────────────────────────────

    def _ensure_stock_map(self) -> None:
        """拉取并缓存全量股票列表。"""
        if self._stock_map is not None:
            return

        logger.info("正在从巨潮资讯网拉取上市公司列表...")
        try:
            r = self._session.get(CNINFO_STOCK_JSON, timeout=self._timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.error("拉取上市公司列表失败: %s", e)
            raise

        stock_list = data.get("stockList", [])
        self._stock_map = {}
        self._name_index = {}

        for item in stock_list:
            code = item.get("code", "")
            org_id = item.get("orgId", "")
            # 简称字段可能是 zwjc 或 name
            name = item.get("zwjc") or item.get("name") or item.get("ename", "")
            if code and org_id:
                self._stock_map[code] = (org_id, name)
                if name:
                    self._name_index[name] = code

        logger.info("已加载 %d 家上市公司", len(self._stock_map))

    def _query_announcements(
        self,
        stock_code: str,
        org_id: str,
        category: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[_AnnouncementEntry]:
        """
        调用 CNINFO 公告查询 API，获取指定类别的公告列表。

        参数
        ----
        stock_code : str
            6位股票代码
        org_id : str
            CNINFO 内部机构 ID
        category : str
            公告类别（如 "category_ndbg_szsh"）
        start_date : date
            开始日期
        end_date : date
            截止日期

        返回
        ----
        List[_AnnouncementEntry]
        """
        date_range = f"{start_date:%Y-%m-%d}~{end_date:%Y-%m-%d}"
        stock_key = f"{stock_code},{org_id}"

        payload = {
            "pageNum": "1",
            "pageSize": "100",
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": stock_key,
            "searchkey": "",
            "secid": "",
            "category": category,
            "trade": "",
            "seDate": date_range,
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }

        r = self._session.post(
            CNINFO_QUERY_URL,
            data=payload,
            timeout=self._timeout,
        )
        r.raise_for_status()
        result = r.json()

        entries: List[_AnnouncementEntry] = []
        for item in result.get("announcements", []):
            adjunct_url = item.get("adjunctUrl", "")
            if not adjunct_url:
                continue

            # 解析公告时间：Unix 毫秒时间戳
            ts_ms = item.get("announcementTime", 0)
            try:
                announce_time = datetime.datetime.fromtimestamp(
                    ts_ms / 1000, tz=datetime.timezone(datetime.timedelta(hours=8))
                )
            except (OSError, OverflowError, ValueError):
                announce_time = datetime.datetime.combine(
                    start_date, datetime.time(), tzinfo=None
                )

            entries.append(
                _AnnouncementEntry(
                    stock_code=stock_code,
                    org_id=org_id,
                    title=item.get("announcementTitle", ""),
                    announcement_time=announce_time,
                    adjunct_url=adjunct_url,
                    adjunct_type=item.get("adjunctType", "PDF"),
                )
            )

        return entries

    @staticmethod
    def _infer_period(
        title: str, announcement_time: datetime.datetime
    ) -> datetime.date:
        """
        从公告标题或时间推断报告期（通常是年报的年末日期）。

        规则：
        - 年报：标题中出现的年份的 12 月 31 日
        - 半年报：标题中出现的年份的 6 月 30 日
        - 一季报：标题中出现的年份的 3 月 31 日
        - 三季报：标题中出现的年份的 9 月 30 日
        - 无法推断时回退到公告时间的年份

        参数
        ----
        title : str
            公告标题
        announcement_time : datetime
            公告时间

        返回
        ----
        date
            推断的报告期
        """
        import re

        year = announcement_time.year

        # 从标题中提取年份
        year_match = re.search(r"(\d{4})", title)
        if year_match:
            year = int(year_match.group(1))

        # 判断报告类型
        if "一季报" in title or "一季" in title or "第一季度" in title:
            return datetime.date(year, 3, 31)
        if "半年报" in title or "半年" in title or "中期报告" in title or "中报" in title:
            return datetime.date(year, 6, 30)
        if "三季报" in title or "三季" in title or "第三季度" in title:
            return datetime.date(year, 9, 30)

        # 年报或其他 → 12月31日
        return datetime.date(year, 12, 31)
