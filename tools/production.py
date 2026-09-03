from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from pydantic import Field, StringConstraints, model_validator

from domain.enums import RiskLevel
from domain.models import ContractModel
from execution.repositories import (
    CommandKind,
    CommandRequest,
    CommandSpec,
    RepositoryAdapterRegistry,
)
from execution.sandbox.policy import EgressPolicy, SandboxPolicy
from execution.sandbox.runner import SandboxRequest, SandboxResult
from observability.tracing import current_correlation
from tools.registry import (
    NetworkProfile,
    ReplayPolicy,
    SideEffectClass,
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
)


class ReadFileArguments(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    path: str = Field(min_length=1, max_length=1_024)
    maximum_bytes: int = Field(default=64_000, ge=1, le=200_000)


class ReadFileResult(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    path: str
    content: Annotated[str, StringConstraints(strip_whitespace=False)]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    truncated: bool = False
    sandbox_execution_id: UUID


class SearchCodeArguments(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    query: str = Field(min_length=1, max_length=500)
    path: str = Field(default=".", min_length=1, max_length=1_024)
    maximum_results: int = Field(default=50, ge=1, le=200)


class SearchMatch(ContractModel):
    path: str
    line: int = Field(ge=1)
    text: str = Field(max_length=2_000)


class SearchCodeResult(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    matches: tuple[SearchMatch, ...]
    truncated: bool
    sandbox_execution_id: UUID


class ApplyPatchArguments(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    path: str = Field(min_length=1, max_length=1_024)
    content: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(max_length=250_000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ApplyPatchResult(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    sandbox_execution_id: UUID


class RunChecksArguments(ContractModel):
    model_config = {"hide_input_in_errors": True}

    schema_version: Literal["1.0"] = "1.0"
    operation: Literal["lint", "typecheck", "targeted_test", "full_test", "build"] = Field(
        description=(
            "Choose a fixed repository check. Only targeted_test accepts a target; "
            "lint, typecheck, full_test and build require target=null."
        )
    )
    target: str | None = Field(
        default=None,
        min_length=1,
        max_length=1_024,
        description=(
            "For targeted_test, supply one discovered test file relative to the repository, "
            "not a directory, shell command or extra flags. For every other operation use null. "
            'Examples: {"operation":"full_test","target":null}; '
            '{"operation":"targeted_test","target":"tests/test_example.py"}.'
        ),
    )

    @model_validator(mode="after")
    def operation_target_contract(self) -> Self:
        # Validate before adapter inspection or sandbox dispatch, using the same
        # authoritative command contract that the fixed adapter allowlist uses.
        try:
            CommandRequest(kind=CommandKind(self.operation), target=self.target)
        except ValueError as error:
            if self.operation == "targeted_test":
                raise ValueError(
                    "targeted_test requires a discovered test file such as tests/test_example.py; "
                    "use full_test with target=null for the whole suite."
                ) from error
            raise ValueError(
                f"{self.operation} requires target=null; use targeted_test with a discovered "
                "test file for a single test."
            ) from error
        return self


class RunChecksResult(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    passed: bool
    command: tuple[str, ...]
    stdout: str
    stderr: str
    exit_code: int | None
    exit_reason: str
    sandbox_execution_id: UUID


_READ_SCRIPT = """import hashlib,json,sys
from pathlib import Path
root=Path('/workspace').resolve(); rel=Path(sys.argv[1]); limit=int(sys.argv[2])
if rel.is_absolute() or '..' in rel.parts: raise SystemExit('unsafe path')
path=(root/rel).resolve(strict=True); path.relative_to(root)
if path.is_symlink() or not path.is_file(): raise SystemExit('not a regular file')
full_data=path.read_bytes(); full_sha=hashlib.sha256(full_data).hexdigest(); full_size=len(full_data)
truncated=full_size>limit
data=full_data[:limit] if truncated else full_data
text=data.decode('utf-8', errors='replace')
print(json.dumps({'path':rel.as_posix(),'content':text,'sha256':full_sha,'size_bytes':full_size,'truncated':truncated}))
"""

_SEARCH_SCRIPT = """import json,sys
from pathlib import Path
root=Path('/workspace').resolve(); rel=Path(sys.argv[1]); query=sys.argv[2]; limit=int(sys.argv[3])
if rel.is_absolute() or '..' in rel.parts: raise SystemExit('unsafe path')
base=(root/rel).resolve(strict=True); base.relative_to(root)
files=[base] if base.is_file() else sorted(base.rglob('*'))
matches=[]; truncated=False
for path in files:
 if len(matches)>=limit: truncated=True; break
 if path.is_symlink() or not path.is_file() or '.git' in path.parts: continue
 try: lines=path.read_text(encoding='utf-8').splitlines()
 except (UnicodeDecodeError,OSError): continue
 for number,line in enumerate(lines,1):
  if query in line:
   matches.append({'path':path.relative_to(root).as_posix(),'line':number,'text':line[:2000]})
   if len(matches)>=limit: break
print(json.dumps({'matches':matches,'truncated':truncated}))
"""

_WRITE_SCRIPT = """import base64,hashlib,json,os,sys,tempfile
from pathlib import Path
root=Path('/workspace').resolve(); rel=Path(sys.argv[1])
expected=None if sys.argv[2]=='-' else sys.argv[2]
if rel.is_absolute() or '..' in rel.parts: raise SystemExit('unsafe path')
path=(root/rel).resolve(strict=False); path.relative_to(root)
parent=path.parent; parent.mkdir(parents=True,exist_ok=True)
parent.resolve(strict=True).relative_to(root)
if path.exists() and path.is_symlink(): raise SystemExit('symlink target forbidden')
if expected is not None:
 current=(hashlib.sha256(path.read_bytes()).hexdigest()
          if path.exists() else hashlib.sha256(b'').hexdigest())
 if current!=expected: raise SystemExit('expected_sha256 mismatch')
data=base64.b64decode(''.join(sys.argv[3:]),validate=True)
fd,name=tempfile.mkstemp(prefix='.autoswe-',dir=parent)
try:
 with os.fdopen(fd,'wb') as stream: stream.write(data); stream.flush(); os.fsync(stream.fileno())
 os.replace(name,path)
finally:
 if os.path.exists(name): os.unlink(name)
print(json.dumps({'path':rel.as_posix(),'sha256':hashlib.sha256(data).hexdigest(),'size_bytes':len(data)}))
"""


def _fixed_python_program(source: str) -> str:
    """Encode a trusted multiline program without weakening the argv control policy."""
    return f"exec({source!r})"


class SandboxManagerClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=3_700)
        self._owns_client = client is None

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        headers = dict(current_correlation().to_headers())
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        response = await self._client.post(
            f"{self._base_url}/executions",
            headers=headers,
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        return SandboxResult.model_validate(response.json())

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class ProductionToolSet:
    """Production registrations backed by constrained sandbox-manager requests."""

    _ROLES = frozenset(
        {"researcher", "coder", "tester", "reviewer", "debugger", "documentation", "validation"}
    )

    def __init__(
        self,
        *,
        source_repository: Path,
        worktree: Path,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        sandbox: SandboxManagerClient,
        python_image: str,
        node_image: str,
        uid: int,
        gid: int,
        adapters: RepositoryAdapterRegistry | None = None,
    ) -> None:
        self.source_repository = source_repository
        self.worktree = worktree
        self.run_id = run_id
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.sandbox = sandbox
        self.python_image = python_image
        self.node_image = node_image
        self.uid = uid
        self.gid = gid
        self.adapters = adapters or RepositoryAdapterRegistry.default()

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            self._spec(
                "read_file",
                ReadFileArguments,
                ReadFileResult,
                capability="repository-read",
                risk=RiskLevel.LOW,
                side_effect=SideEffectClass.NONE,
                path_fields=("path",),
            ),
            self._read_file,
        )
        registry.register(
            self._spec(
                "search_code",
                SearchCodeArguments,
                SearchCodeResult,
                capability="repository-read",
                risk=RiskLevel.LOW,
                side_effect=SideEffectClass.NONE,
                path_fields=("path",),
            ),
            self._search_code,
        )
        registry.register(
            self._spec(
                "apply_patch",
                ApplyPatchArguments,
                ApplyPatchResult,
                capability="repository-write",
                risk=RiskLevel.MEDIUM,
                side_effect=SideEffectClass.LOCAL,
                replay=ReplayPolicy.IDEMPOTENT,
                path_fields=("path",),
            ),
            self._apply_patch,
        )
        registry.register(
            self._spec(
                "run_tests",
                RunChecksArguments,
                RunChecksResult,
                capability="verification",
                risk=RiskLevel.MEDIUM,
                side_effect=SideEffectClass.LOCAL,
                replay=ReplayPolicy.SAFE,
                timeout=1_900,
            ),
            self._run_checks,
        )
        return registry

    def _spec(
        self,
        name: str,
        argument_model: type[ContractModel],
        result_model: type[ContractModel],
        *,
        capability: str,
        risk: RiskLevel,
        side_effect: SideEffectClass,
        replay: ReplayPolicy = ReplayPolicy.SAFE,
        path_fields: tuple[str, ...] = (),
        timeout: float = 120,
    ) -> ToolSpec:
        return ToolSpec(
            name=name,
            version="1.0",
            argument_model=argument_model,
            result_model=result_model,
            owning_capability=capability,
            eligible_agents=self._ROLES,
            base_risk=risk,
            timeout_seconds=timeout,
            max_attempts=2,
            replay_policy=replay,
            sandbox_profile="task-isolated",
            network_profile=NetworkProfile.NONE,
            side_effect=side_effect,
            approval_required=False,
            path_fields=path_fields,
        )

    async def _read_file(
        self, arguments: ContractModel, context: ToolExecutionContext
    ) -> dict[str, Any]:
        values = ReadFileArguments.model_validate(arguments)
        result = await self._generic(
            "read_file",
            CommandSpec(
                argv=(
                    "python",
                    "-c",
                    _fixed_python_program(_READ_SCRIPT),
                    values.path,
                    str(values.maximum_bytes),
                ),
                timeout_seconds=30,
            ),
            tool_call_id=context.tool_call_id,
        )
        payload = self._json_stdout(result)
        payload["sandbox_execution_id"] = str(result.execution.execution_id)
        return payload

    async def _search_code(
        self, arguments: ContractModel, context: ToolExecutionContext
    ) -> dict[str, Any]:
        values = SearchCodeArguments.model_validate(arguments)
        result = await self._generic(
            "search_code",
            CommandSpec(
                argv=(
                    "python",
                    "-c",
                    _fixed_python_program(_SEARCH_SCRIPT),
                    values.path,
                    values.query,
                    str(values.maximum_results),
                ),
                timeout_seconds=60,
            ),
            tool_call_id=context.tool_call_id,
        )
        payload = self._json_stdout(result)
        payload["sandbox_execution_id"] = str(result.execution.execution_id)
        return payload

    async def _apply_patch(
        self, arguments: ContractModel, context: ToolExecutionContext
    ) -> dict[str, Any]:
        values = ApplyPatchArguments.model_validate(arguments)
        encoded = base64.b64encode(values.content.encode()).decode("ascii")
        chunks = tuple(
            encoded[index : index + 3_500] for index in range(0, len(encoded), 3_500)
        )
        result = await self._generic(
            "apply_patch",
            CommandSpec(
                argv=(
                    "python",
                    "-c",
                    _fixed_python_program(_WRITE_SCRIPT),
                    values.path,
                    values.expected_sha256 or "-",
                    *chunks,
                ),
                timeout_seconds=60,
            ),
            tool_call_id=context.tool_call_id,
        )
        payload = self._json_stdout(result)
        payload["sandbox_execution_id"] = str(result.execution.execution_id)
        return payload

    async def _run_checks(
        self, arguments: ContractModel, context: ToolExecutionContext
    ) -> dict[str, Any]:
        values = RunChecksArguments.model_validate(arguments)
        kind = CommandKind(values.operation)
        adapter = self.adapters.detect(self.worktree)
        manifest = adapter.inspect(self.worktree)
        command = adapter.command(manifest, CommandRequest(kind=kind, target=values.target))
        result = await self._execute(
            f"run_checks:{values.operation}:{values.target or '-'}",
            command,
            image=self.python_image if manifest.adapter == "python" else self.node_image,
            tool_call_id=context.tool_call_id,
        )
        return {
            "passed": (
                result.execution.exit_code == 0 and result.execution.exit_reason == "COMPLETED"
            ),
            "command": list(command.argv),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.execution.exit_code,
            "exit_reason": result.execution.exit_reason,
            "sandbox_execution_id": str(result.execution.execution_id),
        }

    async def _generic(
        self, operation: str, command: CommandSpec, *, tool_call_id: UUID | None = None
    ) -> SandboxResult:
        return await self._execute(
            operation, command, image=self.python_image, tool_call_id=tool_call_id
        )

    async def _execute(
        self, operation: str, command: CommandSpec, *, image: str, tool_call_id: UUID | None = None
    ) -> SandboxResult:
        execution_id = uuid5(
            NAMESPACE_URL,
            f"sandbox:{self.attempt_id}:{tool_call_id}:{operation}:{json.dumps(command.argv)}",
        )
        request = SandboxRequest(
            execution_id=execution_id,
            run_id=self.run_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            source_repository=self.source_repository,
            worktree=self.worktree,
            command=command,
            policy=SandboxPolicy(
                image=image,
                uid=self.uid,
                gid=self.gid,
                cpu_nanos=2_000_000_000,
                cpu_time_limit_ms=1_800_000,
                memory_bytes=1_073_741_824,
                pids_limit=256,
                timeout_seconds=max(command.timeout_seconds, 60),
                max_stdout_bytes=262_144,
                max_stderr_bytes=262_144,
                max_total_output_bytes=524_288,
                egress=EgressPolicy.NONE,
            ),
        )
        return await self.sandbox.execute(request)

    @staticmethod
    def _json_stdout(result: SandboxResult) -> dict[str, Any]:
        if result.execution.exit_code != 0 or result.execution.exit_reason != "COMPLETED":
            raise RuntimeError(f"sandbox {result.execution.exit_reason}: {result.stderr[:1_000]}")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("sandbox tool returned invalid JSON") from error
        if not isinstance(value, dict):
            raise RuntimeError("sandbox tool result must be an object")
        return value


async def close_tool_set(tool_set: ProductionToolSet) -> None:
    await asyncio.shield(tool_set.sandbox.close())
