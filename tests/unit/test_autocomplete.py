"""StockIndex 自动补全单元测试"""

import threading

import pytest

from webapp.autocomplete import StockIndex

FAKE_COMPANIES = {
    "600900": {"org_id": "1", "name": "长江电力"},
    "600519": {"org_id": "2", "name": "贵州茅台"},
    "000651": {"org_id": "3", "name": "珠海格力电器"},
    "长江电力": {"org_id": "1", "code": "600900"},
    "贵州茅台": {"org_id": "2", "code": "600519"},
    "珠海格力电器": {"org_id": "3", "code": "000651"},
}


class FakeDatasource:
    def build_known_companies(self):
        return FAKE_COMPANIES


class FailingDatasource:
    """第一次 build_known_companies 抛异常，之后恢复正常"""

    def __init__(self) -> None:
        self.calls = 0

    def build_known_companies(self):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("CNINFO 临时故障")
        return FAKE_COMPANIES


def _ready_index():
    idx = StockIndex(datasource=FakeDatasource())
    idx.start()  # 后台线程构建
    idx.wait_ready(timeout=2.0)  # 测试同步等待
    return idx


def test_code_prefix_match():
    idx = _ready_index()
    assert idx.search("6009") == [{"code": "600900", "name": "长江电力"}]


def test_name_prefix_beats_name_contains():
    idx = _ready_index()
    results = idx.search("长江")
    assert results == [{"code": "600900", "name": "长江电力"}]


def test_name_contains_match():
    idx = _ready_index()
    results = idx.search("格力")
    assert results == [{"code": "000651", "name": "珠海格力电器"}]


def test_top_n_limit():
    idx = _ready_index()
    assert len(idx.search("60", limit=1)) == 1


def test_empty_query_returns_empty():
    idx = _ready_index()
    assert idx.search("") == []
    assert idx.search("   ") == []


def test_no_match_returns_empty():
    idx = _ready_index()
    assert idx.search("zzz") == []


def test_company_name_lookup():
    idx = _ready_index()
    assert idx.company_name("600900") == "长江电力"
    assert idx.company_name("999999") is None


def test_build_failure_allows_retry():
    idx = StockIndex(datasource=FailingDatasource())
    idx.start()
    # 首次构建失败：wait_ready 返回 False（尚未就绪），搜索为空
    assert idx.wait_ready(timeout=0.2) is False
    assert idx.search("6009") == []
    # 网络恢复后再次 start() 应触发重建，而非永久短路
    idx.start()
    assert idx.wait_ready(timeout=2.0) is True
    assert idx.search("6009") == [{"code": "600900", "name": "长江电力"}]


def test_is_valid_code():
    idx = StockIndex(datasource=FakeDatasource())
    assert idx.is_valid_code("600900") is False  # 未就绪
    idx.start()
    assert idx.wait_ready(timeout=2.0) is True
    assert idx.is_valid_code("600900") is True
    assert idx.is_valid_code("999999") is False
    assert idx.is_valid_code("abc") is False