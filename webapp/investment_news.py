"""Collection coordination and daily news digest generation."""

from __future__ import annotations

import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from webapp.investment_news_store import InvestmentNewsStore, NewsItem


_ACTIVE_STATUSES = {"queued", "running"}
_DIGEST_SECTIONS = ("market_themes", "policy_regulatory", "watchlist", "risks")
_MAX_BATCH_CHARS = 24_000
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _shanghai_day(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_SHANGHAI).date().isoformat()


class InvestmentNewsService:
    """Coordinates source calls and persists truthful operation state."""

    def __init__(
        self,
        store: InvestmentNewsStore,
        source: Any,
        ai_client: Any,
        stock_name_lookup: Callable[[str], str | None],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.store = store
        self.source = source
        self.ai_client = ai_client
        self.stock_name_lookup = stock_name_lookup
        self.clock = clock
        self._lock = threading.RLock()
        self._active_runs: dict[str, str] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._run_events: dict[str, threading.Event] = {}
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="investment-news")
        self._closed = False
        self._recover_interrupted_runs()

    def _recover_interrupted_runs(self) -> None:
        for kind in ("collection", "digest"):
            run = self.store.get_active_run(kind)
            if run:
                self.store.update_run(
                    run["run_id"], status="failed",
                    error="服务重启时任务未完成", finished_at=_iso(self.clock()),
                    result={"recovered_after_restart": True},
                )

    def _reserve_run(self, kind: str, trigger: str) -> tuple[str, bool]:
        with self._lock:
            if self._closed:
                raise RuntimeError("investment news service is shut down")
            active_id = self._active_runs.get(kind)
            if active_id:
                active = self._find_run(active_id)
                if active and active["status"] in _ACTIVE_STATUSES:
                    return active_id, True
                self._active_runs.pop(kind, None)
            persisted = self.store.get_active_run(kind)
            if persisted:
                self._active_runs[kind] = persisted["run_id"]
                return persisted["run_id"], True
            run_id = self.store.create_run(kind, trigger)
            self._active_runs[kind] = run_id
            self._run_events[run_id] = threading.Event()
            return run_id, False

    def _find_run(self, run_id: str) -> dict[str, Any] | None:
        for run in self.store.list_runs(100):
            if run["run_id"] == run_id:
                return run
        return None

    def submit_collection(self, trigger: str) -> dict[str, Any]:
        run_id, reused = self._reserve_run("collection", trigger)
        if reused:
            run = self._find_run(run_id) or {"status": "running"}
            return {"run_id": run_id, "status": run["status"], "kind": "collection", "reused": True}
        try:
            future = self._executor.submit(self._run_collection, run_id, trigger)
        except Exception as exc:
            self.store.update_run(
                run_id, status="failed", error=str(exc)[:300], finished_at=_iso(self.clock()),
            )
            with self._lock:
                self._active_runs.pop("collection", None)
            raise
        with self._lock:
            self._futures[run_id] = future
        return {"run_id": run_id, "status": "queued", "kind": "collection", "reused": False}

    def collect_now(self, trigger: str) -> dict[str, Any]:
        run_id, reused = self._reserve_run("collection", trigger)
        if reused:
            run = self._find_run(run_id) or {"status": "running"}
            return {"run_id": run_id, "status": run["status"], "kind": "collection", "reused": True}
        return self._run_collection(run_id, trigger)

    @staticmethod
    def _source_error(code: str | None, name: str | None, exc: Exception) -> dict[str, Any]:
        return {
            "code": code,
            "name": name,
            "status": "failed",
            "item_count": 0,
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }

    def _run_collection(self, run_id: str, trigger: str) -> dict[str, Any]:
        self.store.update_run(run_id, status="running")
        items: list[NewsItem] = []
        source_results: list[dict[str, Any]] = []
        successful_sources = 0
        try:
            try:
                market_items = self.source.fetch_market()
                items.extend(market_items)
                successful_sources += 1
                source_results.append({
                    "source_id": "eastmoney_market",
                    "status": "succeeded",
                    "item_count": len(market_items),
                })
            except Exception as exc:
                source_results.append(self._source_error(None, "东方财富市场快讯", exc))

            for entry in self.store.get_watchlist():
                code, name = entry["code"], entry["name"]
                try:
                    items_for_stock = self.source.fetch_stock(code, name)
                    items.extend(items_for_stock)
                    successful_sources += 1
                    source_results.append({
                        "code": code,
                        "name": name,
                        "status": "succeeded",
                        "item_count": len(items_for_stock),
                    })
                except Exception as exc:
                    source_results.append(self._source_error(code, name, exc))

            if items:
                self.store.upsert_items(items)
            failed_sources = sum(result["status"] == "failed" for result in source_results)
            if failed_sources and successful_sources:
                status = "partial"
            elif failed_sources:
                status = "failed"
            else:
                status = "succeeded"
            result = {
                "run_id": run_id,
                "kind": "collection",
                "trigger": trigger,
                "status": status,
                "item_count": len(items),
                "source_count": successful_sources,
                "failed_source_count": failed_sources,
                "source_results": source_results,
                "data_through": max((item.collected_at for item in items), default=None),
            }
            error = "；".join(
                f"{entry.get('code') or entry.get('name')}: {entry.get('error')}"
                for entry in source_results if entry["status"] == "failed"
            ) or None
            self.store.update_run(
                run_id,
                status=status,
                result=result,
                error=error,
                finished_at=_iso(self.clock()),
            )
            return result
        except Exception as exc:
            error = str(exc)[:500]
            result = {
                "run_id": run_id,
                "kind": "collection",
                "trigger": trigger,
                "status": "failed",
                "item_count": 0,
                "source_count": successful_sources,
                "source_results": source_results,
            }
            self.store.update_run(
                run_id, status="failed", result=result, error=error,
                finished_at=_iso(self.clock()),
            )
            return result
        finally:
            with self._lock:
                if self._active_runs.get("collection") == run_id:
                    self._active_runs.pop("collection", None)
                self._futures.pop(run_id, None)
                event = self._run_events.pop(run_id, None)
                if event:
                    event.set()

    def submit_digest(self, trigger: str, force: bool = False) -> dict[str, Any]:
        run_id, reused = self._reserve_run("digest", trigger)
        if reused:
            run = self._find_run(run_id) or {"status": "running"}
            return {"run_id": run_id, "status": run["status"], "kind": "digest", "reused": True}
        day = _shanghai_day(self.clock())
        try:
            future = self._executor.submit(self._run_digest, run_id, day, force, trigger)
        except Exception as exc:
            self.store.update_run(
                run_id, status="failed", error=str(exc)[:300], finished_at=_iso(self.clock()),
            )
            with self._lock:
                self._active_runs.pop("digest", None)
            raise
        with self._lock:
            self._futures[run_id] = future
        return {"run_id": run_id, "status": "queued", "kind": "digest", "reused": False}

    def generate_digest(self, day: date | str, force: bool = False) -> dict[str, Any]:
        day_value = date.fromisoformat(day) if isinstance(day, str) else day
        trigger = "manual" if force else "scheduled"
        run_id, reused = self._reserve_run("digest", trigger)
        if reused:
            run = self._find_run(run_id) or {"status": "running"}
            return {"run_id": run_id, "status": run["status"], "kind": "digest", "reused": True}
        return self._run_digest(run_id, day_value.isoformat(), force, trigger)

    @staticmethod
    def _digest_batches(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_chars = 0
        for item in items:
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            item_chars = len(encoded)
            if current and current_chars + item_chars > _MAX_BATCH_CHARS:
                batches.append(current)
                current = []
                current_chars = 0
            current.append(item)
            current_chars += item_chars
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _decode_ai_json(response: Any) -> dict[str, Any]:
        content = response.get("content") if isinstance(response, dict) else None
        if isinstance(content, dict):
            payload = content
        elif isinstance(content, str):
            payload = json.loads(content)
        else:
            raise ValueError("AI digest response did not include JSON content")
        if not isinstance(payload, dict):
            raise ValueError("AI digest response must be a JSON object")
        return payload

    @staticmethod
    def _validated_sections(payload: dict[str, Any], allowed_ids: set[str]) -> tuple[dict[str, list[dict[str, Any]]], bool]:
        sections: dict[str, list[dict[str, Any]]] = {name: [] for name in _DIGEST_SECTIONS}
        had_claim = False
        for section in _DIGEST_SECTIONS:
            raw_points = payload.get(section, [])
            if not isinstance(raw_points, list):
                raise ValueError(f"AI digest section {section} must be a list")
            for point in raw_points:
                if not isinstance(point, dict):
                    continue
                text = point.get("text")
                ids = point.get("item_ids")
                if not isinstance(text, str) or not text.strip():
                    continue
                had_claim = True
                if not isinstance(ids, list):
                    continue
                valid_ids = sorted({item_id for item_id in ids if isinstance(item_id, str) and item_id in allowed_ids})
                if valid_ids:
                    sections[section].append({"text": text.strip(), "item_ids": valid_ids})
        return sections, had_claim

    def _call_digest_model(self, content: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], str]:
        messages = [{"role": "user", "content": json.dumps(content, ensure_ascii=False)}]
        system = (
            "你是财经资讯归纳助手。只根据输入条目归纳，不引入外部事实；每个要点都必须附上"
            "输入中存在的 item_ids。watchlist 要点只引用 kind=watchlist 的条目并标明关联股票；"
            "不得把转载或同题报道声称为独立佐证。不得推断新闻导致股价涨跌，不给买卖建议；不确定处明确说明。"
            "只返回 JSON 对象，包含 market_themes、policy_regulatory、watchlist、risks 四个数组，"
            "数组元素格式为 {text, item_ids}。每个数组最多 5 条，每条 text 不超过 100 字。"
        )
        for attempt in range(2):
            response = self.ai_client.chat(
                messages=messages,
                system=system,
                temperature=0.2,
                max_tokens=8192,
                response_format={"type": "json_object"},
            )
            finish_reason = response.get("finish_reason") if isinstance(response, dict) else None
            try:
                if finish_reason == "length":
                    raise ValueError("AI digest response was truncated at the output token limit")
                payload = self._decode_ai_json(response)
                break
            except (json.JSONDecodeError, ValueError):
                if attempt:
                    raise
                messages.append({
                    "role": "assistant",
                    "content": response.get("content", "") if isinstance(response, dict) else "",
                })
                messages.append({
                    "role": "user",
                    "content": "上次输出不完整或不是有效 JSON。请缩短内容，只输出完整 JSON 对象，四个数组各最多 3 条。",
                })
        allowed = set(content.get("allowed_item_ids", []))
        sections, had_claim = self._validated_sections(payload, allowed)
        if had_claim and not any(sections.values()):
            raise ValueError("AI digest produced claims without valid source item citations")
        model = str(response.get("model") or "unknown") if isinstance(response, dict) else "unknown"
        return sections, model

    def _run_digest(self, run_id: str, day: str, force: bool, trigger: str) -> dict[str, Any]:
        self.store.update_run(run_id, status="running")
        try:
            items = self.store.get_day_items(day)
            input_hash = self.store.daily_input_hash(day)
            previous = self.store.get_digest(day)
            if not items:
                result = {
                    "run_id": run_id, "kind": "digest", "status": "skipped",
                    "day": day, "reason": "no_stored_items", "input_count": 0,
                }
                self.store.update_run(run_id, status="skipped", result=result, finished_at=_iso(self.clock()))
                return result
            if not force and previous and previous["input_hash"] == input_hash:
                result = {
                    "run_id": run_id, "kind": "digest", "status": "skipped",
                    "day": day, "reason": "input_unchanged", "input_count": len(items),
                }
                self.store.update_run(run_id, status="skipped", result=result, finished_at=_iso(self.clock()))
                return result

            model_items = []
            all_ids = set()
            for item in items:
                item_id = str(item["id"])
                all_ids.add(item_id)
                model_items.append({
                    "id": item_id,
                    "title": item["title"],
                    "snippet": item["snippet"][:1200],
                    "published_at": item["published_at"],
                    "source_name": item["source_name"],
                    "source_id": item["source_id"],
                    "url": item["url"],
                    "sources": item["sources"],
                    "kinds": item["kinds"],
                    "symbols": item["symbols"],
                    "importance_tags": item["importance_tags"],
                })

            batch_summaries = []
            model_name = "unknown"
            for batch in self._digest_batches(model_items):
                batch_ids = [item["id"] for item in batch]
                sections, model_name = self._call_digest_model({
                    "stage": "summarize_batch",
                    "day": day,
                    "items": batch,
                    "allowed_item_ids": batch_ids,
                })
                batch_summaries.append({
                    "item_ids": batch_ids,
                    "sections": sections,
                })

            final_sections, model_name = self._call_digest_model({
                "stage": "combine_daily_summary",
                "day": day,
                "batch_summaries": batch_summaries,
                "allowed_item_ids": sorted(all_ids),
            })
            items_by_id = {str(item["id"]): item for item in items}
            for section in final_sections.values():
                for point in section:
                    citations = []
                    for item_id in point["item_ids"]:
                        cited_item = items_by_id.get(item_id)
                        if not cited_item:
                            continue
                        sources = cited_item.get("sources") or [{
                            "source_name": cited_item["source_name"], "url": cited_item["url"],
                        }]
                        for source in sources:
                            citations.append({
                                "item_id": item_id,
                                "title": cited_item["title"],
                                "source_name": source.get("source_name") or cited_item["source_name"],
                                "url": source.get("url") or cited_item["url"],
                            })
                    point["citation_count"] = len(citations)
                    point["citations"] = citations[:5]
            source_pairs = {
                (source.get("source_id"), source.get("url"))
                for item in items for source in item.get("sources", [])
            }
            data_through = max((item["collected_at"] for item in items), default=None)
            payload = {
                **final_sections,
                "input_count": len(items),
                "source_count": len(source_pairs),
                "data_through": data_through,
            }
            generated_at = _iso(self.clock())
            self.store.save_digest(
                day, payload, input_hash, model_name,
                input_count=len(items), source_count=len(source_pairs),
                data_through=data_through, generated_at=generated_at,
            )
            result = {
                "run_id": run_id, "kind": "digest", "status": "succeeded", "day": day,
                "input_count": len(items), "source_count": len(source_pairs),
                "input_hash": input_hash, "model": model_name, "generated_at": generated_at,
            }
            self.store.update_run(run_id, status="succeeded", result=result, finished_at=generated_at)
            return {**result, **payload}
        except Exception as exc:
            error = str(exc)[:500]
            result = {"run_id": run_id, "kind": "digest", "status": "failed", "day": day}
            self.store.update_run(
                run_id, status="failed", result=result, error=error,
                finished_at=_iso(self.clock()),
            )
            return result
        finally:
            with self._lock:
                if self._active_runs.get("digest") == run_id:
                    self._active_runs.pop("digest", None)
                self._futures.pop(run_id, None)
                event = self._run_events.pop(run_id, None)
                if event:
                    event.set()

    def wait_for_run(self, run_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        run = self._find_run(run_id)
        if run is None or run["status"] not in _ACTIVE_STATUSES:
            return run
        event = self._run_events.get(run_id)
        if event is not None:
            event.wait(timeout)
        return self._find_run(run_id)

    def status(self) -> dict[str, Any]:
        return {
            "active_collection": self.store.get_active_run("collection"),
            "active_digest": self.store.get_active_run("digest"),
            "recent_runs": self.store.list_runs(10),
            "watchlist": self.store.get_watchlist(),
        }

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
