from types import SimpleNamespace
from uuid import uuid4

import pytest

from domain.enums import RiskLevel
from tools.production import ProductionToolSet, RunChecksArguments
from tools.registry import ToolExecutionContext


async def test_new_test_call_observes_edits_but_exact_call_replay_is_stable(tmp_path):
    source, worktree = tmp_path / "source", tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()
    (worktree / "pyproject.toml").write_text('[project]\nname="sample"\nversion="0.1.0"\n')
    (worktree / "requirements.txt").write_text("")
    (worktree / "tests").mkdir()
    marker = worktree / "value.txt"
    marker.write_text("good")
    cached = {}

    class CachedSandbox:
        async def execute(self, request):
            if request.execution_id not in cached:
                cached[request.execution_id] = SimpleNamespace(
                    execution=SimpleNamespace(
                        execution_id=request.execution_id,
                        exit_code=1 if marker.read_text() == "bad" else 0,
                        exit_reason="OUTPUT_LIMIT"
                        if marker.read_text() == "limited"
                        else "COMPLETED",
                    ),
                    stdout="",
                    stderr="",
                )
            return cached[request.execution_id]

    ids = dict(run_id=uuid4(), task_id=uuid4(), attempt_id=uuid4())
    tools = ProductionToolSet(
        source_repository=source,
        worktree=worktree,
        **ids,
        sandbox=CachedSandbox(),
        python_image="python@sha256:" + "a" * 64,
        node_image="node@sha256:" + "b" * 64,
        uid=1000,
        gid=1000,
    )
    context = ToolExecutionContext(
        **ids,
        project_id=uuid4(),
        repository_id=uuid4(),
        baseline_commit="a" * 40,
        agent_role="tester",
        agent_capabilities=frozenset({"verification"}),
        risk_ceiling=RiskLevel.MEDIUM,
        worktree_root=worktree,
    )
    first = context.model_copy(update={"tool_call_id": uuid4()})
    second = context.model_copy(update={"tool_call_id": uuid4()})
    tool = tools.registry().resolve("run_tests", "1.0")
    args = RunChecksArguments(operation="full_test")
    assert (await tool.executor(args, first))["passed"] is True
    marker.write_text("bad")
    assert (await tool.executor(args, second))["passed"] is False
    marker.write_text("good")
    assert (await tool.executor(args, second))["passed"] is False
    assert len(cached) == 2
    marker.write_text("limited")
    limited = context.model_copy(update={"tool_call_id": uuid4()})
    assert (await tool.executor(args, limited))["passed"] is False


def test_structured_tool_output_is_rejected_after_sandbox_policy_failure():
    failed = SimpleNamespace(
        execution=SimpleNamespace(exit_code=0, exit_reason="OUTPUT_LIMIT"),
        stdout='{"content": "partial"}',
        stderr="",
    )
    with pytest.raises(RuntimeError, match="OUTPUT_LIMIT"):
        ProductionToolSet._json_stdout(failed)
