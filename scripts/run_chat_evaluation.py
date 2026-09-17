#!/usr/bin/env python3
"""Replay a versioned offline chat-evaluation fixture into a safe sidecar."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

# Direct execution starts with ``scripts/`` on sys.path; add only the repository
# root so this command can import the offline evaluator, never the application server.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.chat_evaluation import EvaluationFixture, build_quality_summary


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay an offline chat evaluation fixture")
    parser.add_argument("--fixture", required=True, help="Versioned evaluation fixture JSON path")
    parser.add_argument("--output", required=True, help="Safe quality summary JSON path")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Write a safe summary atomically; leave an existing summary untouched on error."""
    try:
        args = _arguments(argv)
        fixture = EvaluationFixture.load(args.fixture)
        payload = build_quality_summary(
            fixture,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f"{output.name}.tmp")
        with open(temporary, "w", encoding="utf-8") as target:
            json.dump(payload, target, ensure_ascii=False, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, output)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"quality evaluation failed: {error}", file=sys.stderr)
        return 1
    print(f"quality summary written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
