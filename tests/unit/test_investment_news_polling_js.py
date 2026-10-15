"""Schedule-window tests for investment-news background refresh polling."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
POLLING_JS = ROOT / "webapp" / "static" / "investment_news_polling.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="polling schedule tests require Node.js")


def test_polling_window_is_limited_to_hourly_shanghai_schedule_with_five_minute_buffer():
    completed = subprocess.run(
        [NODE, "-e", f"""
const fs = require('fs');
const vm = require('vm');
const window = {{}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(POLLING_JS))}, 'utf8'), {{ window, Intl, Date }});
const check = value => window.InvestmentNewsPolling.isWithinScheduleWindow(new Date(value));
console.log(JSON.stringify({{
  intervalMs: window.InvestmentNewsPolling.intervalMs,
  windows: [
    check('2026-10-15T00:54:00Z'),
    check('2026-10-15T00:55:00Z'),
    check('2026-10-15T01:05:00Z'),
    check('2026-10-15T01:06:00Z'),
    check('2026-10-15T08:05:00Z'),
    check('2026-10-15T08:06:00Z'),
    check('2026-10-15T08:55:00Z')
  ]
}}));
"""],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    result = json.loads(completed.stdout)
    assert result["intervalMs"] == 30000
    assert result["windows"] == [False, True, True, False, True, False, False]
