from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from agents.base import AgentInvocation
from agents.gateway import (
    ModelMessage,
    ModelRequest,
    OpenAICompatibleGateway,
    ProviderCapabilities,
    ToolCall,
    ToolDefinition,
)
from apps.worker.nodes import GatewayToolDispatcher
from domain.enums import RiskLevel
from execution.repositories.python import PythonRepositoryAdapter
from tools.gateway import ToolCallResult, ToolExecutionStatus
from tools.production import (
    ApplyPatchArguments,
    ProductionToolSet,
    ReadFileResult,
    RunChecksArguments,
)
from tools.registry import ToolExecutionContext


@pytest.fixture
def production_tools(tmp_path):
    worktree = tmp_path / "worktree"
    source = tmp_path / "source"
    worktree.mkdir()
    source.mkdir()
    (worktree / "pyproject.toml").write_text('[project]\nname="example"\nversion="0.1.0"\n')
    (worktree / "requirements.txt").write_text("")
    (worktree / "tests").mkdir()
    (worktree / "tests" / "test_example.py").write_text("import unittest\n")
    ids = dict(run_id=uuid4(), task_id=uuid4(), attempt_id=uuid4())
    tools = ProductionToolSet(
        source_repository=source,
        worktree=worktree,
        **ids,
        sandbox=None,
        python_image="python",
        node_image="node",
        uid=1000,
        gid=1000,
    )
    context = ToolExecutionContext(
        **ids,
        project_id=uuid4(),
        repository_id=uuid4(),
        baseline_commit="a" * 40,
        agent_role="coder",
        agent_capabilities=frozenset({"verification", "repository-read", "repository-write"}),
        risk_ceiling=RiskLevel.MEDIUM,
        worktree_root=worktree,
    )
    return tools, context


async def test_published_strict_run_tests_schema_explains_operation_target_relationship(
    production_tools,
):
    tools, _ = production_tools
    registered = tools.registry().resolve("run_tests", "1.0")
    captured = []

    async def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway = OpenAICompatibleGateway(
            base_url="http://provider.test/v1",
            client=client,
            max_concurrency=1,
            default_capabilities=ProviderCapabilities.all_supported(),
        )
        await gateway.complete(
            ModelRequest(
                trace_id="published-tool-contract",
                model="test-model",
                messages=(ModelMessage(role="user", content="Run the tests"),),
                output_schema_name="Result",
                output_schema={"type": "object", "properties": {}, "additionalProperties": False},
                tools=(
                    ToolDefinition(
                        name="run_tests",
                        description="Run checks",
                        input_schema=registered.argument_schema,
                    ),
                ),
            )
        )
    function = captured[0]["tools"][0]["function"]
    assert function["strict"] is True
    schema = function["parameters"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"schema_version", "operation", "target"}
    assert not {"oneOf", "anyOf", "allOf", "if", "then"}.intersection(schema)
    assert schema["properties"]["operation"]["enum"] == [
        "lint",
        "typecheck",
        "targeted_test",
        "full_test",
        "build",
    ]
    guidance = schema["properties"]["target"].get("description", "")
    assert '"operation":"full_test","target":null' in guidance
    assert '"operation":"targeted_test","target":"tests/test_example.py"' in guidance
    assert "discovered test file" in guidance
    assert "directory" in guidance
    assert {"type": "null"} in schema["properties"]["target"]["anyOf"]


@pytest.mark.parametrize("operation", ["lint", "typecheck", "full_test", "build"])
def test_untargeted_operations_reject_targets_during_argument_validation(operation):
    with pytest.raises(ValidationError, match="requires target=null"):
        RunChecksArguments(operation=operation, target="tests")
    assert RunChecksArguments(operation=operation).target is None
    assert RunChecksArguments(operation=operation, target=None).target is None


def test_targeted_test_requires_a_nonempty_target_before_execution():
    with pytest.raises(ValidationError, match="targeted_test requires a discovered test file"):
        RunChecksArguments(operation="targeted_test")
    for target in ("", "  "):
        with pytest.raises(ValidationError):
            RunChecksArguments(operation="targeted_test", target=target)
    assert RunChecksArguments(operation="targeted_test", target="tests/test_example.py").target == (
        "tests/test_example.py"
    )


