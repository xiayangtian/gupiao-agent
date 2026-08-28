"""
tests/unit/test_market_tencent.py

腾讯行情封装单元测试：代码规范化、字段解析（内置样例数据，不联网）。
"""

import pytest

from financial_report_fetcher.market.tencent import (
    TencentQuote,
    _normalize_symbol,
    _parse_quote_value,
    _parse_realtime_line,
)


# ── _normalize_symbol ────────────────────────────────────────────────────

class TestNormalizeSymbol:
    def test_plain_6x(self):
        assert _normalize_symbol("600519") == "sh600519"

    def test_plain_0x_and_3x(self):
        assert _normalize_symbol("000001") == "sz000001"
        assert _normalize_symbol("300750") == "sz300750"

    def test_plain_bj(self):
        assert _normalize_symbol("830799") == "bj830799"

    def test_with_suffix(self):
        assert _normalize_symbol("600519.SH") == "sh600519"
        assert _normalize_symbol("000001.sz") == "sz000001"

    def test_already_prefixed(self):
        assert _normalize_symbol("sh600519") == "sh600519"
        assert _normalize_symbol("SZ000001") == "sz000001"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _normalize_symbol("hello")


# ── _parse_quote_value ───────────────────────────────────────────────────

class TestParseQuoteValue:
    def test_empty_to_none(self):
        assert _parse_quote_value("") is None
        assert _parse_quote_value("--") is None

    def test_numeric(self):
        assert _parse_quote_value("1346.50") == 1346.5

    def test_non_numeric_kept(self):
        assert _parse_quote_value("abc") == "abc"


# ── _parse_realtime_line ─────────────────────────────────────────────────

# 取自腾讯 qt.gtimg.cn 的真实返回样例（贵州茅台，2026-08-11）
_SAMPLE_LINE = (
    'v_sh600519="1~贵州茅台~600519~1346.50~1348.86~1348.00~27073~11307~15766'
    '~1346.43~2~1346.32~6~1346.30~6~1346.29~6~1346.10~5~1346.50~69~1346.53~1'
    '~1346.54~2~1346.57~3~1346.59~6~~20260811153931~-2.36~-0.17~1352.65~1338.00'
    '~1346.50/27073/3640046368~27073~364005~0.22~20.35~~1352.65~1338.00~1.09'
    '~16832.35~16832.35~7.23~1483.75~1213.97~0.70~-56~1344.53~15.45~20.45~~~0.19'
    '~364004.6368~282.7650~21~   A~GP-A~-0.20~1.37~3.86~30.53~26.78~1539.98~1151.01'
    '~2.01~10.83~3.98~1250081601~1250081601~-52.83~-5.07~1250081601~~~-3.34~0.03'
    '~~CNY~0~___D__F__N~1346.73~-10~";'
)


class TestParseRealtimeLine:
    def test_basic_fields(self):
        q = _parse_realtime_line(_SAMPLE_LINE)
        assert q is not None
        assert q["symbol"] == "sh600519"
        assert q["code"] == "600519"          # 代码保持字符串
        assert q["name"] == "贵州茅台"
        assert q["price"] == 1346.5
        assert q["prev_close"] == 1348.86
        assert q["open"] == 1348.0
        assert q["high"] == 1352.65
        assert q["low"] == 1338.0
        assert q["change"] == -2.36
        assert q["change_pct"] == -0.17
        assert q["volume"] == 27073.0
        assert q["pe"] == 20.35
        assert q["pb"] == 7.23
        assert q["total_mv_yi"] == 16832.35

    def test_time_formatted(self):
        q = _parse_realtime_line(_SAMPLE_LINE)
        assert q["time"] == "2026-08-11 15:39:31"

    def test_bid_ask_present(self):
        q = _parse_realtime_line(_SAMPLE_LINE)
        assert q["bid_ask"]["bid1"]["price"] == 1346.43
        assert q["bid_ask"]["ask1"]["price"] == 1346.50
        assert q["bid_ask"]["ask2"]["price"] == 1346.53

    def test_malformed_line_returns_none(self):
        assert _parse_realtime_line("not-a-quote-line") is None


# ── TencentQuote 网络层（mock requests，不真正联网）────────────────────

class _FakeResponse:
    def __init__(self, content: bytes, payload=None):
        self.content = content
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_realtime_parses_lines(monkeypatch):
    body = _SAMPLE_LINE.encode("gbk")
    tq = TencentQuote()
    monkeypatch.setattr(tq._session, "get", lambda url, timeout: _FakeResponse(body))
    quotes = tq.realtime(["600519"])
    assert len(quotes) == 1
    assert quotes[0]["name"] == "贵州茅台"
    assert quotes[0]["price"] == 1346.5


def test_kline_parses_rows(monkeypatch):
    payload = {
        "code": 0,
        "data": {
            "sh600519": {
                "qfqday": [
                    ["2026-08-10", "1325.00", "1348.86", "1359.97", "1318.08", "62686.000"],
                    ["2026-08-11", "1348.00", "1346.50", "1352.65", "1338.00", "27073.000"],
                ]
            }
        },
    }
    tq = TencentQuote()
    monkeypatch.setattr(tq._session, "get", lambda url, params, timeout: _FakeResponse(b"", payload))
    bars = tq.kline("600519", count=2)
    assert len(bars) == 2
    assert bars[0]["date"] == "2026-08-10"
    assert bars[0]["close"] == 1348.86
    assert bars[1]["volume"] == 27073.0


def test_kline_invalid_period_raises(monkeypatch):
    tq = TencentQuote()
    with pytest.raises(ValueError):
        tq.kline("600519", period="year")
