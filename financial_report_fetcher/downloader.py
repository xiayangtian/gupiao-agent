"""
financial_report_fetcher.downloader

Downloader 模块：负责根据 ReportMeta 列表，下载对应 PDF 文件到本地目录，
支持跳过已存在文件、失败重试，并汇总下载结果。
"""

import logging
import os
import tempfile
from typing import Iterator

import requests
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

from financial_report_fetcher.models import DownloadStatus, DownloadSummary, ReportMeta, ReportType

# 模块级日志记录器
logger = logging.getLogger(__name__)


def _is_valid_pdf(path: str) -> bool:
    """仅做快速容器校验：文件非空且具有 PDF 头签名。"""
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


@retry(
    stop=stop_after_attempt(4),                              # 1次初始 + 最多3次重试 = 共4次尝试
    wait=wait_fixed(5),                                      # 每次重试间隔5秒
    retry=retry_if_exception_type(requests.RequestException),  # 仅对网络请求异常重试
    reraise=True,                                            # 最终失败时重新抛出异常
)
def _do_download(url: str, timeout: int) -> bytes:
    """
    执行实际的 HTTP 下载请求，失败时由 tenacity 自动重试。

    :param url: 文件下载地址
    :param timeout: 请求超时时间（秒）
    :return: 文件的二进制内容
    :raises requests.RequestException: 网络请求失败时抛出（重试耗尽后）
    """
    response = requests.get(url, timeout=timeout)
    # HTTP 状态码非 2xx 时抛出异常，触发重试
    response.raise_for_status()
    return response.content


class ReportDownloader:
    """财报下载器，负责文件下载管理"""

    TIMEOUT_SECONDS: int = 60       # 单文件下载超时时间（秒）
    MAX_RETRIES: int = 3            # 最大重试次数
    RETRY_WAIT_SECONDS: int = 5     # 重试间隔时间（秒）

    # 财报类型到中文名称的映射
    _REPORT_TYPE_NAMES = {
        ReportType.ANNUAL: "年报",
        ReportType.SEMI_ANNUAL: "半年报",
        ReportType.QUARTERLY: "季报",
    }

    @staticmethod
    def build_filename(report: ReportMeta) -> str:
        """
        根据财报元信息生成本地文件名。

        文件名格式：{company_name}_{company_id}_{report_type}_{period}.pdf
        示例：长江电力_600900_年报_2025.pdf
        若公司名为空（如测试中未提供），回退为 {company_id}_{report_type}_{year}.pdf
        示例：600519_年报_2023.pdf

        :param report: 财报元信息对象
        :return: 生成的文件名字符串
        """
        # 财报类型转换为中文名称
        type_name = ReportDownloader._REPORT_TYPE_NAMES[report.report_type]
        # 报告期只取年份
        year = report.period.year
        prefix = report.company_name if report.company_name else report.company_id
        if report.company_name:
            return f"{prefix}_{report.company_id}_{type_name}_{year}.pdf"
        return f"{prefix}_{type_name}_{year}.pdf"

    def download_one(self, report: ReportMeta, storage_dir: str) -> DownloadStatus:
        """
        下载单个财报文件：
        - 若同名文件存在，返回 SKIPPED
        - 下载成功且文件非空，返回 SUCCESS
        - 下载失败或文件为空，返回 FAILED

        :param report: 财报元信息对象
        :param storage_dir: 本地存储目录路径
        :return: 下载状态枚举值
        """
        # 生成目标文件名及完整路径
        filename = self.build_filename(report)
        file_path = os.path.join(storage_dir, filename)

        # 目录不存在时自动创建（含多级目录）
        os.makedirs(storage_dir, exist_ok=True)

        # 只跳过已存在的有效 PDF；中断下载/错误页会重新拉取。
        if os.path.exists(file_path):
            if _is_valid_pdf(file_path):
                logger.info("文件已存在，跳过：%s", file_path)
                return DownloadStatus.SKIPPED
            logger.warning("检测到损坏或不完整 PDF，将重新下载：%s", file_path)
            try:
                os.remove(file_path)
            except OSError as exc:
                logger.error("无法清理损坏文件：%s | 原因: %s", filename, exc)
                return DownloadStatus.FAILED

        # 执行下载（含超时 + 自动重试）
        try:
            content = _do_download(report.download_url, self.TIMEOUT_SECONDS)
        except Exception as exc:
            # 下载失败：记录含文件名和错误原因的 ERROR 日志，返回 FAILED
            logger.error("文件下载失败：%s | 原因: %s", filename, exc)
            return DownloadStatus.FAILED

        # 下载成功后验证文件内容不为空
        if len(content) == 0:
            # 空文件：不写入磁盘，直接返回 FAILED
            logger.error("文件下载失败：%s | 原因: 下载内容为 0 字节", filename)
            return DownloadStatus.FAILED

        # 先写入同目录临时文件，校验通过后再原子替换。
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{filename}.",
                suffix=".tmp",
                dir=storage_dir,
            )
            with os.fdopen(fd, "wb") as f:
                f.write(content)

            if not _is_valid_pdf(tmp_path):
                logger.error("文件下载失败：%s | 原因: 响应不是有效 PDF", filename)
                return DownloadStatus.FAILED

            os.replace(tmp_path, file_path)
            tmp_path = ""
        except OSError as exc:
            logger.error("文件写入失败：%s | 原因: %s", filename, exc)
            return DownloadStatus.FAILED
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning("临时文件清理失败：%s", tmp_path)

        return DownloadStatus.SUCCESS

    def download_all(
        self, reports: Iterator[ReportMeta], storage_dir: str
    ) -> DownloadSummary:
        """
        下载所有财报，返回 DownloadSummary（成功/跳过/失败数量）。
        单个文件失败不中断整体流程。

        :param reports: 财报元信息迭代器
        :param storage_dir: 本地存储目录路径
        :return: 下载汇总统计对象
        """
        summary = DownloadSummary()

        # 逐个处理每份财报，单个失败不影响后续文件
        for report in reports:
            status = self.download_one(report, storage_dir)

            # 根据返回状态累加对应计数器
            if status == DownloadStatus.SUCCESS:
                summary.success += 1
            elif status == DownloadStatus.SKIPPED:
                summary.skipped += 1
            else:
                summary.failed += 1

        # 输出汇总报告
        logger.info(
            "下载完成：成功 %d 份，跳过 %d 份，失败 %d 份，共 %d 份",
            summary.success,
            summary.skipped,
            summary.failed,
            summary.total,
        )

        return summary
