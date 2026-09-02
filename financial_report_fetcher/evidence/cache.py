"""版本化、原子写入的财报证据缓存。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

from .models import EvidenceRecord


CACHE_SCHEMA_VERSION = 1


def make_cache_key(
    report_id: str,
    pdf_hash: str,
    provider_versions: Mapping[str, str],
    parser_versions: Mapping[str, str],
) -> str:
    payload = json.dumps(
        {
            "report_id": report_id,
            "pdf_hash": pdf_hash,
            "provider_versions": dict(sorted(provider_versions.items())),
            "parser_versions": dict(sorted(parser_versions.items())),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_key(key: str) -> None:
    if len(key) != 64 or any(character not in "0123456789abcdef" for character in key.lower()):
        raise ValueError("证据缓存 key 必须是 64 位 SHA-256 十六进制字符串")


class EvidenceCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        _validate_key(key)
        return self.root / f"{key}.json"

    def save(self, key: str, records: Sequence[EvidenceRecord]) -> Path:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "records": [record.to_dict() for record in records],
        }
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.stem}-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            os.replace(temporary_path, path)
            return path
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def load(self, key: str) -> list[EvidenceRecord] | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            with path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict) or payload.get("schema_version") != CACHE_SCHEMA_VERSION:
                raise ValueError("不支持的证据缓存版本")
            raw_records = payload.get("records")
            if not isinstance(raw_records, list):
                raise ValueError("证据缓存 records 必须是列表")
            return [EvidenceRecord.from_dict(item) for item in raw_records]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            self._quarantine(path)
            return None

    @staticmethod
    def _quarantine(path: Path) -> Path:
        candidate = path.with_name(f"{path.name}.corrupt")
        index = 1
        while candidate.exists():
            candidate = path.with_name(f"{path.name}.corrupt.{index}")
            index += 1
        os.replace(path, candidate)
        return candidate
