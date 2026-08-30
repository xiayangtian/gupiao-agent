"""chunking — PDF 全文提取、章节感知分块、分析报告/指标切分。

年报内容来源分两类：
- source=pdf：PDF 逐页全量提取，按「第X节」章节边界聚合分块；
- source=analysis：reports/analysis/*.json，按分析维度切分，
  并把结构化 metrics 自然语言化为「指标摘要」chunk。
"""

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from financial_report_fetcher.report_identity import parse_report_filename

# A 股年报标准章节（按出现顺序）
SECTION_RE = re.compile(r"第[一二三四五六七八九十百]+节\s*[^\n]{2,30}")

# metrics 字段 → 中文标签；revenue/net_profit 单位为亿元，其余为百分比
METRIC_LABELS = {
    "revenue": ("营业收入", "亿元"),
    "net_profit": ("归母净利润", "亿元"),
    "roe": ("加权平均净资产收益率(ROE)", "%"),
    "gross_margin": ("毛利率", "%"),
    "debt_ratio": ("资产负债率", "%"),
}

@dataclass
class Chunk:
    report_id: str
    source: str          # "pdf" | "analysis"
    text: str
    section: str
    page: Optional[int]
    chunk_index: int
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """幂等派生 id：内容变化则 id 变化，重复摄取走 upsert 天然去重"""
        raw = f"{self.report_id}|{self.source}|{self.section}|{self.chunk_index}|{self.text}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ── PDF ─────────────────────────────────────────────────────────

