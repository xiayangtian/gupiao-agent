"""公司/行业 Scope 解析的单元测试。

覆盖：focus_report 硬单报告边界、行业关键词按需扩展、行业失败回退、
本地可检索同业样本限制、显式 mode 覆盖、无公司上下文的全库回退，以及
行业分类缓存的重用与过期重查。
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from webapp.chat_models import IndustryRef
from webapp.chat_scope import ScopeRequest, ScopeResolver


TARGET_REPORT = "601288:2026-06-30:semi_annual"
TARGET_OTHER_REPORT = "601288:2025-12-31:annual"
PEER_REPORT = "600000:2026-06-30:semi_annual"
OTHER_REPORT = "600900:2026-06-30:semi_annual"

INDUSTRIES = {
    "601288": IndustryRef("银行业", "company-profile"),
    "600000": IndustryRef("银行业", "company-profile"),
    "600900": IndustryRef("电力", "company-profile"),
}

NAMES = {"601288": "农业银行", "600000": "浦发银行", "600900": "长江电力"}


def _resolver(report_ids, industries, *, names=None, cache_path=None, now=None):
    return ScopeResolver(
        lambda: list(report_ids),
        lambda code: (names or {}).get(code),
        lambda code: industries.get(code),
        cache_path=cache_path,
        now=now,
    )


# ── brief 规定的基础用例 ─────────────────────────────────────

def test_focus_report_resolves_to_hard_company_only_scope():
    resolver = _resolver([TARGET_REPORT, PEER_REPORT, OTHER_REPORT], INDUSTRIES, names=NAMES)

    scope = resolver.resolve("营收是多少？", ScopeRequest.auto({"code": "601288", "period": "2026-06-30"}))

    assert scope.mode == "company_only"
    assert scope.report_ids == (TARGET_REPORT,)


def test_industry_keyword_expands_only_to_local_same_industry_reports():
    resolver = _resolver([TARGET_REPORT, PEER_REPORT, OTHER_REPORT], INDUSTRIES, names=NAMES)

    scope = resolver.resolve("在行业中收入增速排第几？", ScopeRequest.auto({"code": "601288", "period": "2026-06-30"}))

    assert scope.mode == "company_industry"
    assert set(scope.report_ids) == {TARGET_REPORT, PEER_REPORT}
    assert scope.industry.sample_kind == "local_indexed"


def test_unavailable_industry_falls_back_to_company_only_with_reason():
    resolver = _resolver(
        [TARGET_REPORT, PEER_REPORT, OTHER_REPORT],
        {"601288": None, "600000": None, "600900": None},
        names=NAMES,
    )

    scope = resolver.resolve("同业竞争如何？", ScopeRequest.auto({"code": "601288", "period": "2026-06-30"}))

    assert scope.mode == "company_only"
    assert scope.report_ids == (TARGET_REPORT,)
    assert "行业分类" in scope.fallback_reason


# ── 台账裁定要求的显式边界用例 ───────────────────────────────

def test_focus_report_resolves_to_single_report_even_with_other_company_reports():
    resolver = _resolver([TARGET_OTHER_REPORT, TARGET_REPORT, PEER_REPORT], INDUSTRIES, names=NAMES)

    scope = resolver.resolve("营收是多少？", ScopeRequest.auto({"code": "601288", "period": "2026-06-30"}))

    assert scope.mode == "company_only"
    assert scope.report_ids == (TARGET_REPORT,)


def test_resolved_company_without_focus_includes_all_company_reports():
    resolver = _resolver([TARGET_OTHER_REPORT, TARGET_REPORT, PEER_REPORT], INDUSTRIES, names=NAMES)

    scope = resolver.resolve("601288 的营收是多少？", ScopeRequest.auto(None))

    assert scope.mode == "company_only"
    assert scope.report_ids == (TARGET_OTHER_REPORT, TARGET_REPORT)
    assert scope.companies[0].name == "农业银行"


# ── 显式 mode 与无公司上下文 ─────────────────────────────────

def test_explicit_company_only_overrides_auto_industry_keyword():
    resolver = _resolver([TARGET_REPORT, PEER_REPORT], INDUSTRIES, names=NAMES)

    scope = resolver.resolve("同业竞争如何？", ScopeRequest.company_only({"code": "601288", "period": "2026-06-30"}))

    assert scope.mode == "company_only"
    assert scope.report_ids == (TARGET_REPORT,)


def test_explicit_company_industry_expands_without_keyword():
    resolver = _resolver([TARGET_REPORT, PEER_REPORT], INDUSTRIES, names=NAMES)

    scope = resolver.resolve("营收多少？", ScopeRequest.company_industry({"code": "601288", "period": "2026-06-30"}))

    assert scope.mode == "company_industry"
    assert set(scope.report_ids) == {TARGET_REPORT, PEER_REPORT}


def test_explicit_whole_corpus_ignores_focus_report():
    resolver = _resolver([TARGET_REPORT, PEER_REPORT], INDUSTRIES, names=NAMES)

    scope = resolver.resolve("营收多少？", ScopeRequest.whole_corpus())

    assert scope.mode == "whole_corpus"
    assert scope.report_ids == ()
    assert scope.companies == ()


def test_auto_without_company_resolves_whole_corpus():
    resolver = _resolver([TARGET_REPORT], INDUSTRIES, names=NAMES)

    scope = resolver.resolve("最近有什么报告？", ScopeRequest.auto(None))

    assert scope.mode == "whole_corpus"
    assert scope.report_ids == ()


def test_industry_with_no_local_peers_falls_back_to_company_only():
    resolver = _resolver([TARGET_REPORT], {"601288": IndustryRef("银行业", "company-profile")}, names=NAMES)

    scope = resolver.resolve("同业对比", ScopeRequest.auto({"code": "601288", "period": "2026-06-30"}))

    assert scope.mode == "company_only"
    assert scope.report_ids == (TARGET_REPORT,)
    assert "同业" in scope.fallback_reason


# ── 行业分类缓存 ─────────────────────────────────────────────

def test_industry_resolution_is_cached_and_reused(tmp_path):
    cache_path = str(tmp_path / "company_industries.json")
    calls = []

    def industry_provider(code):
        calls.append(code)
        return IndustryRef("银行业", "company-profile")

    resolver = ScopeResolver(
        lambda: [TARGET_REPORT, PEER_REPORT],
        lambda code: NAMES.get(code),
        industry_provider,
        cache_path=cache_path,
    )

    scope = resolver.resolve("同业对比", ScopeRequest.auto({"code": "601288", "period": "2026-06-30"}))

    assert scope.mode == "company_industry"
    assert calls == ["601288", "600000"]
    cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    assert cache["601288"]["industry_name"] == "银行业"
    assert cache["601288"]["provider"] == "company-profile"

    # 新 resolver + 失效 provider：命中缓存后不应再调用 provider
    fresh_calls = []

    def failing_provider(code):
        fresh_calls.append(code)
        return None

    resolver2 = ScopeResolver(
        lambda: [TARGET_REPORT, PEER_REPORT],
        lambda code: NAMES.get(code),
        failing_provider,
        cache_path=cache_path,
    )
    scope2 = resolver2.resolve("同业对比", ScopeRequest.auto({"code": "601288", "period": "2026-06-30"}))

    assert scope2.mode == "company_industry"
    assert set(scope2.report_ids) == {TARGET_REPORT, PEER_REPORT}
    assert fresh_calls == []


def test_expired_industry_cache_refetches_provider(tmp_path):
    cache_path = str(tmp_path / "company_industries.json")
    calls = []
    clock = [datetime(2026, 9, 10, tzinfo=timezone.utc)]

    def industry_provider(code):
        calls.append(code)
        return IndustryRef("银行业", "company-profile")

    resolver = ScopeResolver(
        lambda: [TARGET_REPORT, PEER_REPORT],
        lambda code: NAMES.get(code),
        industry_provider,
        cache_path=cache_path,
        now=lambda: clock[0],
    )
    resolver.resolve("同业对比", ScopeRequest.auto({"code": "601288", "period": "2026-06-30"}))
    assert calls == ["601288", "600000"]

    # 缓存有效期默认 7 天，越过有效期后应重新查询 provider
    clock[0] = datetime(2026, 9, 20, tzinfo=timezone.utc)
    resolver2 = ScopeResolver(
        lambda: [TARGET_REPORT, PEER_REPORT],
        lambda code: NAMES.get(code),
        industry_provider,
        cache_path=cache_path,
        now=lambda: clock[0],
    )
    scope2 = resolver2.resolve("同业对比", ScopeRequest.auto({"code": "601288", "period": "2026-06-30"}))

    assert scope2.mode == "company_industry"
    assert calls == ["601288", "600000", "601288", "600000"]
