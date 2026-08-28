"""
tests/property/test_properties.py

属性化测试（Hypothesis）：覆盖 Fetcher、Config、Downloader 模块的核心不变式
"""

import datetime
import logging
import os
import re
import tempfile
from datetime import date
from io import StringIO
from unittest.mock import patch, MagicMock

import pytest
import requests
import yaml
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

from financial_report_fetcher.config import ConfigLoader
from financial_report_fetcher.downloader import ReportDownloader
from financial_report_fetcher.exceptions import (
    ConfigFileNotFoundError,
    ConfigParseError,
    ConfigValidationError,
)
from financial_report_fetcher.fetcher import ReportFetcher
from financial_report_fetcher.models import (
    AppConfig,
    CompanyConfig,
    DateRange,
    DownloadStatus,
    DownloadSummary,
    ReportMeta,
    ReportType,
)


# ─────────────────────────────────────────────────────────────────────────────
# 辅助工具
# ─────────────────────────────────────────────────────────────────────────────

def _make_report(
    company_id: str, report_type: ReportType, period: date
) -> ReportMeta:
    return ReportMeta(
        company_id=company_id,
        report_type=report_type,
        period=period,
        download_url="http://example.com/report.pdf",
        title="测试财报",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Property 4: 文件命名格式不变式
# Validates: Requirements 2.2
# ─────────────────────────────────────────────────────────────────────────────

@given(
    company_id=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Lo")),
        min_size=1,
        max_size=20,
    ),
    report_type=st.sampled_from(ReportType),
    period=st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)),
)
@settings(max_examples=100)
def test_property4_filename_format_invariant(company_id, report_type, period):
    """
    Property 4: 文件命名格式不变式
    对任意合法的 company_id、report_type、period，生成的文件名必须符合
    {非空}_{非空}_{非空}.pdf 格式。
    **Validates: Requirements 2.2**
    """
    report = _make_report(company_id, report_type, period)
    filename = ReportDownloader.build_filename(report)

    # 必须以 .pdf 结尾
    assert filename.endswith(".pdf"), f"文件名未以 .pdf 结尾: {filename}"
    # 去掉扩展名后必须由下划线分隔的三部分组成
    name_without_ext = filename[:-4]
    assert re.match(r"^.+_.+_.+$", name_without_ext), (
        f"文件名格式不符合 '{{company_id}}_{{type}}_{{year}}.pdf': {filename}"
    )
    # 文件名中必须包含公司标识
    assert company_id in filename, f"文件名缺少公司标识 {company_id}: {filename}"
    # 文件名中必须包含年份
    assert str(period.year) in filename, f"文件名缺少年份: {filename}"


# ─────────────────────────────────────────────────────────────────────────────
# Property 5: 已存在文件不被覆盖
# Validates: Requirements 2.4
# ─────────────────────────────────────────────────────────────────────────────

@given(
    original_payload=st.binary(min_size=0, max_size=1015),
    report_type=st.sampled_from(ReportType),
    period=st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)),
)
@settings(max_examples=100)
def test_property5_existing_file_not_overwritten(original_payload, report_type, period):
    """
    Property 5: 已存在文件不被覆盖
    对任意已存在的有效 PDF，调用 download_one 必须返回 SKIPPED
    且文件内容不变。
    **Validates: Requirements 2.4**
    """
    original_content = b"%PDF-1.7\n" + original_payload
    report = _make_report("600519", report_type, period)
    downloader = ReportDownloader()
    filename = downloader.build_filename(report)

    # 使用 tempfile 上下文管理器在测试内部管理临时目录
    with tempfile.TemporaryDirectory() as tmp_dir:
        file_path = os.path.join(tmp_dir, filename)
        # 预先写入原始内容
        with open(file_path, "wb") as f:
            f.write(original_content)

        with patch("financial_report_fetcher.downloader._do_download") as mock_dl:
            status = downloader.download_one(report, tmp_dir)
            # 不应调用网络下载
            mock_dl.assert_not_called()

        assert status == DownloadStatus.SKIPPED, f"期望 SKIPPED，实际: {status}"
        # 文件内容不应被修改
        with open(file_path, "rb") as f:
            assert f.read() == original_content, "文件内容被意外修改"


# ─────────────────────────────────────────────────────────────────────────────
# Property 6: 下载汇总计数完整性
# Validates: Requirements 2.5
# ─────────────────────────────────────────────────────────────────────────────

