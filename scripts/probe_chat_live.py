#!/usr/bin/env python3
"""Fail-closed validator for a future, separately approved live-provider probe.

This command intentionally performs configuration validation only. It never loads
provider credentials, imports the chat server, or makes outbound requests.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

_ALLOWED_PROVIDERS = frozenset({"openai", "deepseek", "gemini"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate, but do not execute, an opt-in live chat probe.")
    parser.add_argument("--allow-live", action="store_true", required=True,
                        help="Acknowledge that this configuration is intended for a separately authorized probe.")
    parser.add_argument("--cases", required=True, help="Path to a versioned, reviewed case whitelist.")
    parser.add_argument("--providers", required=True, help="Comma-separated provider allowlist.")
    parser.add_argument("--max-calls", required=True, type=int, help="Positive total call ceiling.")
    parser.add_argument("--max-cost", required=True, help="Positive maximum cost in the configured account currency.")
    parser.add_argument("--timeout", required=True, type=float, help="Positive per-run timeout in seconds.")
    parser.add_argument("--account-profile", required=True,
                        help="Must be exactly 'non-production'; production profiles are refused.")
    return parser


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.environ.get("CI", "").strip().lower() in {"1", "true", "yes", "on"}:
        return _fail("live probe validation is disabled in CI")
    if args.account_profile != "non-production":
        return _fail("account profile must be non-production")
    if args.max_calls <= 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        return _fail("max-calls and timeout must be positive finite limits")
    try:
        maximum_cost = Decimal(args.max_cost)
    except InvalidOperation:
        return _fail("max-cost must be a positive finite decimal")
    if not maximum_cost.is_finite() or maximum_cost <= 0:
        return _fail("max-cost must be a positive finite decimal")

    providers = tuple(dict.fromkeys(item.strip().lower() for item in args.providers.split(",") if item.strip()))
    if not providers or set(providers) - _ALLOWED_PROVIDERS:
        return _fail("provider is empty or outside the reviewed allowlist")
    cases_path = Path(args.cases).expanduser()
    if not cases_path.is_file():
        return _fail("case whitelist was not found")
    try:
        # Load only the strict local schema; it rejects URLs, credentials and unknown providers.
        from webapp.chat_runtime_eval_cases import RuntimeSuite
        suite = RuntimeSuite.load(cases_path)
    except Exception as exc:
        return _fail(f"case whitelist rejected ({type(exc).__name__})")
    if not suite.cases:
        return _fail("case whitelist must contain at least one case")

    # Do not infer authorization to spend quota from the command line flag itself.
    # A separate reviewed implementation is required before any live request can run.
    print(json.dumps({
        "validated_only": True,
        "executed": False,
        "reason": "live provider execution is disabled; no credentials or network were accessed",
        "case_count": len(suite.cases),
        "provider_count": len(providers),
        "max_calls": args.max_calls,
        "max_cost": str(maximum_cost),
        "timeout_seconds": args.timeout,
        "account_profile": "non-production",
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
