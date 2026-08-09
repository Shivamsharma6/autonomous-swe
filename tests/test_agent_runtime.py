# tests/test_agent_runtime.py
from autoswe.agent_runtime import AgentRuntime, get_default_agent_specs
from autoswe.models import AgentSpec


def test_default_agent_specs():
    specs = get_default_agent_specs()
    assert "Architect" in specs
    assert "Researcher" in specs
    assert "Coder" in specs
    assert "Tester" in specs
    assert "Reviewer" in specs
    assert "Debugger" in specs
    assert "Final Reviewer" in specs

    # Verify specs are AgentSpec instances and have expected roles
    assert specs["Architect"].role == "Architect"
    assert specs["Researcher"].role == "Researcher"
    assert specs["Coder"].role == "Coder"
    assert specs["Tester"].role == "Test Generator"
    assert specs["Reviewer"].role == "Reviewer"
    assert specs["Debugger"].role == "Debugger"
    assert specs["Final Reviewer"].role == "Final Reviewer"


def test_agent_runtime_invocation():
    spec = get_default_agent_specs()["Architect"]
    runtime = AgentRuntime(spec=spec)
    prompt = runtime.build_agent_prompt(task_goal="Create user authentication API")
    assert "Architect" in prompt
    assert "Create user authentication API" in prompt
    assert "Decomposes requirements into structured task DAGs" in prompt


def test_agent_runtime_build_prompt_with_context():
    spec = get_default_agent_specs()["Coder"]
    runtime = AgentRuntime(spec=spec)
    context = "### Repository Context\nFile: `app.py`"
    prompt = runtime.build_agent_prompt(task_goal="Implement login endpoint", assembled_context=context)
    assert "Coder" in prompt
    assert "Implement login endpoint" in prompt
    assert "### Repository Context" in prompt
    assert "write_file" in prompt
