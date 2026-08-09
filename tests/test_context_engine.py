from autoswe.context_engine import ContextEngine, ContextBuilder


def test_context_engine_4d_assembly():
    engine = ContextEngine(workspace_path="/tmp")
    ctx = engine.assemble_context(
        task_request="Add login authentication endpoint",
        repo_files={"app/main.py": "def main(): pass"},
        memory_notes=["User prefers FastAPI router pattern"],
        execution_context={
            "failed_command": "pytest",
            "stack_trace": "AssertionError at test_auth.py:12",
            "current_diff": "--- a/app/main.py\n+++ b/app/main.py",
        },
    )

    assert "Repository Context" in ctx
    assert "Task Context" in ctx
    assert "Memory Context" in ctx
    assert "Execution Context" in ctx
    assert "FastAPI router pattern" in ctx
    assert "AssertionError at test_auth.py:12" in ctx


def test_context_builder_pruning():
    builder = ContextBuilder(token_budget=100)
    huge_text = "Word " * 500
    pruned = builder.prune_text(huge_text, max_chars=200)
    assert len(pruned) <= 220
    assert "... [TRUNCATED FOR TOKEN BUDGET] ..." in pruned


def test_context_builder_no_pruning_needed():
    builder = ContextBuilder(token_budget=100)
    short_text = "Hello World"
    pruned = builder.prune_text(short_text, max_chars=200)
    assert pruned == "Hello World"


def test_context_engine_partial_assembly():
    engine = ContextEngine(workspace_path="/tmp")
    ctx = engine.assemble_context(task_request="Fix bug in calculation")

    assert "Task Context" in ctx
    assert "Goal: Fix bug in calculation" in ctx
    assert "Repository Context" not in ctx
    assert "Memory Context" not in ctx
    assert "Execution Context" not in ctx
