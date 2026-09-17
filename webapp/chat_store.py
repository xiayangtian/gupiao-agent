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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional

from webapp.chat_models import AnswerRun, ChatMessage
from webapp.research_models import ResearchRun

logger = logging.getLogger(__name__)

DEFAULT_PATH = "data/chat_sessions.json"

# 会话标题取首条用户消息的最大字符数
TITLE_MAX_CHARS = 24


@dataclass(frozen=True)
class ChatSessionRun:
    """Read-only metadata needed by the derived research-workspace index."""

    session_id: str
    title: str
    updated_at: str
    run: AnswerRun
    research_run: ResearchRun | None = None


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ChatStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or DEFAULT_PATH
        self._lock = threading.RLock()
        self._session_delete_hooks: List[Callable[[str], None]] = []
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
                supplements = data.get("supplements")
                if not isinstance(supplements, Mapping):
                    # 旧版或损坏的 supplements 字段（如数组）一律忽略，不影响会话读取。
                    supplements = {}
                return {
                    "schema_version": 2,
                    "sessions": [
                        self._normalize_session(session)
                        for session in data["sessions"]
                        if isinstance(session, Mapping)
                    ],
                    "supplements": {
                        str(key): dict(value)
                        for key, value in supplements.items()
                        if isinstance(value, Mapping)
                    },
                    "research_runs": {
                        str(key): dict(value)
                        for key, value in (data.get("research_runs") or {}).items()
                        if isinstance(value, Mapping) and isinstance(value.get("session_id"), str)
                        and isinstance(value.get("run"), Mapping)
                    } if isinstance(data.get("research_runs", {}), Mapping) else {},
                }
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return {"schema_version": 2, "sessions": [], "supplements": {}, "research_runs": {}}

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

    def iter_session_runs(self) -> List[ChatSessionRun]:
        """Return immutable AnswerRun/ResearchRun metadata without message content.

        The workspace is a derived view, so callers receive parsed immutable run
        contracts rather than mutable session dictionaries or raw chat messages.
        Invalid historic records are ignored just as unavailable evidence is.
        """
        with self._lock:
            records: List[ChatSessionRun] = []
            for session in self._data["sessions"]:
                session_id = session.get("id")
                if not isinstance(session_id, str):
                    continue
                title = session.get("title") if isinstance(session.get("title"), str) else "新会话"
                updated_at = session.get("updated_at") if isinstance(session.get("updated_at"), str) else ""
                for message in session.get("messages", []):
                    if message.get("role") != "assistant" or not isinstance(message.get("run"), Mapping):
                        continue
                    try:
                        run = AnswerRun.from_dict(message["run"])
                    except ValueError:
                        continue
                    research_run = None
                    if run.research_run_id:
                        saved = self._data.get("research_runs", {}).get(run.research_run_id)
                        if isinstance(saved, Mapping) and saved.get("session_id") == session_id:
                            try:
                                research_run = ResearchRun.from_dict(saved.get("run", {}))
                            except ValueError:
                                pass
                    records.append(ChatSessionRun(
                        session_id=session_id,
                        title=title,
                        updated_at=updated_at,
                        run=run,
                        research_run=research_run,
                    ))
            return records

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

    # ── 补充授权请求 ────────────────────────────────────────

    def save_supplement(self, request: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """持久化补充授权请求；会话不存在返回 None，已绑定的会话不可更换。"""
        if not isinstance(request, Mapping):
            raise ValueError("supplement request must be a JSON object")
        record = json.loads(json.dumps(dict(request), ensure_ascii=False))
        supplement_id = record.get("id")
        session_id = record.get("session_id")
        if not isinstance(supplement_id, str) or not supplement_id.strip():
            raise ValueError("supplement request must include a non-empty id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("supplement request must include a non-empty session_id")
        with self._lock:
            if not any(session["id"] == session_id for session in self._data["sessions"]):
                return None
            existing = self._data["supplements"].get(supplement_id)
            if existing is not None and existing.get("session_id") != session_id:
                raise ValueError("supplement session binding must not change")
            self._data["supplements"][supplement_id] = record
            self._save()
            return json.loads(json.dumps(record, ensure_ascii=False))

    def get_supplement(self, supplement_id: str) -> Optional[Dict[str, Any]]:
        """返回补充授权请求深拷贝；不存在返回 None"""
        with self._lock:
            record = self._data["supplements"].get(supplement_id)
            if record is None:
                return None
            return json.loads(json.dumps(record, ensure_ascii=False))

    def save_research_run(self, sid: str, run: ResearchRun) -> Optional[Dict[str, Any]]:
        """Persist a complete M3 run under its owner; no cross-session lookup exists."""
        if not isinstance(run, ResearchRun):
            raise ValueError("run must be a ResearchRun")
        with self._lock:
            if not any(session["id"] == sid for session in self._data["sessions"]):
                return None
            record = {"session_id": sid, "run": run.to_dict()}
            self._data.setdefault("research_runs", {})[run.id] = record
            self._save()
            return json.loads(json.dumps(record, ensure_ascii=False))

    def get_research_run(self, sid: str, run_id: str) -> Optional[ResearchRun]:
        """Return only a run owned by sid; unknown or foreign records are invisible."""
        with self._lock:
            record = self._data.get("research_runs", {}).get(run_id)
            if not isinstance(record, Mapping) or record.get("session_id") != sid:
                return None
            try:
                return ResearchRun.from_dict(record.get("run", {}))
            except ValueError:
                return None

    def add_session_delete_hook(self, hook: Callable[[str], None]) -> None:
        """Register a best-effort callback after a persisted session deletion."""
        if not callable(hook):
            raise ValueError("session delete hook must be callable")
        with self._lock:
            self._session_delete_hooks.append(hook)

    def delete_session(self, sid: str) -> bool:
        """删除会话；不存在返回 False，并在持久化后通知关联的派生索引。"""
        with self._lock:
            before = len(self._data["sessions"])
            self._data["sessions"] = [
                s for s in self._data["sessions"] if s["id"] != sid
            ]
            # 补充授权请求属于会话，随会话一并删除，避免跨会话残留。
            self._data["supplements"] = {
                key: value for key, value in self._data["supplements"].items()
                if value.get("session_id") != sid
            }
            self._data["research_runs"] = {
                key: value for key, value in self._data.get("research_runs", {}).items()
                if value.get("session_id") != sid
            }
            if len(self._data["sessions"]) == before:
                return False
            self._save()
            hooks = tuple(self._session_delete_hooks)
        for hook in hooks:
            try:
                hook(sid)
            except Exception:  # pragma: no cover - hooks must not undo deletion
                logger.exception("session delete hook failed")
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
