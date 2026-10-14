"""SQLite persistence for locally collected investment news."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SCHEMA_VERSION = 1
_TRACKING_PARAMS = {
    "from", "spm", "src", "source", "share_source", "share_token", "clickid",
    "_t", "timestamp", "activity_id",
}
_WATCHLIST_LIMIT = 20


class WatchlistLimitError(ValueError):
    """Raised when adding a 21st distinct security to the shared watchlist."""


@dataclass(frozen=True)
class NewsItem:
    id: str
    title: str
    url: str
    snippet: str
    source_id: str
    source_name: str
    published_at: str | None
    collected_at: str
    kind: str
    symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("id", "title", "url", "source_id", "source_name", "collected_at"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} must not be empty")
        if self.kind not in {"market", "watchlist"}:
            raise ValueError("kind must be 'market' or 'watchlist'")
        if any(not re.fullmatch(r"\d{6}", symbol) for symbol in self.symbols):
            raise ValueError("symbols must contain six-digit A-share codes")


class InvestmentNewsStore:
    """Thread-safe SQLite repository. Each operation owns a short-lived connection."""

    def __init__(self, db_path: str) -> None:
        if db_path == ":memory:":
            raise ValueError("InvestmentNewsStore requires a file-backed SQLite path")
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._write_lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._write_lock, self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise RuntimeError(f"news database schema {version} is newer than supported {_SCHEMA_VERSION}")
            if version == 0:
                db.executescript(
                    """
                    CREATE TABLE news_items (
                        id TEXT PRIMARY KEY,
                        url_key TEXT NOT NULL UNIQUE,
                        url TEXT NOT NULL,
                        title TEXT NOT NULL,
                        snippet TEXT NOT NULL DEFAULT '',
                        source_id TEXT NOT NULL,
                        source_name TEXT NOT NULL,
                        published_at TEXT,
                        published_day TEXT,
                        collected_at TEXT NOT NULL,
                        collected_day TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        story_group_id TEXT NOT NULL
                    );
                    CREATE INDEX news_items_published_day ON news_items(published_day);
                    CREATE INDEX news_items_collected_day ON news_items(collected_day);
                    CREATE INDEX news_items_story_group ON news_items(story_group_id);

                    CREATE TABLE news_item_sources (
                        item_id TEXT NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
                        source_id TEXT NOT NULL,
                        source_name TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        published_at TEXT,
                        snippet TEXT NOT NULL DEFAULT '',
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        PRIMARY KEY(item_id, source_id, source_url)
                    );
                    CREATE INDEX news_sources_item_id ON news_item_sources(item_id);

                    CREATE TABLE news_item_scopes (
                        item_id TEXT NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL CHECK(kind IN ('market', 'watchlist')),
                        symbol TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY(item_id, kind, symbol)
                    );
                    CREATE INDEX news_scopes_filter ON news_item_scopes(kind, symbol, item_id);

                    CREATE TABLE watchlist (
                        code TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE news_runs (
                        run_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL CHECK(kind IN ('collection', 'digest')),
                        trigger TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        error TEXT
                    );
                    CREATE INDEX news_runs_kind_started ON news_runs(kind, started_at DESC);

                    CREATE TABLE daily_digests (
                        day TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        input_hash TEXT NOT NULL,
                        input_count INTEGER NOT NULL,
                        source_count INTEGER NOT NULL,
                        data_through TEXT,
                        model TEXT NOT NULL,
                        generated_at TEXT NOT NULL
                    );

                    CREATE TABLE schedule_slots (
                        slot_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @staticmethod
    def _canonical_url(url: str) -> tuple[str, str]:
        parsed = urlsplit(str(url).strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("news URL must be an absolute HTTP(S) URL")
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        if not hostname:
            raise ValueError("news URL must include a hostname")
        port = parsed.port
        netloc = hostname if port is None or (parsed.scheme.lower(), port) in {("http", 80), ("https", 443)} else f"{hostname}:{port}"
        path = parsed.path or "/"
        if path != "/":
            path = path.rstrip("/")
        query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            normalized_key = key.lower()
            if normalized_key.startswith("utm_") or normalized_key in _TRACKING_PARAMS:
                continue
            query.append((key, value))
        query.sort()
        canonical = urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(query), ""))
        return canonical, canonical

    @staticmethod
    def _as_utc(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _business_day(cls, value: str | None) -> str | None:
        parsed = cls._as_utc(value)
        if parsed is None:
            return None
        from zoneinfo import ZoneInfo

        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()

    @staticmethod
    def _normalize_title(title: str) -> str:
        normalized = unicodedata.normalize("NFKC", title).casefold()
        return "".join(
            char for char in normalized
            if not char.isspace() and not unicodedata.category(char).startswith("P")
        )

    @classmethod
    def _story_group_id(cls, item: NewsItem, published_day: str | None, collected_day: str) -> str:
        day = published_day or collected_day
        key = f"{day}\0{cls._normalize_title(item.title)}".encode("utf-8")
        return hashlib.sha256(key).hexdigest()[:24]

    def upsert_items(self, items: Iterable[NewsItem]) -> int:
        now_count = 0
        with self._write_lock, self._connect() as db:
            for item in items:
                canonical_url, url_key = self._canonical_url(item.url)
                collected = self._as_utc(item.collected_at)
                if collected is None:
                    raise ValueError("collected_at must be an ISO-8601 timestamp")
                collected_at = collected.isoformat()
                collected_day = self._business_day(collected_at)
                published = self._as_utc(item.published_at)
                published_at = published.isoformat() if published else None
                published_day = self._business_day(published_at)
                group_id = self._story_group_id(item, published_day, collected_day or "")
                existing = db.execute("SELECT id FROM news_items WHERE url_key = ?", (url_key,)).fetchone()
                item_id = str(existing["id"]) if existing else item.id
                db.execute(
                    """
                    INSERT INTO news_items (
                        id, url_key, url, title, snippet, source_id, source_name,
                        published_at, published_day, collected_at, collected_day,
                        first_seen_at, last_seen_at, story_group_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url_key) DO UPDATE SET
                        title = excluded.title,
                        snippet = excluded.snippet,
                        published_at = COALESCE(excluded.published_at, news_items.published_at),
                        published_day = COALESCE(excluded.published_day, news_items.published_day),
                        last_seen_at = excluded.last_seen_at,
                        story_group_id = excluded.story_group_id
                    """,
                    (
                        item_id, url_key, item.url, item.title.strip(), item.snippet[:1200],
                        item.source_id, item.source_name, published_at, published_day,
                        collected_at, collected_day or "", collected_at, collected_at, group_id,
                    ),
                )
                db.execute(
                    """
                    INSERT INTO news_item_sources (
                        item_id, source_id, source_name, source_url, published_at,
                        snippet, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_id, source_id, source_url) DO UPDATE SET
                        source_name = excluded.source_name,
                        published_at = COALESCE(excluded.published_at, news_item_sources.published_at),
                        snippet = excluded.snippet,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (item_id, item.source_id, item.source_name, item.url, published_at,
                     item.snippet[:1200], collected_at, collected_at),
                )
                scope_symbols = item.symbols if item.kind == "watchlist" and item.symbols else ("",)
                for symbol in scope_symbols:
                    db.execute(
                        "INSERT OR IGNORE INTO news_item_scopes(item_id, kind, symbol) VALUES (?, ?, ?)",
                        (item_id, item.kind, symbol),
                    )
                now_count += 1
        return now_count

    def _item_rows(self, day: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT n.*,
                       (SELECT COUNT(*) FROM news_items related WHERE related.story_group_id = n.story_group_id) AS related_count,
                       (SELECT json_group_array(json_object(
                           'source_id', s.source_id, 'source_name', s.source_name,
                           'url', s.source_url, 'published_at', s.published_at
                       )) FROM news_item_sources s WHERE s.item_id = n.id) AS sources_json,
                       (SELECT json_group_array(symbol) FROM news_item_scopes x
                         WHERE x.item_id = n.id AND x.symbol != '') AS symbols_json,
                       (SELECT json_group_array(DISTINCT kind) FROM news_item_scopes x
                         WHERE x.item_id = n.id) AS kinds_json
                FROM news_items n
                WHERE n.published_day = ? OR (n.published_day IS NULL AND n.collected_day = ?)
                ORDER BY COALESCE(n.published_at, n.last_seen_at) DESC, n.id
                """,
                (day, day),
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry["sources"] = sorted(
                json.loads(entry.pop("sources_json") or "[]"),
                key=lambda source: (source["source_id"], source["url"]),
            )
            entry["symbols"] = sorted(set(json.loads(entry.pop("symbols_json") or "[]")))
            entry["kinds"] = json.loads(entry.pop("kinds_json") or "[]")
            entry["importance_tags"] = ["自选股关联"] if "watchlist" in entry["kinds"] else []
            result.append(entry)
        return result

    def get_day_items(self, day: str) -> list[dict[str, Any]]:
        """Return every stored item assigned to the Shanghai business day, without paging."""
        return self._item_rows(day)

    def list_items(
        self, day: str, kind: str | None = None, symbol: str | None = None,
        sort: str = "latest", limit: int = 20, offset: int = 0,
    ) -> dict[str, Any]:
        if sort not in {"latest", "importance"}:
            raise ValueError("sort must be 'latest' or 'importance'")
        if not 1 <= int(limit) <= 20:
            raise ValueError("limit must be between 1 and 20")
        if int(offset) < 0:
            raise ValueError("offset must not be negative")
        if kind not in {None, "all", "market", "watchlist"}:
            raise ValueError("kind must be market, watchlist, all, or None")
        if symbol is not None and not re.fullmatch(r"\d{6}", symbol):
            raise ValueError("symbol must be a six-digit A-share code")

        rows = self._item_rows(day)
        filtered = []
        for row in rows:
            if kind not in {None, "all"} and kind not in row["kinds"]:
                continue
            if symbol and symbol not in row["symbols"]:
                continue
            filtered.append(row)
        known = [row for row in filtered if row["published_at"] is not None]
        unknown = [row for row in filtered if row["published_at"] is None]
        known.sort(key=lambda row: row["published_at"], reverse=True)
        unknown.sort(key=lambda row: row["last_seen_at"], reverse=True)
        if sort == "importance":
            filtered = sorted(known + unknown, key=lambda row: bool(row["importance_tags"]), reverse=True)
        else:
            filtered = known + unknown
        total = len(filtered)
        return {"items": filtered[int(offset):int(offset) + int(limit)], "total": total, "day": day}

    def get_watchlist(self) -> list[dict[str, str]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT code, name, created_at, updated_at FROM watchlist ORDER BY created_at, code"
            ).fetchall()]

    def add_watchlist(self, code: str, name: str) -> dict[str, str]:
        code = str(code).strip()
        name = str(name).strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("code must be a six-digit A-share code")
        if not name:
            raise ValueError("name must not be empty")
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._connect() as db:
            exists = db.execute("SELECT 1 FROM watchlist WHERE code = ?", (code,)).fetchone()
            if exists:
                db.execute("UPDATE watchlist SET name = ?, updated_at = ? WHERE code = ?", (name, now, code))
            else:
                count = int(db.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0])
                if count >= _WATCHLIST_LIMIT:
                    raise WatchlistLimitError(f"watchlist is limited to {_WATCHLIST_LIMIT} securities")
                db.execute(
                    "INSERT INTO watchlist(code, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (code, name, now, now),
                )
        return next(item for item in self.get_watchlist() if item["code"] == code)

    def remove_watchlist(self, code: str) -> bool:
        with self._write_lock, self._connect() as db:
            cursor = db.execute("DELETE FROM watchlist WHERE code = ?", (str(code),))
            return cursor.rowcount > 0

    def create_run(self, kind: str, trigger: str) -> str:
        import uuid

        if kind not in {"collection", "digest"}:
            raise ValueError("kind must be collection or digest")
        run_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._connect() as db:
            db.execute(
                "INSERT INTO news_runs(run_id, kind, trigger, status, started_at) VALUES (?, ?, ?, 'queued', ?)",
                (run_id, kind, trigger, now),
            )
        return run_id

    def update_run(
        self, run_id: str, *, status: str, result: dict[str, Any] | None = None,
        error: str | None = None, finished_at: str | None = None,
    ) -> None:
        allowed = {"queued", "running", "succeeded", "partial", "failed", "skipped"}
        if status not in allowed:
            raise ValueError(f"unsupported run status: {status}")
        with self._write_lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE news_runs SET status = ?, result_json = ?, error = ?, finished_at = ? WHERE run_id = ?",
                (status, json.dumps(result or {}, ensure_ascii=False), error, finished_at, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown news run: {run_id}")

    def get_active_run(self, kind: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM news_runs WHERE kind = ? AND status IN ('queued', 'running') ORDER BY started_at DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM news_runs ORDER BY started_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [self._run_row(row) for row in rows]

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json") or "{}")
        return result

    def daily_input_hash(self, day: str) -> str:
        rows = self.get_day_items(day)
        digest_input = [
            (
                row["id"], row["title"], row["snippet"], row["published_at"], row["sources"],
                row["kinds"], row["symbols"], row["importance_tags"],
            )
            for row in rows
        ]
        encoded = json.dumps(digest_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def save_digest(
        self, day: str, payload: dict[str, Any], input_hash: str, model: str,
        *, input_count: int | None = None, source_count: int | None = None,
        data_through: str | None = None, generated_at: str | None = None,
    ) -> None:
        generated_at = generated_at or datetime.now(timezone.utc).isoformat()
        if input_count is None:
            input_count = int(payload.get("input_count", 0))
        if source_count is None:
            source_count = int(payload.get("source_count", 0))
        with self._write_lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO daily_digests(day, payload_json, input_hash, input_count, source_count, data_through, model, generated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    payload_json = excluded.payload_json, input_hash = excluded.input_hash,
                    input_count = excluded.input_count, source_count = excluded.source_count,
                    data_through = excluded.data_through, model = excluded.model,
                    generated_at = excluded.generated_at
                """,
                (day, json.dumps(payload, ensure_ascii=False), input_hash, input_count,
                 source_count, data_through, model, generated_at),
            )

    def get_digest(self, day: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM daily_digests WHERE day = ?", (day,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def has_schedule_slot(self, slot_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM schedule_slots WHERE slot_id = ?", (slot_id,)).fetchone() is not None

    def mark_schedule_slot(self, slot_id: str, created_at: str | None = None) -> bool:
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO schedule_slots(slot_id, created_at) VALUES (?, ?)",
                (slot_id, created_at),
            )
            return cursor.rowcount == 1

    def cleanup_expired(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff_time = now.astimezone(timezone.utc) - timedelta(days=90)
        cutoff = cutoff_time.isoformat()
        cutoff_day = cutoff_time.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with self._write_lock, self._connect() as db:
            cursor = db.execute("DELETE FROM news_items WHERE last_seen_at < ?", (cutoff,))
            deleted_items = cursor.rowcount
            db.execute("DELETE FROM daily_digests WHERE day < ?", (cutoff_day,))
            return deleted_items