async def test_bad_operation_target_gets_early_feedback_then_recovers_without_adapter_side_effects(
    production_tools,
    monkeypatch,
):
    tools, context = production_tools
    registry = tools.registry()
    inspected = []
    executed = []
    adapter = PythonRepositoryAdapter()

    class Adapters:
        def detect(self, root):
            inspected.append(root)
            return adapter

    tools.adapters = Adapters()

    async def execute(operation, command, **kwargs):
        executed.append(command.argv)
        return SimpleNamespace(
            execution=SimpleNamespace(exit_code=0, exit_reason="COMPLETED", execution_id=uuid4()),
            stdout="tests passed\n",
            stderr="",
        )

    monkeypatch.setattr(tools, "_execute", execute)

    class ValidatingGateway:
        async def execute(self, request, *, context):
            tool = registry.resolve(request.tool_name, request.tool_version)
            arguments = tool.validate_arguments(request.arguments, context)
            tool.authorize(context)
            output = tool.validate_result(await tool.executor(arguments, context))
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                tool_version="1.0",
                status=ToolExecutionStatus.COMPLETED,
                risk=RiskLevel.MEDIUM,
                output=output.model_dump(mode="json"),
                attempts=1,
            )

    invocation = AgentInvocation(
        trace_id="run-tests-recovery",
        run_id=context.run_id,
        task_id=context.task_id,
        attempt_id=context.attempt_id,
        project_id=context.project_id,
        repository_id=context.repository_id,
        baseline_commit=context.baseline_commit,
        goal="Run repository tests",
        input_payload={"agent_role": "coder"},
    )
    dispatcher = GatewayToolDispatcher(
        gateway=ValidatingGateway(),
        registry=registry,
        project_id=context.project_id,
        repository_id=context.repository_id,
        baseline_commit=context.baseline_commit,
        worktree=tools.worktree,
        agent_capabilities=context.agent_capabilities,
        risk_ceiling=context.risk_ceiling,
    )
    denied = await dispatcher.dispatch(
        ToolCall(
            call_id="invalid",
            name="run_tests",
            arguments={"operation": "full_test", "target": "tests"},
        ),
        invocation=invocation,
    )
    assert denied["status"] == "FAILED"
    assert denied["attempts"] == 0
    assert inspected == []
    assert executed == []
    assert "full_test requires target=null" in denied["error"]
    assert len(denied["error"]) < 400
    recovered = await dispatcher.dispatch(
        ToolCall(
            call_id="corrected",
            name="run_tests",
            arguments={"operation": "full_test", "target": None},
        ),
        invocation=invocation,
    )
    assert recovered["status"] == "COMPLETED"
    assert len(inspected) == 1
    assert executed == [("python", "-m", "unittest", "discover", "-s", "tests")]


@pytest.mark.parametrize("target", ["tests", "../outside.py", "tests/test_example.py; echo unsafe"])
async def test_targeted_checks_keep_repository_file_and_command_allowlists(
    production_tools,
    target,
    monkeypatch,
):
    tools, context = production_tools
    executed = []

    async def execute(*args, **kwargs):
        executed.append(args)
        raise AssertionError("unsafe test target reached execution")

    monkeypatch.setattr(tools, "_execute", execute)
    tool = tools.registry().resolve("run_tests", "1.0")
    arguments = tool.validate_arguments({"operation": "targeted_test", "target": target}, context)
    with pytest.raises(ValueError):
        await tool.executor(arguments, context)
    assert executed == []


@pytest.mark.parametrize("content", ["  indented first line\r\nsecond line\t \r\n", " \n\t ", ""])
async def test_read_to_patch_roundtrip_preserves_exact_utf8_content_and_hash(
    production_tools,
    content,
    monkeypatch,
):
    tools, context = production_tools
    raw = content.encode("utf-8")
    sha256 = hashlib.sha256(raw).hexdigest()
    written = []

    async def sandbox_program(operation, command, **kwargs):
        if operation == "read_file":
            payload = {
                "path": "source.txt",
                "content": content,
                "sha256": sha256,
                "size_bytes": len(raw),
            }
        else:
            assert operation == "apply_patch"
            encoded = "".join(command.argv[5:])
            data = base64.b64decode(encoded, validate=True)
            written.append(data)
            payload = {
                "path": command.argv[3],
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        return SimpleNamespace(
            execution=SimpleNamespace(exit_code=0, exit_reason="COMPLETED", execution_id=uuid4()),
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(tools, "_generic", sandbox_program)
    registry = tools.registry()
    reader = registry.resolve("read_file", "1.0")
    result = reader.validate_result(
        await reader.executor(reader.validate_arguments({"path": "source.txt"}, context), context)
    )
    restored = ReadFileResult.model_validate_json(result.model_dump_json())
    assert restored.content == content
    assert hashlib.sha256(restored.content.encode("utf-8")).hexdigest() == restored.sha256
    writer = registry.resolve("apply_patch", "1.0")
    arguments = writer.validate_arguments(
        {"path": "copy.txt", "content": restored.content}, context
    )
    restored_arguments = ApplyPatchArguments.model_validate_json(arguments.model_dump_json())
    output = writer.validate_result(await writer.executor(restored_arguments, context))
    assert written == [raw]
    assert output.sha256 == sha256
    assert output.size_bytes == len(raw)


def test_patch_preserves_content_without_weakening_other_contract_constraints():
    content = "\n  keep indentation and trailing newline\n"
    arguments = ApplyPatchArguments(path=" file.txt ", content=content)
    assert arguments.path == "file.txt"
    assert arguments.content == content
    with pytest.raises(ValidationError):
        ApplyPatchArguments(path="file.txt", content="x" * 250_001)
    with pytest.raises(ValidationError):
        ApplyPatchArguments(path="file.txt", content=content, command="arbitrary shell")
