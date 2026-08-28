"""IngestionService — 扫描本地资产并摄取进向量库。

支持两类来源：
- reports/*.pdf → 章节感知分块（source=pdf）
- reports/analysis/*.json → 按维度 + 指标摘要切分（source=analysis）

幂等：manifest 记录每份报告的 pdf/analysis hash，
内容未变化时跳过，实现增量摄取。
"""

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .chunking import chunk_analysis_json, chunk_pdf, parse_pdf_report_id
from .store import RagStore

logger = logging.getLogger(__name__)

PDF_SUFFIX = ".pdf"
ANALYSIS_SUFFIX = "_分析报告.json"


def _sha1_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class IngestResult:
    ingested: int = 0          # 本次实际摄取（含更新）的报告数
    skipped: int = 0           # 内容未变化跳过的报告数
    total_chunks: int = 0      # 库中总 chunk 数
    errors: List[str] = field(default_factory=list)


class IngestionService:
    def _locked(method: Callable) -> Callable:
        """串行化 manifest/摄取写路径：多任务并发分析时避免 manifest 竞态"""
        def wrapper(self, *args, **kwargs):
            with self._lock:
                return method(self, *args, **kwargs)
        return wrapper

    def __init__(
        self,
        store: RagStore,
        reports_dir: str = "reports",
        analysis_dir: Optional[str] = None,
        chunk_size: int = 800,
        chunk_overlap: int = 100,
        manifest_path: Optional[str] = None,
        auto_ingest: bool = False,
    ) -> None:
        self.store = store
        self.reports_dir = reports_dir
        self.analysis_dir = analysis_dir or os.path.join(reports_dir, "analysis")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        # manifest_path 由调用方显式传入（server/CLI 传 os.path.join(store_path, "manifest.json")）；
        # 缺省回退 data/rag/manifest.json
        base = os.path.dirname(manifest_path) if manifest_path else "data/rag"
        self.manifest_path = manifest_path or os.path.join(base, "manifest.json")
        self.auto_ingest = auto_ingest
        # 可重入锁：auto_ingest_report → ingest_pdf → ingest_file 嵌套调用安全
        self._lock = threading.RLock()
        self._manifest = self._load_manifest()

    # ── manifest ──────────────────────────────────────────────

    def _load_manifest(self) -> Dict[str, Any]:
        try:
            with open(self.manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_manifest(self) -> None:
        os.makedirs(os.path.dirname(self.manifest_path), exist_ok=True)
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(self._manifest, f, ensure_ascii=False, indent=2)

    # ── 摄取 ──────────────────────────────────────────────────

    @_locked
    def ingest_all(self, force: bool = False) -> IngestResult:
        result = IngestResult()
        if not os.path.isdir(self.reports_dir):
            logger.warning("报告目录不存在：%s", self.reports_dir)
            return result

        pdfs = sorted(
            f for f in os.listdir(self.reports_dir)
            if f.endswith(PDF_SUFFIX)
        )
        for name in pdfs:
            pdf_path = os.path.join(self.reports_dir, name)
            rid = parse_pdf_report_id(pdf_path)
            if rid is None:
                result.errors.append(f"无法解析报告身份：{name}")
                continue
            try:
                if self.ingest_one(pdf_path, rid, force=force):
                    result.ingested += 1
                else:
                    result.skipped += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("摄取失败：%s", pdf_path)
                result.errors.append(f"{name}: {exc}")

        self._save_manifest()
        result.total_chunks = self.store.count_chunks()
        return result

    @_locked
    def ingest_one(self, pdf_path: str, report_id: str, force: bool = False) -> bool:
        """单份报告摄取：PDF 原文 + 关联分析报告（按 report_id 匹配文件名）。

        返回 True 表示本次实际摄取（含更新），False 表示内容未变化而跳过。
        """
        pdf_hash = _sha1_file(pdf_path)
        code = report_id.split(":")[0]
        year = report_id.split(":")[1][:4]
        analysis_path = os.path.join(self.analysis_dir, f"{self._company_label(pdf_path)}_{code}_{year}{ANALYSIS_SUFFIX}")
        analysis_hash = None
        if os.path.exists(analysis_path):
            analysis_hash = _sha1_file(analysis_path)

        prev = self._manifest.get(report_id) or {}
        if not force and prev.get("pdf_hash") == pdf_hash and prev.get("analysis_hash") == analysis_hash:
            return False  # 内容未变化，跳过

        # 删除旧 chunk 后全量重建（保证删除的章节不残留）
        self.store.delete_report(report_id)
        chunks = chunk_pdf(pdf_path, report_id, self.chunk_size, self.chunk_overlap)
        if analysis_hash is not None:
            chunks += chunk_analysis_json(analysis_path, report_id, self.chunk_size, self.chunk_overlap)
        self.store.upsert(chunks)

        pdf_chunks = sum(1 for c in chunks if c.source == "pdf")
        analysis_chunks = sum(1 for c in chunks if c.source == "analysis")
        self._manifest[report_id] = {
            "pdf_hash": pdf_hash,
            "analysis_hash": analysis_hash,
            "pdf_chunks": pdf_chunks,
            "analysis_chunks": analysis_chunks,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        logger.info("摄取完成：%s（%d chunks）", report_id, len(chunks))
        return True

    @_locked
    def ingest_pdf(self, pdf_path: str, force: bool = False) -> None:
        """单份 PDF 摄取入口：解析 report_id 后自动连带对应分析报告。

        供 CLI / API 单文件加入复用。
        """
        rid = parse_pdf_report_id(pdf_path)
        if rid is None:
            raise ValueError(f"无法解析报告身份：{pdf_path}")
        self.ingest_one(pdf_path, rid, force=force)
        # 单文件入口需立即落盘，否则进程重启后 hash 记录丢失、增量跳过失效
        self._save_manifest()

    @staticmethod
    def _company_label(pdf_path: str) -> str:
        return os.path.basename(pdf_path).split("_")[0]

    @_locked
    def ingest_file(self, report_id: str, source: str, file_path: Optional[str] = None) -> None:
        """文件级摄取：仅处理单个来源（pdf 或 analysis），不连带另一来源"""
        if source == "pdf":
            path = file_path or self._find_pdf(report_id)
            if path is None:
                raise FileNotFoundError(f"找不到 PDF：{report_id}")
            chunks = chunk_pdf(path, report_id, self.chunk_size, self.chunk_overlap)
            fhash = _sha1_file(path)
        elif source == "analysis":
            path = file_path or self._find_analysis(report_id)
            if path is None:
                raise FileNotFoundError(f"找不到分析报告：{report_id}")
            chunks = chunk_analysis_json(path, report_id, self.chunk_size, self.chunk_overlap)
            fhash = _sha1_file(path)
        else:
            raise ValueError(f"未知来源：{source}")

        self.store.delete_file(report_id, source)          # 重建前清旧
        self.store.upsert(chunks)

        info = self._manifest.setdefault(report_id, {
            "pdf_hash": None, "analysis_hash": None,
            "pdf_chunks": 0, "analysis_chunks": 0,
            "updated_at": "",
        })
        # Task 4 旧 manifest 只有 chunks 键，setdefault 不会补齐既有 dict，需显式迁移
        info.setdefault("pdf_hash", None)
        info.setdefault("analysis_hash", None)
        info.setdefault("pdf_chunks", 0)
        info.setdefault("analysis_chunks", 0)
        info.pop("chunks", None)
        if source == "pdf":
            info["pdf_hash"], info["pdf_chunks"] = fhash, len(chunks)
        else:
            info["analysis_hash"], info["analysis_chunks"] = fhash, len(chunks)
        info["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_manifest()

    @_locked
    def delete_file_index(self, report_id: str, source: str) -> None:
        """删除单文件索引，恢复未添加状态"""
        self.store.delete_file(report_id, source)
        info = self._manifest.get(report_id)
        if info:
            if source == "pdf":
                info["pdf_hash"], info["pdf_chunks"] = None, 0
            else:
                info["analysis_hash"], info["analysis_chunks"] = None, 0
            self._save_manifest()

    def _find_pdf(self, report_id: str) -> Optional[str]:
        if not os.path.isdir(self.reports_dir):
            return None
        for name in os.listdir(self.reports_dir):
            p = os.path.join(self.reports_dir, name)
            if name.endswith(PDF_SUFFIX) and parse_pdf_report_id(p) == report_id:
                return p
        return None

    def _find_analysis(self, report_id: str) -> Optional[str]:
        if not os.path.isdir(self.analysis_dir):
            return None
        for name in os.listdir(self.analysis_dir):
            if not name.endswith(ANALYSIS_SUFFIX):
                continue
            p = os.path.join(self.analysis_dir, name)
            if self._analysis_report_id(p) == report_id:
                return p
        return None

    def _analysis_report_id(self, json_path: str) -> Optional[str]:
        """从分析报告 json 的 meta.source_file 解析 report_id；失败按文件名回退"""
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
            src = data.get("meta", {}).get("source_file")
            if src:
                rid = parse_pdf_report_id(src)
                if rid:
                    return rid
        except (OSError, json.JSONDecodeError):
            pass
        import re as _re
        m = _re.search(r"_(\d{6})_(\d{4})_分析报告\.json$", os.path.basename(json_path))
        if m:
            code, year = m.group(1), m.group(2)
            return f"{code}:{year}-12-31:annual"
        return None

    @_locked
    def list_files(self) -> List[Dict[str, Any]]:
        """本地文件清单（PDF + 分析报告），与 manifest 对比标注 added"""
        out: List[Dict[str, Any]] = []
        if os.path.isdir(self.reports_dir):
            for name in sorted(os.listdir(self.reports_dir)):
                if not name.endswith(PDF_SUFFIX):
                    continue
                p = os.path.join(self.reports_dir, name)
                rid = parse_pdf_report_id(p)
                if rid is None:
                    continue
                info = self._manifest.get(rid) or {}
                out.append(self._entry(rid, "pdf", name, info, is_pdf=True))
        if os.path.isdir(self.analysis_dir):
            for name in sorted(os.listdir(self.analysis_dir)):
                if not name.endswith(ANALYSIS_SUFFIX):
                    continue
                p = os.path.join(self.analysis_dir, name)
                rid = self._analysis_report_id(p)
                if rid is None:
                    continue
                info = self._manifest.get(rid) or {}
                out.append(self._entry(rid, "analysis", name, info, is_pdf=False))
        return out

    @staticmethod
    def _entry(report_id: str, source: str, filename: str,
               info: Dict[str, Any], is_pdf: bool) -> Dict[str, Any]:
        code, period, rtype = report_id.split(":")
        year = period[:4]
        added = bool(info.get("pdf_hash" if is_pdf else "analysis_hash"))
        chunks = int(info.get("pdf_chunks" if is_pdf else "analysis_chunks") or 0)
        type_label = {"annual": "年报", "semi_annual": "半年报", "quarterly": "季报"}.get(rtype, rtype)
        return {
            "report_id": report_id,
            "source": source,
            "type": rtype,
            "type_label": type_label,
            "company": filename.split("_")[0],
            "code": code,
            "year": int(year),
            "filename": filename,
            "added": added,
            "chunk_count": chunks,
        }

    @_locked
    def auto_ingest_pdf(self, pdf_path: str) -> None:
        """自动摄取钩子（下载场景）：仅摄 PDF，文件已加入则跳过（幂等）"""
        if not self.auto_ingest:
            return
        rid = parse_pdf_report_id(pdf_path)
        if rid is None:
            return
        info = self._manifest.get(rid) or {}
        try:
            if info.get("pdf_hash") == _sha1_file(pdf_path):
                return
            self.ingest_file(rid, "pdf", file_path=pdf_path)
        except Exception:
            logger.exception("自动摄取失败：%s", pdf_path)

    @_locked
    def auto_ingest_report(self, pdf_path: str) -> None:
        """自动摄取钩子（分析场景）：连带分析报告双源入库；内容未变化则跳过"""
        if not self.auto_ingest:
            return
        rid = parse_pdf_report_id(pdf_path)
        if rid is None:
            return
        info = self._manifest.get(rid) or {}
        try:
            pdf_hash = _sha1_file(pdf_path)
            analysis_path = self._find_analysis(rid)
            analysis_hash = _sha1_file(analysis_path) if analysis_path else None
            if info.get("pdf_hash") == pdf_hash and info.get("analysis_hash") == analysis_hash:
                return
            # 走公共入口 ingest_pdf：内部解析 report_id 并连带分析报告，且会落盘 manifest
            self.ingest_pdf(pdf_path, force=False)
        except Exception:
            logger.exception("自动摄取失败：%s", pdf_path)

    # ── 状态 ──────────────────────────────────────────────────

    @_locked
    def status(self) -> Dict[str, Any]:
        reports = {}
        for rid, info in self._manifest.items():
            # R1：manifest 已升级为分 source 统计，这里合并为总 chunks，保持返回结构兼容
            pdf_chunks = int(info.get("pdf_chunks", 0) or 0)
            analysis_chunks = int(info.get("analysis_chunks", 0) or 0)
            reports[rid] = {
                "chunks": pdf_chunks + analysis_chunks,
                "pdf_chunks": pdf_chunks,
                "analysis_chunks": analysis_chunks,
                "updated_at": info.get("updated_at", ""),
            }
        return {
            "store_path": os.path.dirname(self.manifest_path) or "data/rag",
            "reports": reports,
            "total_chunks": self.store.count_chunks(),
        }
