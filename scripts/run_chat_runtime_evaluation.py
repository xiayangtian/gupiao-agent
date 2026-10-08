#!/usr/bin/env python3
"""Explicit offline entrypoint for the chat runtime evaluation baseline.

This command is separate from ``scripts/run_chat_evaluation.py`` (which replays fixed
``AnswerRun`` products). It drives the real chat orchestration with frozen providers and
never reads production credentials or reaches real providers.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

HARNESS_VERSION = "chat-runtime-offline-v1"
_SANITIZED_KEYS = ("suite_id", "corpus_version", "code_revision", "harness_version", "generated_at", "totals", "cases")


def _code_revision() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
                              capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except Exception:
        return "unknown"


def _write_atomic(path: Path, payload: dict) -> None:
    """Write the report through a same-directory temp file and atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False)
    try:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the offline chat runtime evaluation against frozen providers.",
    )
    parser.add_argument("--cases", required=True, help="Path to a versioned runtime case manifest.")
    parser.add_argument("--output", required=True, help="Destination JSON report path (isolated, never the quality sidecar).")
    parser.add_argument("--limit", type=int, default=0, help="Optional cap on the number of cases to run (0 = all).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from webapp.chat_runtime_eval_cases import RuntimeSuite
    from webapp.chat_runtime_eval_runner import OfflineChatHarness

    cases_path = Path(args.cases).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    if output_path.name == "research_quality_summary.json":
        print("refusing to overwrite the read-only quality sidecar", file=sys.stderr)
        return 2
    if not cases_path.is_file():
        print(f"case manifest not found: {cases_path}", file=sys.stderr)
        return 2

    suite = RuntimeSuite.load(cases_path)
    selected = suite.cases[:args.limit] if args.limit and args.limit > 0 else suite.cases
    # The app is imported only when a real run starts, so --help never loads the server.
    from webapp import server

    started = time.perf_counter()
    results = []
    for case in selected:
        workspace = Path(tempfile.mkdtemp(prefix=f"chat-eval-{case.id}-"))
        harness = OfflineChatHarness(case, workspace=workspace)
        try:
            observation = harness.run(server.app)
        except AssertionError as exc:
            results.append({"id": case.id, "category": case.category, "status": "harness_error",
                            "error": str(exc)[:200], "calls": dict(harness.fixture.calls)})
            continue
        results.append({
            "id": case.id, "category": case.category, "status": observation.persisted_status,
            "expected_status": case.expected_status,
            "status_matches_expectation": observation.persisted_status == case.expected_status,
            "event_count": len(observation.events),
            "calls": dict(observation.call_attempts),
            "total_seconds": round(observation.total_seconds or 0.0, 4),
            "answer_present": bool(observation.answer),
            "usage_reported": observation.usage is not None,
        })

    matched = sum(1 for item in results if item.get("status_matches_expectation"))
    payload = {
        "suite_id": suite.suite_id,
        "corpus_version": suite.corpus_version,
        "code_revision": _code_revision(),
        "harness_version": HARNESS_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "totals": {
            "cases": len(results),
            "statuses_matching_expectation": matched,
            "harness_errors": sum(1 for item in results if item["status"] == "harness_error"),
            "elapsed_seconds": round(time.perf_counter() - started, 4),
        },
        "cases": results,
    }
    _write_atomic(output_path, payload)
    print(json.dumps(payload["totals"], ensure_ascii=False))
    return 0 if payload["totals"]["harness_errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
