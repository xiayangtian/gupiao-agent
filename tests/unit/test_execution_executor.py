from webapp.chat_models import Scope
from webapp.execution_executor import ExecutionExecutor
from webapp.execution_plan import ExecutionPlan

def test_market_plan_never_calls_retrieve():
    calls=[]
    plan=ExecutionPlan.from_dict({"objective":"今日行情","source_mode":"external_market","steps":[{"id":"q","kind":"market_quote","required":True},{"id":"w","kind":"web_search","depends_on":["q"]},{"id":"a","kind":"answer","depends_on":["q","w"]}],"acceptance":["时间"]})
    result=ExecutionExecutor(retrieve=lambda *_:calls.append("retrieve"), market_quote=lambda *_:calls.append("quote"), web_search=lambda *_:calls.append("web")).execute(plan,"分析今日行情",Scope.whole_corpus())
    assert calls == ["quote", "web"]
    assert result.source_summary["local_pdf"] == "未使用"
