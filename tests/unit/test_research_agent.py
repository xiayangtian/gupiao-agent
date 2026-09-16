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


def _production_handlers():
    def retrieve(step, run):
        return {"artifacts": [{"source": "pdf", "report_id": "601288:2024-12-31:annual",
            "pdf_filename": "annual.pdf", "page": 1, "snippet": "可核验披露"}]}
    def answer(step, run): return {"answer": "研究结论见范围内披露。"}
    return {"retrieve": retrieve, "normalize": lambda *_: {}, "compare": lambda *_: {},
            "verify": lambda *_: {}, "answer": answer}


def test_agent_returns_completed_answer_with_plan_and_verified_evidence():
    events = []
    agent = ResearchAgent(planner=ResearchPlanner(), executor=ResearchExecutor(_production_handlers()), verifier=PassedVerifier())
    answer = agent.run("比较近三年盈利质量", _scope(), IntentDecision("research_task"), _policy(), stop_event=Event(), emit=events.append)
    assert answer.status == "completed"
    assert answer.research_run_id
    assert answer.artifacts
    assert any(e["type"] == "research_plan" for e in events)
    assert any(e["type"] == "research_running" and e["status"] == "running" for e in events)
    assert any(e["type"] == "research_step_started" and e["status"] == "running" for e in events)
    assert any(e["type"] == "research_step_completed" for e in events)
    assert any(e["type"] == "research_done" for e in events)


def test_agent_without_production_handlers_cannot_claim_completed():
    events = []
    answer = ResearchAgent(verifier=PassedVerifier()).run(
        "比较近三年盈利质量", _scope(), IntentDecision("research_task"), _policy(), stop_event=Event(), emit=events.append)
    assert answer.status != "completed"
    assert "已按计划核对" not in answer.content
    assert any(e["type"] == "research_step_failed" for e in events)
    assert any(e["type"] == "research_blocked" for e in events)


def test_agent_uses_the_same_persist_callback_for_steps_and_final_run():
    persisted = []
    agent = ResearchAgent(executor=ResearchExecutor(_production_handlers()), verifier=PassedVerifier(),
                          persist=persisted.append)
    answer = agent.run("比较近三年盈利质量", _scope(), IntentDecision("research_task"), _policy(),
                       stop_event=Event(), emit=lambda _: None)
    assert persisted[-1].id == answer.research_run_id
    assert any(len(item.step_runs) == 1 for item in persisted)
    assert persisted[-1].status == "completed"


def test_verifier_blocked_answer_marks_research_partial_not_completed():
    agent = ResearchAgent(planner=ResearchPlanner(), executor=ResearchExecutor(_production_handlers()), verifier=BlockedVerifier())
    answer = agent.run("比较近三年盈利质量", _scope(), IntentDecision("research_task"), _policy(), stop_event=Event(), emit=lambda _: None)
    assert answer.status == "partial"


def test_simple_question_uses_normal_answer_without_plan():
    agent = ResearchAgent(normal_answer=lambda *args: "普通回答")
    answer = agent.run("营收是多少？", _scope(), IntentDecision("report_fact"), _policy(), stop_event=Event(), emit=lambda _: None)
    assert answer.content == "普通回答"
    assert answer.research_run_id == ""