def extract_pdf_pages(pdf_path: str) -> List[Tuple[int, str]]:
    """pypdf 逐页全量提取，返回 [(页码从1起, 页文本), ...]"""
    from pypdf import PdfReader

    reader = PdfReader(pdf_path)
    return [(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]


def parse_pdf_report_id(pdf_path: str) -> Optional[str]:
    """委托统一身份模块从 PDF 文件名解析 report_id。"""
    return parse_report_filename(pdf_path)


def _normalize_section_title(title: str) -> str:
    """规范化章节标题：剥离尾部点号/空白/页码数字，使目录页与正文标题一致"""
    return re.sub(r"[.。·…\s\d]+$", "", title.strip())


def _split_by_sections(pages: List[Tuple[int, str]]) -> List[Dict[str, Any]]:
    """按「第X节」标题聚合页文本，目录页与正文同标题时以正文为准。

    流程：
    1. 收集每页所有标题位置（规范化后）：[(page_no, pos, title)]；
    2. 同标题保留最后一次出现（正文页覆盖目录页），得到有效标题位置集合；
    3. 逐页构建 sections：页内按标题位置切分——标题前文本归当前 section，
       标题及其后文本开新 section（同页多标题依次切分）；页内无标题时整页归当前
       section；首个标题之前（封面/目录前置）的文本丢弃。

    Returns: [{"section": str, "start_page": int, "texts": [str, ...]}]
    """
    # 1) 收集每页所有标题（目录页标题规范化后与正文标题一致）
    title_positions: List[Tuple[int, int, str]] = []
    for page_no, text in pages:
        for m in SECTION_RE.finditer(text):
            title_positions.append((page_no, m.start(), _normalize_section_title(m.group(0).strip())))

    if not title_positions:
        # 无章节标题：整份报告作为单一 section
        return [{
            "section": "全文",
            "start_page": pages[0][0] if pages else 1,
            "texts": [t for _, t in pages],
        }]

    # 2) 同标题保留最后一次出现（正文页覆盖目录页）
    last_by_title: Dict[str, Tuple[int, int]] = {}
    for page_no, pos, title in title_positions:
        last_by_title[title] = (page_no, pos)
    effective_positions = set(last_by_title.values())

    # 3) 逐页构建 sections
    sections: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    started = False
    for page_no, text in pages:
        # 本页有效标题（按页内位置排序）
        page_titles = sorted(
            (pos, title) for (pno, pos, title) in title_positions
            if pno == page_no and (page_no, pos) in effective_positions
        )
        if not page_titles:
            # 页内无标题：整页归当前 section（首个有效标题之前丢弃）
            if started and text.strip():
                current["texts"].append(text)
            continue
        # 页内第一个标题之前的文本归当前 section（首个标题之前丢弃）
        if started:
            head = text[:page_titles[0][0]].strip()
            if head:
                current["texts"].append(head)
        started = True
        # 页内按标题位置依次切分：标题及其后文本开新 section
        for i, (pos, title) in enumerate(page_titles):
            end = page_titles[i + 1][0] if i + 1 < len(page_titles) else None
            seg = text[pos:end].strip() if end is not None else text[pos:].strip()
            current = {"section": title, "start_page": page_no, "texts": []}
            sections.append(current)
            if seg:
                current["texts"].append(seg)
    return sections


def _chunk_text(text: str, chunk_size: int, overlap: int) -> List[str]:
    """按段落聚合到 chunk_size，相邻 chunk 重叠 overlap 字符"""
    if overlap >= chunk_size:
        overlap = max(chunk_size - 1, 0)
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chunks: List[str] = []
    buf = ""
    for para in paragraphs:
        while len(para) > chunk_size:
            piece = para[:chunk_size]
            chunks.append(piece)
            para = para[chunk_size - overlap:]
        if len(buf) + len(para) + 1 > chunk_size and buf:
            chunks.append(buf)
            buf = buf[-overlap:] if overlap else ""
        buf = (buf + "\n" + para).strip() if buf else para
    if buf.strip():
        chunks.append(buf)
    return chunks or ([text] if text.strip() else [])


def chunk_pdf(
    pdf_path: str,
    report_id: str,
    chunk_size: int = 800,
    overlap: int = 100,
) -> List[Chunk]:
    """PDF 逐页提取 + 章节感知分块"""
    pages = extract_pdf_pages(pdf_path)
    if not pages:
        return []
    chunks: List[Chunk] = []
    for sec in _split_by_sections(pages):
        text = "\n".join(sec["texts"]).strip()
        if not text:
            continue
        for i, seg in enumerate(_chunk_text(text, chunk_size, overlap)):
            chunks.append(Chunk(
                report_id=report_id,
                source="pdf",
                text=seg,
                section=sec["section"],
                page=sec["start_page"],
                chunk_index=i,
                meta={"ticker": report_id.split(":")[0]},
            ))
    return chunks


# ── 分析报告（analysis）─────────────────────────────────────────

def chunk_metrics(metrics: Optional[List[Dict[str, Any]]], report_id: str) -> Optional[Chunk]:
    """metrics 自然语言化为「指标摘要」chunk；空则返回 None"""
    if not metrics:
        return None
    lines = []
    for m in metrics:
        if not isinstance(m, dict) or not m.get("year"):
            continue
        try:
            year = int(m["year"])
        except (TypeError, ValueError):
            continue  # 非数字年份跳过该行
        parts = [f"{year}年"]
        for key, (label, unit) in METRIC_LABELS.items():
            val = m.get(key)
            if val is None:
                continue
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(num):
                continue
            # 整数去掉 .0（如 862 → "862"），小数保留
            num_str = str(int(num)) if num.is_integer() else str(num)
            parts.append(f"{label} {num_str}{unit}")
        if len(parts) > 1:
            lines.append("、".join(parts))
    if not lines:
        return None
    text = "财务指标摘要：" + "；".join(lines) + "。"
    return Chunk(
        report_id=report_id,
        source="analysis",
        text=text,
        section="指标摘要",
        page=None,
        chunk_index=0,
        # 与维度 chunk 保持一致，保证按 ticker 过滤的通用问答能检索到指标摘要
        meta={"ticker": report_id.split(":")[0]},
    )


def chunk_analysis_json(
    json_path: str,
    report_id: str,
    chunk_size: int = 800,
    overlap: int = 100,
) -> List[Chunk]:
    """分析报告按维度切分；超长维度按小节再切；附指标摘要 chunk"""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    chunks: List[Chunk] = []
    for dim in data.get("dimensions", []) or []:
        if dim.get("error") or not dim.get("content"):
            continue
        name = dim.get("name") or dim.get("id") or "分析"
        for i, seg in enumerate(_chunk_text(dim["content"], chunk_size, overlap)):
            chunks.append(Chunk(
                report_id=report_id,
                source="analysis",
                text=seg,
                section=name,
                page=None,
                chunk_index=i,
                meta={"ticker": report_id.split(":")[0]},
            ))
    m_chunk = chunk_metrics(data.get("metrics"), report_id)
    if m_chunk is not None:
        chunks.append(m_chunk)
    return chunks
