"""有界验证 agent-browser CLI 及其底层浏览器是否可用于浏览器回归。"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Iterable
from pathlib import Path


def _successful_json(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 0 or not result.stdout.strip():
        return False
    try:
        return bool(json.loads(result.stdout).get("success", False))
    except json.JSONDecodeError:
        return False


def _probe_browser(candidate: str) -> bool:
    """确认 wrapper 能启动真实浏览器，而不只确认其文件存在。"""
    try:
        version = subprocess.run(
            [candidate, "--version"], capture_output=True, text=True, timeout=5
        )
        if version.returncode != 0:
            return False
        session = f"financial-visuals-probe-{uuid.uuid4().hex}"
        opened = subprocess.run(
            [candidate, "--session", session, "--json", "open", "about:blank"],
            capture_output=True, text=True, timeout=8,
        )
        return _successful_json(opened)
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        if "session" in locals():
            try:
                subprocess.run(
                    [candidate, "--session", session, "--json", "close"],
                    capture_output=True, text=True, timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass


def find_usable_agent_browser(candidates: Iterable[str | None]) -> str | None:
    """返回首个可执行且可实际启动浏览器的 agent-browser 路径。"""
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        if _probe_browser(str(path)):
            return str(path)
    return None