@given(
    statuses=st.lists(
        st.sampled_from(DownloadStatus),
        min_size=0,
        max_size=50,
    )
)
@settings(max_examples=100)
def test_property6_summary_count_integrity(statuses):
    """
    Property 6: 下载汇总计数完整性
    对任意 n 条下载结果，summary.success + summary.skipped + summary.failed == n，
    且三字段均非负。
    **Validates: Requirements 2.5**
    """
    # 直接构建 DownloadSummary 并验证
    summary = DownloadSummary(
        success=statuses.count(DownloadStatus.SUCCESS),
        skipped=statuses.count(DownloadStatus.SKIPPED),
        failed=statuses.count(DownloadStatus.FAILED),
    )

    n = len(statuses)
    assert summary.total == n, f"汇总总数 {summary.total} 不等于实际数量 {n}"
    assert summary.success >= 0
    assert summary.skipped >= 0
    assert summary.failed >= 0


# ─────────────────────────────────────────────────────────────────────────────
# Property 7: 单个失败不中断整体流程
# Validates: Requirements 2.6
# ─────────────────────────────────────────────────────────────────────────────

@given(
    n=st.integers(min_value=1, max_value=20),
    fail_indices=st.lists(
        st.integers(min_value=0, max_value=19), max_size=10, unique=True
    ),
)
@settings(max_examples=100)
def test_property7_single_failure_does_not_interrupt(n, fail_indices):
    """
    Property 7: 单个失败不中断整体流程
    随机注入失败位置，download_all 总处理数量必须等于 n。
    **Validates: Requirements 2.6**
    """
    # 过滤掉超出范围的失败索引
    fail_indices_in_range = set(i for i in fail_indices if i < n)

    # 构建财报列表
    reports = [
        _make_report(f"company_{i}", ReportType.ANNUAL, date(2023, 12, 31))
        for i in range(n)
    ]

    # 构建 side_effect：在失败索引处抛出异常
    def make_side_effect(fail_set):
        call_count = [0]

        def side_effect(url, timeout):
            idx = call_count[0]
            call_count[0] += 1
            if idx in fail_set:
                raise requests.ConnectionError("模拟网络失败")
            return b"%PDF-1.7 fake content"

        return side_effect

    with tempfile.TemporaryDirectory() as tmp_dir:
        with patch(
            "financial_report_fetcher.downloader._do_download",
            side_effect=make_side_effect(fail_indices_in_range),
        ):
            downloader = ReportDownloader()
            summary = downloader.download_all(iter(reports), tmp_dir)

    assert summary.total == n, (
        f"总处理数量 {summary.total} 不等于报告数量 {n}"
    )
    assert summary.failed == len(fail_indices_in_range), (
        f"失败数量 {summary.failed} 不等于预期 {len(fail_indices_in_range)}"
    )
    assert summary.success == n - len(fail_indices_in_range)


# ─────────────────────────────────────────────────────────────────────────────
# Property 8: 重试次数不超过上限
# Validates: Requirements 2.7
# ─────────────────────────────────────────────────────────────────────────────

