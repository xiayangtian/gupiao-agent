#!/usr/bin/env python3
"""Explicit offline entrypoint for the chat runtime evaluation baseline.

This command is separate from ``scripts/run_chat_evaluation.py`` (which replays fixed
``AnswerRun`` products). It drives the real chat orchestration with frozen providers and
never reads production credentials or reaches real providers.
"""
from __future__ import annotations

import argparse
import hashlib
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

_SANITIZED_KEYS = ("suite_id", "corpus_version", "code_revision", "harness_version", "generated_at", "totals", "cases")


def _code_revision() -> str:
    try:
        revision = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
                                  capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--", ".", ":(exclude)docs/**", ":(exclude)README.md"],
            cwd=PROJECT_ROOT, capture_output=True, timeout=10, check=True,
        ).stdout
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"],
                                   cwd=PROJECT_ROOT, capture_output=True, timeout=5, check=True).stdout
        untracked_paths = [path for path in sorted(filter(None, untracked.split(b"\0")))
                           if not path.startswith(b"docs/") and path != b"README.md"]
        if not diff and not untracked_paths:
            return revision
        digest = hashlib.sha256()
        digest.update(diff)
        for raw_path in untracked_paths:
            path = PROJECT_ROOT / raw_path.decode("utf-8", errors="surrogateescape")
            if path.is_file():
                digest.update(raw_path + b"\0")
                digest.update(path.read_bytes())
        return f"{revision}+dirty:{digest.hexdigest()[:12]}"
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
    parser.add_argument("--context-window-strategy", choices=("legacy", "relevance"), default="relevance",
                        help="Evidence passage projection policy for a declared baseline/candidate run.")
    parser.add_argument("--context-budget", choices=("disabled", "enabled"), default="enabled",
                        help="Apply the bounded history/evidence projection policy or run the unbounded comparator.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from webapp.chat_runtime_eval_cases import RuntimeSuite
    from webapp.chat_runtime_eval_report import build_report, score, write_report
    from webapp.chat_runtime_eval_runner import FROZEN_NOW, HARNESS_VERSION, OfflineChatHarness

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
    scores = []
    results = []
    for case in selected:
        workspace = Path(tempfile.mkdtemp(prefix=f"chat-eval-{case.id}-"))
        harness = OfflineChatHarness(case, workspace=workspace,
                                     context_window_strategy=args.context_window_strategy,
                                     context_budget_enabled=args.context_budget == "enabled")
        try:
            observation = harness.run(server.app)
        except AssertionError as exc:
            results.append({"id": case.id, "category": case.category, "status": "harness_error",
                            "error": str(exc)[:200], "calls": dict(harness.fixture.calls)})
            continue
        case_score = score(case, observation)
        scores.append(case_score)
        results.append({
            "id": case.id, "category": case.category, "status": observation.persisted_status,
            "expected_status": case.expected_status,
            "status_matches_expectation": observation.persisted_status == case.expected_status,
            "event_count": len(observation.events),
            "calls": dict(observation.call_attempts),
            "total_seconds": round(observation.total_seconds or 0.0, 4),
            "answer_present": bool(observation.answer),
            "blocking_codes": list(case_score.blocking_codes),
            "citation_support_found": case_score.citation_support_found,
            "citation_support_expected": case_score.citation_support_expected,
            "usage_reported": case_score.usage_reported,
        })

    report = build_report(suite.suite_id, suite.corpus_version, _code_revision(), scores,
                          elapsed_seconds=time.perf_counter() - started,
                          clock=FROZEN_NOW.isoformat(),
                          context_window_strategy=args.context_window_strategy,
                          context_budget=args.context_budget)
    limits_by_case = {case.id: dict(case.max_calls) for case in selected}
    for item in report["cases"]:
        item["call_limits"] = limits_by_case.get(item["case_id"], {})
    harness_errors = sum(1 for item in results if item["status"] == "harness_error")
    payload = {
        **report,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "runs": results,
        "totals": {**report["totals"], "harness_errors": harness_errors,
                   "statuses_matching_expectation": sum(1 for item in scores if item.terminal_matches)},
    }
    write_report(output_path, payload)
    print(json.dumps(payload["totals"], ensure_ascii=False))
    return 0 if harness_errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
