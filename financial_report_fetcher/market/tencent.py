"""
腾讯免费行情 API 封装（无需 key，零外部依赖）

数据接口（均为公开的网页行情接口，仅供学习研究）：
- 实时行情（个股/指数/基金）：https://qt.gtimg.cn/q=sh600519,sz000001
- 日/周/月 K 线（前/后/不复权）：https://web.ifzq.gtimg.cn/appstock/app/fqkline/get

返回统一为 dict / list[dict]，可直接 JSON 序列化供 Web / CLI / agent 使用。
"""

import datetime as dt
import logging
import re
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_REALTIME_URL = "https://qt.gtimg.cn/q={query}"
_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

# 字段 → (索引, 键名) 的常用实时行情字段映射（按 "~" 分割后的下标）
# 参考腾讯行情返回结构：0=未知 1=名称 2=代码 3=最新价 4=昨收 5=今开
# 6=成交量(手) 30=时间 31=涨跌 32=涨跌幅 33=最高 34=最低 36=成交量 37=成交额(万)
# 38=换手率 39=市盈率 44=流通市值(亿) 45=总市值(亿) 46=市净率 47=涨停 48=跌停
_QUOTE_FIELDS = [
    (1, "name", "名称"),
    (2, "code", "代码"),
    (3, "price", "最新价"),
    (4, "prev_close", "昨收"),
    (5, "open", "今开"),
    (6, "volume", "成交量(手)"),
    (30, "time", "时间"),
    (31, "change", "涨跌"),
    (32, "change_pct", "涨跌幅%"),
    (33, "high", "最高"),
    (34, "low", "最低"),
    (37, "amount_wan", "成交额(万)"),
    (38, "turnover_rate", "换手率%"),
    (39, "pe", "市盈率"),
    (44, "float_mv_yi", "流通市值(亿)"),
    (45, "total_mv_yi", "总市值(亿)"),
    (46, "pb", "市净率"),
    (47, "limit_up", "涨停价"),
    (48, "limit_down", "跌停价"),
]


def _normalize_symbol(symbol: str) -> str:
    """
    将用户输入的股票代码规范化为腾讯格式（带市场前缀）。

    - 已带前缀（sh/sz/bj）原样返回
    - 纯数字：6 开头 → sh；0/2/3 开头 → sz；其余（4/8/9）→ bj
    - 允许附带 ".SH"/".SZ" 后缀（如 600519.SH）
    """
    s = symbol.strip().lower()
    if re.match(r"^(sh|sz|bj)\d{6}$", s):
        return s
    m = re.match(r"^(\d{6})\.(sh|sz|bj)$", s)
    if m:
        return f"{m.group(2)}{m.group(1)}"
    m = re.match(r"^(\d{6})$", s)
    if m:
        code = m.group(1)
        if code.startswith("6"):
            return f"sh{code}"
        if code.startswith(("0", "2", "3")):
            return f"sz{code}"
        return f"bj{code}"
    raise ValueError(f"无法识别的股票代码：{symbol}（支持 600519 / 600519.SH / sh600519）")


def _parse_quote_value(raw: str) -> Any:
    """把 '--' / 空串转 None，其余尽量转 float"""
    raw = raw.strip()
    if raw in ("", "--", "None", "null"):
        return None
    try:
        return float(raw)
    except ValueError:
        return raw


