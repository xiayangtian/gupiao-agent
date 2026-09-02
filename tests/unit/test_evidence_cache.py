"""证据缓存键、原子写入和损坏降级测试。"""

import json
from decimal import Decimal

from financial_report_fetcher.evidence.cache import EvidenceCache, make_cache_key
from financial_report_fetcher.evidence.models import (
    EntityScope,
    EvidenceRecord,
    SourceLocator,
    SourceType,
    VerificationState,
)


def _record() -> EvidenceRecord:
    return EvidenceRecord(
        report_id="600900:2025-12-31:annual",
        entity_scope=EntityScope.CONSOLIDATED,
        fact_name="revenue",
        value=Decimal("100.01"),
        unit="yuan",
        currency="CNY",
        period="2025-12-31",
        source_type=SourceType.STRUCTURED,
        source_locator=SourceLocator(provider="akshare", record_id="income:2025"),
        extraction_confidence=0.95,
        verification_state=VerificationState.SINGLE_SOURCE,
        content_hash="source-hash",
        parser_version="akshare-v1",
    )


def test_cache_key_is_stable_across_mapping_order_and_changes_with_parser():
    """映射顺序不能导致缓存未命中，解析器升级必须换键。"""
    first = make_cache_key(
        "r1", "pdf-hash", {"tushare": "v1", "akshare": "v1"},
        {"pdf": "v1", "ocr": "v3"},
    )
    reordered = make_cache_key(
        "r1", "pdf-hash", {"akshare": "v1", "tushare": "v1"},
        {"ocr": "v3", "pdf": "v1"},
    )
    upgraded = make_cache_key(
        "r1", "pdf-hash", {"akshare": "v1", "tushare": "v1"},
        {"ocr": "v4", "pdf": "v1"},
    )

    assert first == reordered
    assert len(first) == 64
    assert upgraded != first


def test_cache_round_trip_preserves_evidence_and_uses_key_path(tmp_path):
    """缓存往返必须保留 Decimal 和来源定位。"""
    cache = EvidenceCache(tmp_path)
    record = _record()

    saved = cache.save("a" * 64, [record])

    assert saved == tmp_path / f"{'a' * 64}.json"
    assert cache.load("a" * 64) == [record]
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_cache_is_quarantined_and_returns_none(tmp_path):
    """损坏缓存不得返回部分证据，原文件需隔离以便排查。"""
    cache = EvidenceCache(tmp_path)
    path = tmp_path / f"{'b' * 64}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"records": [', encoding="utf-8")

    assert cache.load("b" * 64) is None
    assert not path.exists()
    quarantined = list(tmp_path.glob(f"{'b' * 64}.json.corrupt*"))
    assert len(quarantined) == 1


def test_cache_rejects_wrong_schema_as_corrupt(tmp_path):
    """未知缓存版本不能被误读为当前证据。"""
    cache = EvidenceCache(tmp_path)
    path = tmp_path / f"{'c' * 64}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 99, "records": []}), encoding="utf-8")

    assert cache.load("c" * 64) is None
    assert list(tmp_path.glob(f"{'c' * 64}.json.corrupt*"))
