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
