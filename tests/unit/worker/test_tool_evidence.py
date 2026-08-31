from uuid import uuid4

import pytest

from apps.worker.nodes import NodeAgentOutput, validate_tool_evidence
from domain.enums import RiskLevel
from tools.gateway import ToolCallResult, ToolExecutionStatus


def result(
    name: str,
    *,
    status: ToolExecutionStatus = ToolExecutionStatus.COMPLETED,
    output: dict[str, object] | None = None,
) -> ToolCallResult:
    return ToolCallResult(
        call_id=uuid4(),
        tool_name=name,
        tool_version="1.0",
        status=status,
        output=output or {},
        risk=RiskLevel.MEDIUM,
        attempts=1,
    )


def test_mutation_and_verification_outputs_require_matching_successful_tool_evidence() -> None:
    validate_tool_evidence(
        "implement",
        NodeAgentOutput(summary="implemented", changed_paths=("src/app.py",)),
        (result("apply_patch", output={"path": "src/app.py"}),),
    )
    validate_tool_evidence(
        "verify",
        NodeAgentOutput(summary="failed as expected", verification_passed=False),
        (result("run_tests", output={"passed": False}),),
    )

    with pytest.raises(RuntimeError, match="successful apply_patch"):
        validate_tool_evidence(
            "implement",
            NodeAgentOutput(summary="claimed implementation"),
            (result("apply_patch", status=ToolExecutionStatus.FAILED),),
        )
    with pytest.raises(RuntimeError, match="does not match"):
        validate_tool_evidence(
            "verify",
            NodeAgentOutput(summary="claimed pass", verification_passed=True),
            (result("run_tests", output={"passed": False}),),
        )


def test_a_failed_read_must_be_recovered_before_agent_can_finish() -> None:
    failed = result("read_file", status=ToolExecutionStatus.FAILED)
    with pytest.raises(RuntimeError, match="unrecovered tool failure"):
        validate_tool_evidence("investigate", NodeAgentOutput(summary="done"), (failed,))

    validate_tool_evidence(
        "investigate",
        NodeAgentOutput(summary="done after retry"),
        (failed, result("read_file", output={"content": "verified"})),
    )


@pytest.mark.parametrize("node", ["implement", "generate_tests", "refactor", "draft"])
def test_mutation_stage_cannot_complete_without_a_patch(node: str) -> None:
    with pytest.raises(RuntimeError, match="successful apply_patch"):
        validate_tool_evidence(node, NodeAgentOutput(summary="Done"), ())


def test_research_cannot_claim_it_investigated_without_reading() -> None:
    with pytest.raises(RuntimeError, match="repository evidence"):
        validate_tool_evidence("investigate", NodeAgentOutput(summary="Investigated crashes"), ())


@pytest.mark.parametrize("node", ["targeted_test", "execute", "regression_verify", "verify"])
def test_test_stage_cannot_complete_without_executing_tests(node: str) -> None:
    with pytest.raises(RuntimeError, match="run_tests"):
        validate_tool_evidence(node, NodeAgentOutput(summary="Tests passed"), ())


def test_verification_claim_requires_a_test_result() -> None:
    with pytest.raises(RuntimeError, match="run_tests"):
        validate_tool_evidence(
            "evidence", NodeAgentOutput(summary="Passed", verification_passed=True), ()
        )


def test_patch_paths_must_match_the_recorded_writes() -> None:
    with pytest.raises(RuntimeError, match="changed paths"):
        validate_tool_evidence(
            "implement",
            NodeAgentOutput(summary="Changed both", changed_paths=("a.py", "b.py")),
            (result("apply_patch", output={"path": "a.py"}),),
        )


def test_pre_patch_tests_cannot_support_a_post_patch_verification_claim() -> None:
    prior = (result("run_tests", output={"passed": True}),)
    patch = result("apply_patch", output={"path": "app.py"})
    output = NodeAgentOutput(
        summary="verified refactor", changed_paths=("app.py",), verification_passed=True
    )
    with pytest.raises(RuntimeError, match="run_tests"):
        validate_tool_evidence("refactor", output, (patch,), prior_results=prior)
    with pytest.raises(RuntimeError, match="run_tests"):
        validate_tool_evidence("refactor", output, (*prior, patch))


def test_documentation_examples_require_execution_evidence() -> None:
    with pytest.raises(RuntimeError, match="run_tests"):
        validate_tool_evidence("validate_examples", NodeAgentOutput(summary="examples pass"), ())
