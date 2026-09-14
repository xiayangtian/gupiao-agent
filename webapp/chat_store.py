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
from typing import Any, Dict, List, Mapping, Optional

from webapp.chat_models import AnswerRun, ChatMessage

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

    @staticmethod
    def _normalize_message(message: Mapping[str, Any] | ChatMessage) -> Dict[str, Any]:
        """Convert v1 role/content messages and v2 messages to the ChatMessage contract."""
        if isinstance(message, ChatMessage):
            return message.to_dict()
        return ChatMessage.from_dict(message).to_dict()

    @classmethod
    def _normalize_session(cls, session: Mapping[str, Any]) -> Dict[str, Any]:
        normalized = dict(session)
        messages = session.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("session messages must be a JSON array")
        normalized["messages"] = [cls._normalize_message(message) for message in messages]
        return normalized

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                return {
                    "schema_version": 2,
                    "sessions": [
                        self._normalize_session(session)
                        for session in data["sessions"]
                        if isinstance(session, Mapping)
                    ],
                }
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return {"schema_version": 2, "sessions": []}

    def _save(self) -> None:
        self._data["schema_version"] = 2
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
            summaries = []
            for session in sessions:
                latest_run = next(
                    (
                        message.get("run")
                        for message in reversed(session.get("messages", []))
                        if message.get("role") == "assistant"
                        and isinstance(message.get("run"), Mapping)
                    ),
                    None,
                )
                scope = latest_run.get("scope") if latest_run else None
                companies = scope.get("companies", []) if isinstance(scope, Mapping) else []
                summaries.append({
                    "id": session["id"],
                    "title": session.get("title") or "新会话",
                    "message_count": len(session.get("messages", [])),
                    "created_at": session.get("created_at", ""),
                    "updated_at": session.get("updated_at", ""),
                    "status": latest_run.get("status") if latest_run else None,
                    "scope_mode": scope.get("mode") if isinstance(scope, Mapping) else None,
                    "company_codes": [
                        company.get("code") for company in companies
                        if isinstance(company, Mapping) and isinstance(company.get("code"), str)
                    ],
                })
            return summaries

    def get_session(self, sid: str) -> Optional[Dict[str, Any]]:
        """返回会话深拷贝（含完整消息）；不存在返回 None"""
        with self._lock:
            for s in self._data["sessions"]:
                if s["id"] == sid:
                    return json.loads(json.dumps(s, ensure_ascii=False))
            return None

    def get_messages(self, sid: str) -> List[Dict[str, str]]:
        """Return model-compatible role/content messages without persisted run metadata."""
        session = self.get_session(sid)
        if not session:
            return []
        messages = []
        for message in session.get("messages", []):
            run = message.get("run")
            if (
                message.get("role") == "assistant"
                and not message.get("content")
                and isinstance(run, Mapping)
                and run.get("status") == "failed"
            ):
                continue
            messages.append({
                "role": message.get("role", ""),
                "content": message.get("content", ""),
            })
        return messages

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

    def _append_normalized_messages(
        self,
        sid: str,
        messages: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        for session in self._data["sessions"]:
            if session["id"] != sid:
                continue
            session.setdefault("messages", []).extend(messages)
            if session.get("title") in (None, "", "新会话"):
                first_user = next(
                    (message.get("content", "") for message in session["messages"]
                     if message.get("role") == "user"),
                    "",
                )
                title = first_user.strip().replace("\n", " ")
                session["title"] = title[:TITLE_MAX_CHARS] or "新会话"
            session["updated_at"] = _now_iso()
            self._save()
            return dict(session)
        return None

    def append_turn(
        self,
        sid: str,
        *,
        question: str,
        run: AnswerRun,
    ) -> Optional[Dict[str, Any]]:
        """Atomically persist a user question and its complete or incomplete answer run."""
        if not isinstance(run, AnswerRun):
            raise ValueError("run must be an AnswerRun")
        messages = [
            ChatMessage(role="user", content=question).to_dict(),
            ChatMessage(role="assistant", content=run.content, run=run).to_dict(),
        ]
        with self._lock:
            return self._append_normalized_messages(sid, messages)

    def append_messages(
        self,
        sid: str,
        messages: List[Dict[str, Any] | ChatMessage],
    ) -> Optional[Dict[str, Any]]:
        """Compatibility entry point that writes every message in schema-v2 form."""
        normalized = [self._normalize_message(message) for message in messages]
        with self._lock:
            return self._append_normalized_messages(sid, normalized)
