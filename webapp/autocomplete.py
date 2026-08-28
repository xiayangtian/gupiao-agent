"""webapp.autocomplete — 股票补全索引

启动时后台线程调用 CNINFODatasource.build_known_companies() 构建
(code, name) 列表（实测约 1 秒）；搜索按三级优先级匹配：
1. 代码前缀（q 为数字）
2. 名称前缀
3. 名称包含
返回 Top N（默认 10）。
"""

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class StockIndex:
    """股票补全索引。

    构建失败不会置位就绪事件（_entries 保持 None）：start() 幂等守卫
    不会短路，调用方可稍后再次 start() 触发重建重试；期间 wait_ready()
    返回 False 表示「此次尚未就绪」，search() 返回空。
    """

    def __init__(self, datasource: Any) -> None:
        self._datasource = datasource          # 提供 build_known_companies()
        self._entries: Optional[List[Tuple[str, str]]] = None  # [(code, name)]
        self._ready = threading.Event()

    # ── 生命周期 ─────────────────────────────────────────────

    def start(self) -> None:
        """后台线程异步构建索引（幂等）"""
        if self._ready.is_set() or self._entries is not None:
            return
        threading.Thread(target=self._build, daemon=True, name="stock-index").start()

    def _build(self) -> None:
        try:
            known = self._datasource.build_known_companies()
        except Exception as exc:
            logger.error("股票索引构建失败：%s", exc)
            # 不 set 就绪事件（保持未 set 状态即可）：失败后再次 start()
            # 会重新 spawn 构建线程重试；期间 wait_ready() 返回 False、
            # search() 返回空。避免 clear() 是为了防止与并发成功的重试
            # 线程 set() 竞态（clear 可能抹掉后置位的就绪事件）。
            return
        entries = []
        for key, info in known.items():
            if not key.isdigit():
                continue
            name = info.get("name")
            if name:
                entries.append((key, name))
        entries.sort(key=lambda e: e[0])
        self._entries = entries
        self._ready.set()
        logger.info("股票索引构建完成：%d 家", len(entries))

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """等待索引构建完成（Web 启动期间用）。True=就绪，False=超时/失败"""
        return self._ready.wait(timeout) and self._entries is not None

    @property
    def is_ready(self) -> bool:
        return self._entries is not None

    # ── 查询 ─────────────────────────────────────────────────

    def search(self, q: str, limit: int = 10) -> List[Dict[str, str]]:
        q = q.strip()
        if not q or self._entries is None:
            return []

        code_matches, prefix_matches, contains_matches = [], [], []
        for code, name in self._entries:
            if code.startswith(q):
                code_matches.append((code, name))
            elif name.startswith(q):
                prefix_matches.append((code, name))
            elif q in name:
                contains_matches.append((code, name))

        ordered = code_matches + prefix_matches + contains_matches
        # 去重（理论上不重复，防御性保序去重）
        seen, results = set(), []
        for code, name in ordered:
            if code in seen:
                continue
            seen.add(code)
            results.append({"code": code, "name": name})
            if len(results) >= limit:
                break
        return results

    def company_name(self, code: str) -> Optional[str]:
        """按代码查公司名（无则 None）"""
        if self._entries is None:
            return None
        for c, name in self._entries:
            if c == code:
                return name
        return None

    def is_valid_code(self, code: str) -> bool:
        if not code.isdigit() or self._entries is None:
            return False
        return any(c == code for c, _ in self._entries)