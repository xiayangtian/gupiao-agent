import json
import subprocess


def test_execution_summary_marks_external_plan_without_local_pdf():
    script = "const r=require('./webapp/static/chat_rendering.js'); console.log(JSON.stringify(r.renderExecutionSummary({execution_plan:{steps:[{kind:'market_quote'},{kind:'web_search'},{kind:'answer'}]},source_summary:{local_pdf:'未使用'}})));"
    output = subprocess.check_output(["node", "-e", script], text=True).strip()
    assert "实时行情" in json.loads(output)
    assert "未使用本地财报" in json.loads(output)
