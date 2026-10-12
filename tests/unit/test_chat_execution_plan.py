import json
import subprocess


def test_execution_summary_marks_external_plan_without_local_pdf():
    script = "const r=require('./webapp/static/chat_rendering.js'); console.log(JSON.stringify(r.renderExecutionSummary({execution_plan:{source_mode:'market_recap',steps:[{kind:'market_overview'},{kind:'web_search'},{kind:'answer'}]},source_summary:{local_pdf:'未使用'}})));"
    output = subprocess.check_output(["node", "-e", script], text=True).strip()
    assert "A 股市场概况" in json.loads(output)
    assert "A 股市场复盘" in json.loads(output)
    assert "未查询本地财报" in json.loads(output)


def test_execution_summary_marks_general_web_as_model_and_web_only():
    script = "const r=require('./webapp/static/chat_rendering.js'); console.log(JSON.stringify(r.renderExecutionSummary({execution_plan:{source_mode:'general_web',steps:[{kind:'web_search'},{kind:'answer'}]},source_summary:{local_pdf:'未使用'}})));"
    output = subprocess.check_output(["node", "-e", script], text=True).strip()
    assert "结合网页资料补充" in json.loads(output)