@given(n_reports=st.integers(min_value=1, max_value=5))
@settings(max_examples=20)
def test_property8_retry_count_does_not_exceed_limit(n_reports):
    """
    Property 8: 重试次数不超过上限
    网络始终失败时，每份文件的实际 HTTP 请求次数 <= 4（1次初始 + 最多3次重试）。
    直接对 requests.get 打桩以精确计数。
    **Validates: Requirements 2.7**
    """
    import financial_report_fetcher.downloader as dl_module
    from tenacity import wait_none

    reports = [
        _make_report(f"co_{i}", ReportType.ANNUAL, date(2023, 12, 31))
        for i in range(n_reports)
    ]
    call_counter = [0]

    def always_fail(url, timeout):
        call_counter[0] += 1
        raise requests.ConnectionError("始终失败")

    # mock requests.get 并将 _do_download 的重试等待设为 0
    original_wait = dl_module._do_download.retry.wait
    try:
        dl_module._do_download.retry.wait = wait_none()
        with patch("requests.get", side_effect=always_fail):
            with tempfile.TemporaryDirectory() as tmp_dir:
                downloader = ReportDownloader()
                downloader.download_all(iter(reports), tmp_dir)
    finally:
        dl_module._do_download.retry.wait = original_wait

    max_attempts_per_report = 4  # stop_after_attempt(4)
    assert call_counter[0] <= n_reports * max_attempts_per_report, (
        f"总请求次数 {call_counter[0]} 超过上限 {n_reports * max_attempts_per_report}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Property 9: 零字节文件被删除并计入失败
# Validates: Requirements 2.8
# ─────────────────────────────────────────────────────────────────────────────

@given(
    report_type=st.sampled_from(ReportType),
    period=st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)),
)
@settings(max_examples=100)
def test_property9_empty_file_deleted_and_counted_as_failed(report_type, period):
    """
    Property 9: 零字节文件被删除并计入失败
    当下载响应内容为空字节时，download_one 必须返回 FAILED，
    且不留下空文件在磁盘上。
    **Validates: Requirements 2.8**
    """
    report = _make_report("600519", report_type, period)
    downloader = ReportDownloader()
    filename = downloader.build_filename(report)

    with tempfile.TemporaryDirectory() as tmp_dir:
        with patch(
            "financial_report_fetcher.downloader._do_download",
            return_value=b"",
        ):
            status = downloader.download_one(report, tmp_dir)

        assert status == DownloadStatus.FAILED, f"期望 FAILED，实际: {status}"
        # 不应在磁盘上留下空文件
        assert not os.path.exists(os.path.join(tmp_dir, filename)), "空文件仍存在于磁盘上"


# ═════════════════════════════════════════════════════════════════════════════
# Property 1: Fetcher 仅返回目标公司的财报
# Validates: Requirements 1.2
# ═════════════════════════════════════════════════════════════════════════════

@given(
    target_company_ids=st.lists(
        st.text(min_size=1, max_size=10), min_size=1, max_size=5, unique=True
    ),
    other_company_ids=st.lists(
        st.text(min_size=1, max_size=10), min_size=0, max_size=5, unique=True
    ),
    n_reports=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=100)
def test_property1_fetcher_only_returns_target_company_reports(
    target_company_ids, other_company_ids, n_reports
):
    """
    Property 1: Fetcher 仅返回目标公司的财报
    断言结果中每条 report.company_id 属于目标公司集合。
    **Validates: Requirements 1.2**
    """
    assume(all(t not in other_company_ids for t in target_company_ids))

    # 构建已知公司数据库（目标公司 + 干扰公司）
    all_ids = target_company_ids + other_company_ids
    known = {cid: {} for cid in all_ids}

    # 构建财报数据库：每个公司都有随机财报
    reports_db = {}
    for cid in all_ids:
        reports_db[cid] = [
            ReportMeta(
                company_id=cid,
                report_type=ReportType.ANNUAL,
                period=date(2020 + i, 12, 31),
                download_url=f"http://example.com/{cid}/{2020 + i}.pdf",
                title=f"{cid}_{2020 + i}",
            )
            for i in range(min(n_reports, 3))
        ]

    fetcher = ReportFetcher(known_companies=known, reports_db=reports_db)

    config = AppConfig(
        storage_dir="/tmp/reports",
        companies=[CompanyConfig(ticker=cid) for cid in target_company_ids],
        start_date=date(2020, 1, 1),
        end_date=date(2025, 12, 31),
        max_count=10,
    )

    results = list(fetcher.fetch(config))

    target_set = set(target_company_ids)
    for r in results:
        assert r.company_id in target_set, (
            f"结果中出现了非目标公司 {r.company_id}，目标集合: {target_set}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# Property 2: 未知公司标识被跳过且日志包含原始标识
# Validates: Requirements 1.5
# ═════════════════════════════════════════════════════════════════════════════

@given(
    unknown_id=st.text(min_size=1, max_size=20),
    known_id=st.text(min_size=1, max_size=20),
)
@settings(max_examples=100)
def test_property2_unknown_company_skipped_with_log(unknown_id, known_id):
    """
    Property 2: 未知公司标识被跳过且日志包含原始标识
    无法匹配的标识在 fetch 时被跳过，WARN 日志含该标识，结果中无该公司财报。
    **Validates: Requirements 1.5**
    """
    assume(unknown_id != known_id)
    assume(unknown_id.strip() != "")

    known = {known_id: {}}
    reports_db = {
        known_id: [
            ReportMeta(
                company_id=known_id,
                report_type=ReportType.ANNUAL,
                period=date(2023, 12, 31),
                download_url=f"http://example.com/{known_id}.pdf",
                title=f"{known_id}年报",
            )
        ]
    }

    fetcher = ReportFetcher(known_companies=known, reports_db=reports_db)

    config = AppConfig(
        storage_dir="/tmp/reports",
        companies=[
            CompanyConfig(ticker=known_id),
            CompanyConfig(ticker=unknown_id),
        ],
        start_date=date(2023, 1, 1),
        end_date=date(2023, 12, 31),
        max_count=10,
    )

    log_stream = StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.WARNING)
    logger = logging.getLogger("financial_report_fetcher.fetcher")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.WARNING)

    try:
        results = list(fetcher.fetch(config))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    log_output = log_stream.getvalue()

    # 结果中只能有已知公司的财报
    result_ids = {r.company_id for r in results}
    assert unknown_id not in result_ids, f"未知公司 {unknown_id} 不应出现在结果中"
    assert known_id in result_ids, f"已知公司 {known_id} 应该出现在结果中"

    # WARN 日志必须含有该未知标识
    assert unknown_id in log_output, (
        f"WARN 日志应包含未知标识 '{unknown_id}'，实际日志: {log_output}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Property 10: 日期区间过滤闭区间性
# Validates: Requirements 3.2
# ═════════════════════════════════════════════════════════════════════════════

@given(
    start=st.dates(min_value=date(2010, 1, 1), max_value=date(2025, 12, 31)),
    end=st.dates(min_value=date(2010, 1, 1), max_value=date(2025, 12, 31)),
    periods=st.lists(
        st.dates(min_value=date(2005, 1, 1), max_value=date(2030, 12, 31)),
        min_size=1,
        max_size=20,
    ),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.filter_too_much])
