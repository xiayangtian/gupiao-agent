import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="requires Node.js")


def test_source_status_and_unknown_data_time_are_visible_without_merging_sources():
    script = f"""
    const r = require({json.dumps(str(ROOT / 'webapp/static/chat_rendering.js'))});
    const html = r.renderRunArtifacts({{tool_artifacts:[
      {{provider:'fixture', tool_name:'quote', status:'partial', as_of:'',
       fetched_at:'2026-09-24T10:00:00+08:00', source_id:'s1'}},
      {{provider:'fixture', tool_name:'fund_flow', status:'failed', as_of:'', source_id:'s2'}}
    ]}});
    console.log(JSON.stringify(html));
    """
    output = subprocess.run([NODE, "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)
    html = json.loads(output.stdout)
    assert "数据时间未知" in html
    assert "获取失败" in html
    assert "部分取得" in html
    assert "获取于" in html
    assert html.count("chat-artifact-tool") == 2