def _parse_realtime_line(line: str) -> Optional[Dict[str, Any]]:
    """
    解析单行 v_sh600519="1~贵州茅台~..." 格式的实时行情。

    返回键名英文的 dict（便于程序消费），并附带中文名说明文档。
    """
    m = re.match(r'v_(\w+)="(.*)"', line.strip())
    if not m:
        return None
    parts = m.group(2).split("~")
    quote: Dict[str, Any] = {"symbol": m.group(1)}
    for idx, key, _label in _QUOTE_FIELDS:
        if idx < len(parts):
            # 代码保持字符串原样（如 "600519"），其余字段尽量转数值
            quote[key] = parts[idx].strip() if key == "code" else _parse_quote_value(parts[idx])
    # 时间字段原始形如 20260811153931，转成可读格式
    raw_time = str(parts[30]) if len(parts) > 30 else ""
    if len(raw_time) == 14 and raw_time.isdigit():
        try:
            quote["time"] = dt.datetime.strptime(raw_time, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    # 买卖五档（9~28：买一价/量 ... 卖一价/量 ...）
    try:
        quote["bid_ask"] = {
            f"bid{i}": {"price": _parse_quote_value(parts[9 + (i - 1) * 2]),
                        "volume": _parse_quote_value(parts[10 + (i - 1) * 2])}
            for i in range(1, 6)
        }
        quote["bid_ask"].update({
            f"ask{i}": {"price": _parse_quote_value(parts[19 + (i - 1) * 2]),
                        "volume": _parse_quote_value(parts[20 + (i - 1) * 2])}
            for i in range(1, 6)
        })
    except IndexError:
        pass
    return quote


class TencentQuote:
    """腾讯免费行情数据源。"""

    def __init__(self, timeout: int = 10) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://gu.qq.com/",
        })
        self._timeout = timeout

    # ─────────────────────────────────────────────────────────────────
    # 实时行情
    # ─────────────────────────────────────────────────────────────────

    def realtime(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """
        获取个股实时行情（可批量）。

        参数
        ----
        symbols : List[str]
            股票代码列表，如 ["600519", "000001"]，支持 sh/sz/bj 前缀。

        返回
        ----
        List[Dict[str, Any]]
            每项含 name/code/price/prev_close/open/high/low/volume/amount/
            change/change_pct/pe/pb/total_mv_yi 等字段。
        """
        query = ",".join(_normalize_symbol(s) for s in symbols)
        resp = self._session.get(_REALTIME_URL.format(query=query), timeout=self._timeout)
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
        quotes = []
        for line in text.splitlines():
            if not line.strip():
                continue
            q = _parse_realtime_line(line)
            if q is not None and q.get("code"):
                quotes.append(q)
        return quotes

    def index(self, codes: List[str]) -> List[Dict[str, Any]]:
        """
        获取指数实时行情。

        参数
        ----
        codes : List[str]
            指数代码（需带前缀），如 ["sh000001", "sz399001", "sz399006"]。

        返回结构同 realtime()。
        """
        query = ",".join(c.strip().lower() for c in codes)
        resp = self._session.get(_REALTIME_URL.format(query=query), timeout=self._timeout)
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
        quotes = []
        for line in text.splitlines():
            q = _parse_realtime_line(line)
            if q is not None and q.get("code"):
                quotes.append(q)
        return quotes

    # ─────────────────────────────────────────────────────────────────
    # K 线
    # ─────────────────────────────────────────────────────────────────

    def kline(self, symbol: str, period: str = "day", count: int = 320,
              adjust: str = "qfq") -> List[Dict[str, Any]]:
        """
        获取历史 K 线（日/周/月，支持复权）。

        参数
        ----
        symbol : str
            股票代码，如 "600519" 或 "sh600519"。
        period : str
            "day"（日）/ "week"（周）/ "month"（月）。
        count : int
            返回最近 K 线根数，1~800。
        adjust : str
            复权方式："qfq"（前复权）/ "hfq"（后复权）/ "none"（不复权）。

        返回
        ----
        List[Dict[str, Any]]
            每项含 date/open/close/high/low/volume（volume 单位：手）。
        """
        sym = _normalize_symbol(symbol)
        if period not in ("day", "week", "month"):
            raise ValueError(f"period 仅支持 day/week/month，收到：{period}")
        if adjust not in ("qfq", "hfq", "none"):
            raise ValueError(f"adjust 仅支持 qfq/hfq/none，收到：{adjust}")
        count = max(1, min(int(count), 800))

        params = {"param": f"{sym},{period},,,{count},{adjust}"}
        resp = self._session.get(_KLINE_URL, params=params, timeout=self._timeout)
        resp.raise_for_status()
        payload = resp.json()
        node = (payload.get("data") or {}).get(sym) or {}
        # 复权数据在 qfqday/hfqday 键下，不复权在 day/week/month 键下
        key_map = {"qfq": "qfq" + period, "hfq": "hfq" + period, "none": period}
        rows = node.get(key_map[adjust]) or []
        result = []
        for row in rows:
            # 腾讯 K 线行：[日期, 开, 收, 高, 低, 成交量, ...]
            if len(row) < 6:
                continue
            result.append({
                "date": row[0],
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]),
            })
        return result
