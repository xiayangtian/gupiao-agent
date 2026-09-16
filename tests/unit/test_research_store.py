from webapp.chat_models import Scope
from webapp.chat_store import ChatStore
from webapp.research_models import ResearchPlan, ResearchRun, ResearchStep


def test_research_run_persists_across_reload_and_is_session_bound(tmp_path):
    path = tmp_path / "sessions.json"
    store = ChatStore(str(path))
    owner = store.create_session()["id"]
    other = store.create_session()["id"]
    plan = ResearchPlan("研究", Scope.company_only("601288", "农业银行", ["601288:2024-12-31:annual"]), (ResearchStep("r", "retrieve", "检索"),), ("核对",))
    run = ResearchRun.new(plan).transition("running").transition("stopped")
    store.save_research_run(owner, run)
    assert ChatStore(str(path)).get_research_run(owner, run.id).status == "stopped"
    assert store.get_research_run(other, run.id) is None
