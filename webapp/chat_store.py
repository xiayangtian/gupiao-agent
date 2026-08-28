"""webapp.chat_store — 智能问答历史会话的 JSON 文件持久化。

会话以 {id, title, messages, created_at, updated_at} 结构落盘到
data/chat_sessions.json（已加入 .gitignore）。提供新建/列表/读取/追加接口，
供 /api/chat/stream 与 /api/chat/sessions 端点使用；线程安全、写盘原子。
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = "data/chat_sessions.json"

# 会话标题取首条用户消息的最大字符数
TITLE_MAX_CHARS = 24


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ChatStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or DEFAULT_PATH
        self._lock = threading.RLock()
        self._data = self._load()

    # ── 持久化 ──────────────────────────────────────────────

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"sessions": []}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ── 查询 ────────────────────────────────────────────────

    def list_sessions(self) -> List[Dict[str, Any]]:
        """按更新时间降序返回会话摘要（不含完整消息）"""
        with self._lock:
            sessions = sorted(
                self._data["sessions"],
                key=lambda s: s.get("updated_at", ""),
                reverse=True,
            )
            return [{
                "id": s["id"],
                "title": s.get("title") or "新会话",
                "message_count": len(s.get("messages", [])),
                "created_at": s.get("created_at", ""),
                "updated_at": s.get("updated_at", ""),
            } for s in sessions]

    def get_session(self, sid: str) -> Optional[Dict[str, Any]]:
        """返回会话深拷贝（含完整消息）；不存在返回 None"""
        with self._lock:
            for s in self._data["sessions"]:
                if s["id"] == sid:
                    return json.loads(json.dumps(s, ensure_ascii=False))
            return None

    def get_messages(self, sid: str) -> List[Dict[str, str]]:
        s = self.get_session(sid)
        return list(s.get("messages", [])) if s else []

    # ── 写入 ────────────────────────────────────────────────

    def create_session(self) -> Dict[str, Any]:
        with self._lock:
            now = _now_iso()
            session: Dict[str, Any] = {
                "id": uuid.uuid4().hex[:12],
                "title": "新会话",
                "messages": [],
                "created_at": now,
                "updated_at": now,
            }
            self._data["sessions"].append(session)
            self._save()
            return dict(session)

    def get_or_create_empty(self) -> Dict[str, Any]:
        """已有「未对话过的新会话」（空会话）则复用，否则新建。

        供「新会话」入口使用：保证历史列表最多保留一个待用的空会话，
        避免反复创建导致空会话堆积。
        """
        with self._lock:
            for s in self._data["sessions"]:
                if not s.get("messages"):
                    return dict(s)
            return self.create_session()

    def get_or_create(self, sid: Optional[str]) -> Dict[str, Any]:
        """按 id 取会话；不存在或未传 id 时创建独立新会话"""
        if sid:
            existing = self.get_session(sid)
            if existing:
                return existing
        return self.create_session()

    def rename_session(self, sid: str, title: str) -> Optional[Dict[str, Any]]:
        """重命名会话标题；不存在返回 None"""
        title = (title or "").strip()
        if not title:
            return None
        with self._lock:
            for s in self._data["sessions"]:
                if s["id"] != sid:
                    continue
                s["title"] = title[:TITLE_MAX_CHARS]
                s["updated_at"] = _now_iso()
                self._save()
                return dict(s)
            return None

    def delete_session(self, sid: str) -> bool:
        """删除会话；不存在返回 False"""
        with self._lock:
            before = len(self._data["sessions"])
            self._data["sessions"] = [
                s for s in self._data["sessions"] if s["id"] != sid
            ]
            if len(self._data["sessions"]) == before:
                return False
            self._save()
            return True

    def append_messages(
        self,
        sid: str,
        messages: List[Dict[str, str]],
    ) -> Optional[Dict[str, Any]]:
        """向会话追加消息；标题缺省时取首条用户消息截断；返回更新后会话"""
        with self._lock:
            for s in self._data["sessions"]:
                if s["id"] != sid:
                    continue
                s.setdefault("messages", []).extend(messages)
                if s.get("title") in (None, "", "新会话"):
                    first_user = next(
                        (m.get("content", "") for m in s["messages"]
                         if m.get("role") == "user"),
                        "",
                    )
                    title = first_user.strip().replace("\n", " ")
                    s["title"] = title[:TITLE_MAX_CHARS] or "新会话"
                s["updated_at"] = _now_iso()
                self._save()
                return dict(s)
            return None