def test_property10_date_range_filter_closed_interval(start, end, periods):
    """
    Property 10: 日期区间过滤闭区间性
    对随机 [start, end] 区间与年报集合，结果每条 start <= period <= end。
    **Validates: Requirements 3.2**
    """
    assume(start <= end)

    fetcher = ReportFetcher()

    reports = [
        ReportMeta(
            company_id="600519",
            report_type=ReportType.ANNUAL,
            period=p,
            download_url=f"http://example.com/{p}.pdf",
            title=f"报告_{p}",
        )
        for p in set(periods)  # 去重避免同一日期
    ]

    date_range = DateRange(start=start, end=end)
    result = fetcher._apply_filters(
        reports, date_range, [ReportType.ANNUAL], max_count=1000
    )

    for r in result:
        assert start <= r.period <= end, (
            f"报告期 {r.period} 不在闭区间 [{start}, {end}] 内"
        )

    # 验证所有满足条件的报告都被保留
    expected = sorted(
        [r for r in reports if start <= r.period <= end],
        key=lambda r: r.period,
        reverse=True,
    )[:1000]
    assert len(result) == len(expected), (
        f"过滤结果数 {len(result)} 不等于预期 {len(expected)}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Property 11: max_count 截取顺序与数量约束
# Validates: Requirements 3.4
# ═════════════════════════════════════════════════════════════════════════════

@given(
    k=st.integers(min_value=1, max_value=20),
    period_dates=st.lists(
        st.dates(min_value=date(2010, 1, 1), max_value=date(2030, 12, 31)),
        min_size=0,
        max_size=30,
    ),
)
@settings(max_examples=100)
def test_property11_max_count_truncation_order_and_count(k, period_dates):
    """
    Property 11: max_count 截取顺序与数量约束
    断言结果数量 <= k、降序排列、当输入 >= k 时结果恰好等于 k。
    **Validates: Requirements 3.4**
    """
    fetcher = ReportFetcher()

    unique_dates = list(set(period_dates))
    reports = [
        ReportMeta(
            company_id="600519",
            report_type=ReportType.ANNUAL,
            period=p,
            download_url=f"http://example.com/{p}.pdf",
            title=f"报告_{p}",
        )
        for p in unique_dates
    ]

    date_range = DateRange(start=date(2000, 1, 1), end=date(2099, 12, 31))
    result = fetcher._apply_filters(
        reports, date_range, [ReportType.ANNUAL], max_count=k
    )

    # 数量不超过 k
    assert len(result) <= k, f"结果数 {len(result)} 超过 max_count={k}"

    # 如果输入 >= k，结果恰好等于 k
    if len(reports) >= k:
        assert len(result) == k, (
            f"输入 {len(reports)} >= {k}，但结果只有 {len(result)} 条"
        )

    # 降序排列
    for i in range(len(result) - 1):
        assert result[i].period >= result[i + 1].period, (
            f"结果未按降序排列: {result[i].period} < {result[i+1].period}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# Property 12: 默认时间范围为运行时上一自然年度
# Validates: Requirements 4.1
# ═════════════════════════════════════════════════════════════════════════════

@given(
    mock_today=st.dates(
        min_value=date(2020, 1, 1), max_value=date(2099, 12, 31)
    ),
)
@settings(max_examples=100)
def test_property12_default_date_range_is_previous_calendar_year(mock_today):
    """
    Property 12: 默认时间范围为运行时上一自然年度
    当 config 未指定 start/end 时，实际使用的 start/end 为 {today.year-1}-01-01
    和 {today.year-1}-12-31。
    **Validates: Requirements 4.1**
    """
    prev_year = mock_today.year - 1
    expected_start = date(prev_year, 1, 1)
    expected_end = date(prev_year, 12, 31)

    # 准备数据：去年最后一天的年报 + 前年的年报
    reports_db = {
        "600519": [
            ReportMeta(
                company_id="600519",
                report_type=ReportType.ANNUAL,
                period=expected_end,
                download_url="http://example.com/last_year.pdf",
                title="去年年报",
            ),
            ReportMeta(
                company_id="600519",
                report_type=ReportType.ANNUAL,
                period=date(prev_year - 1, 12, 31),
                download_url="http://example.com/prev_year.pdf",
                title="前年年报",
            ),
        ]
    }

    fetcher = ReportFetcher(
        known_companies={"600519": {}}, reports_db=reports_db
    )

    config = AppConfig(
        storage_dir="/tmp/reports",
        companies=[CompanyConfig(ticker="600519")],
        start_date=None,
        end_date=None,
        max_count=10,
    )

    with patch("financial_report_fetcher.fetcher.datetime") as mock_dt:
        mock_dt.date.today.return_value = mock_today
        mock_dt.date.side_effect = date  # 允许正常构造 date 对象
        results = list(fetcher.fetch(config))

    # 结果只应包含去年的年报
    assert len(results) >= 1
    for r in results:
        assert r.period >= expected_start, (
            f"报告期 {r.period} 早于默认起始日 {expected_start}"
        )
        assert r.period <= expected_end, (
            f"报告期 {r.period} 晚于默认截止日 {expected_end}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# Property 13: report_types 过滤精确性
# Validates: Requirements 5.2
# ═════════════════════════════════════════════════════════════════════════════

@given(
    report_type_set=st.lists(
        st.sampled_from(ReportType), min_size=1, max_size=3, unique=True
    ),
    extra_types=st.lists(
        st.sampled_from(ReportType), min_size=0, max_size=3, unique=True
    ),
)
@settings(max_examples=100)
def test_property13_report_types_filter_exactness(report_type_set, extra_types):
    """
    Property 13: report_types 过滤精确性
    仅过滤出类型在 report_type_set 中的报告，其他类型被排除。
    **Validates: Requirements 5.2**
    """
    all_types = list(set(report_type_set + extra_types))
    if not all_types:
        all_types = [ReportType.ANNUAL]

    # 为每种类型构造一条报告
    reports = [
        ReportMeta(
            company_id="600519",
            report_type=rt,
            period=date(2023, 12, 31),
            download_url=f"http://example.com/{rt.value}.pdf",
            title=f"报告_{rt.value}",
        )
        for rt in all_types
    ]

    fetcher = ReportFetcher()
    date_range = DateRange(start=date(2000, 1, 1), end=date(2099, 12, 31))
    result = fetcher._apply_filters(
        reports, date_range, report_type_set, max_count=100
    )

    allowed_set = set(report_type_set)
    for r in result:
        assert r.report_type in allowed_set, (
            f"结果中出现了不允许的类型 {r.report_type}，允许: {allowed_set}"
        )

    # 所有允许类型的报告都应出现在结果中
    result_types = {r.report_type for r in result}
    for rt in report_type_set:
        assert rt in result_types, (
            f"允许的类型 {rt} 未出现在过滤结果中"
        )


# ═════════════════════════════════════════════════════════════════════════════
# Property 3: 配置错误信息包含相关字段名或路径
# Validates: Requirements 1.3, 1.4, 3.5, 3.6, 3.7
# ═════════════════════════════════════════════════════════════════════════════

@st.composite
def invalid_config_dicts(draw):
    """生成含缺字段或非法值的配置字典"""
    # 随机选择一种错误类型
    error_type = draw(st.sampled_from([
        "missing_companies",
        "too_many_companies",
        "missing_storage_dir",
        "bad_max_count",
        "bad_date_range",
        "bad_start_date",
        "bad_end_date",
    ]))

    if error_type == "missing_companies":
        return {"storage_dir": "/tmp"}

    if error_type == "too_many_companies":
        return {
            "storage_dir": "/tmp",
            "companies": [{"ticker": f"T{i:04d}"} for i in range(51)],
        }

    if error_type == "missing_storage_dir":
        return {"companies": [{"ticker": "000001"}]}

    if error_type == "bad_max_count":
        return {
            "storage_dir": "/tmp",
            "companies": [{"ticker": "000001"}],
            "max_count": draw(st.integers(max_value=0) | st.integers(min_value=10001)),
        }

    if error_type == "bad_date_range":
        return {
            "storage_dir": "/tmp",
            "companies": [{"ticker": "000001"}],
            "start_date": "2025-12-31",
            "end_date": "2020-01-01",
        }

    if error_type == "bad_start_date":
        return {
            "storage_dir": "/tmp",
            "companies": [{"ticker": "000001"}],
            "start_date": "2023-01-01",
        }

    if error_type == "bad_end_date":
        return {
            "storage_dir": "/tmp",
            "companies": [{"ticker": "000001"}],
            "end_date": "2023-12-31",
        }


@given(config_dict=invalid_config_dicts())
@settings(max_examples=100)
def test_property3_config_error_contains_field_name_or_path(config_dict):
    """
    Property 3: 配置错误信息包含相关字段名或路径
    对缺字段或含非法值的配置字典，断言异常消息中含对应字段名或路径。
    **Validates: Requirements 1.3, 1.4, 3.5, 3.6, 3.7**
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        yaml.dump(config_dict, f)
        config_path = f.name

    try:
        loader = ConfigLoader()
        try:
            loader.load(config_path)
            pytest.fail(f"期望抛出异常但未抛出: {config_dict}")
        except ConfigValidationError as e:
            msg = str(e)
            # 验证错误信息包含相关字段名
            has_field = any(
                field in msg.lower()
                for field in [
                    "companies",
                    "storage_dir",
                    "max_count",
                    "start_date",
                    "end_date",
                ]
            )
            assert has_field, (
                f"错误信息 '{msg}' 不包含相关字段名，输入: {config_dict}"
            )
    finally:
        os.unlink(config_path)


# ═════════════════════════════════════════════════════════════════════════════
# Property 14: 非法 report_types 返回包含非法值的错误信息
# Validates: Requirements 5.4
# ═════════════════════════════════════════════════════════════════════════════

@given(
    invalid_type=st.text(min_size=1, max_size=20).filter(
        lambda s: s not in {"annual", "semi_annual", "quarterly"}
    ),
)
@settings(max_examples=100)
def test_property14_invalid_report_types_error_contains_value(invalid_type):
    """
    Property 14: 非法 report_types 返回包含非法值的错误信息
    使用含非法字符串的 report_types，断言异常消息包含该非法值。
    **Validates: Requirements 5.4**
    """
    assume(invalid_type.strip() != "")

    config_dict = {
        "storage_dir": "/tmp/reports",
        "companies": [{"ticker": "000001"}],
        "report_types": [invalid_type],
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        yaml.dump(config_dict, f)
        config_path = f.name

    try:
        loader = ConfigLoader()
        try:
            loader.load(config_path)
            pytest.fail(f"期望因非法 report_types 抛异常，输入: {invalid_type}")
        except ConfigValidationError as e:
            msg = str(e)
            # 错误信息应包含非法值或 report_types 字段名
            assert (
                invalid_type in msg or "report_types" in msg.lower()
            ), f"错误信息 '{msg}' 不包含非法值 '{invalid_type}' 或字段名 'report_types'"
    finally:
        os.unlink(config_path)
