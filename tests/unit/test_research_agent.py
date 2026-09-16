from threading import Event

from webapp.chat_models import IntentDecision, Scope, ToolPolicy, VerificationReport
from webapp.research_agent import ResearchAgent
from webapp.research_executor import ResearchExecutor
from webapp.research_planner import ResearchPlanner


def _scope(): return Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"])
def _policy(): return ToolPolicy("research_task")

class PassedVerifier:
    def verify(self, *args): return VerificationReport("passed")
class BlockedVerifier:
    def verify(self, *args): return VerificationReport("blocked")


def test_agent_returns_completed_answer_with_plan_and_verified_evidence():
    events = []
    agent = ResearchAgent(planner=ResearchPlanner(), executor=ResearchExecutor(), verifier=PassedVerifier())
    answer = agent.run("比较近三年盈利质量", _scope(), IntentDecision("research_task"), _policy(), stop_event=Event(), emit=events.append)
    assert answer.status == "completed"
    assert answer.research_run_id
    assert any(e["type"] == "research_plan" for e in events)


def test_verifier_blocked_answer_marks_research_partial_not_completed():
    agent = ResearchAgent(planner=ResearchPlanner(), executor=ResearchExecutor(), verifier=BlockedVerifier())
    answer = agent.run("比较近三年盈利质量", _scope(), IntentDecision("research_task"), _policy(), stop_event=Event(), emit=lambda _: None)
    assert answer.status == "partial"


def test_simple_question_uses_normal_answer_without_plan():
    agent = ResearchAgent(normal_answer=lambda *args: "普通回答")
    answer = agent.run("营收是多少？", _scope(), IntentDecision("report_fact"), _policy(), stop_event=Event(), emit=lambda _: None)
    assert answer.content == "普通回答"
    assert answer.research_run_id == ""
