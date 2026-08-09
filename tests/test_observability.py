# tests/test_observability.py
import os
from autoswe.observability import TokenCostTracker, LangSmithTracer, setup_observability

def test_token_cost_tracker():
    tracker = TokenCostTracker()
    cost1 = tracker.record_usage("gemini-3.6-flash", prompt_tokens=10000, completion_tokens=2000)
    assert tracker.total_prompt_tokens == 10000
    assert tracker.total_completion_tokens == 2000
    assert cost1 > 0.0
    assert tracker.total_cost_usd == cost1

def test_langsmith_tracer_metadata():
    tracer = LangSmithTracer(project_name="test-project")
    meta = tracer.get_run_metadata(task_id="task-99", agent_role="Coder", model_name="gemini-3.6-flash")
    assert meta["task_id"] == "task-99"
    assert meta["agent_role"] == "Coder"
    assert meta["langsmith_project"] == "test-project"

def test_setup_observability():
    config = setup_observability()
    assert config.project_name is not None
