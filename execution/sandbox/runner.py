from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, Self
from uuid import UUID

import docker  # type: ignore[import-untyped]
from pydantic import Field, field_validator, model_validator

from domain.models import ContractModel, SandboxExecution
from execution.repositories import CommandSpec
from execution.sandbox.policy import EgressPolicy, SandboxPolicy

_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "CI",
        "LANG",
        "LC_ALL",
        "NODE_ENV",
        "NO_COLOR",
        "PYTHONUNBUFFERED",
        "TZ",
    }
)


class SandboxRequest(ContractModel):
    execution_id: UUID
    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    source_repository: Path
    worktree: Path
    command: CommandSpec
    policy: SandboxPolicy
    environment: dict[
        Annotated[str, Field(min_length=1, max_length=100)],
        Annotated[str, Field(max_length=4_096)],
    ] = Field(default_factory=dict, max_length=20)

    @field_validator("source_repository", "worktree")
    @classmethod
    def canonical_directory(cls, value: Path) -> Path:
        if value.is_symlink():
            raise ValueError("sandbox host paths cannot be symlinks")
        try:
            resolved = value.resolve(strict=True)
        except OSError as exc:
            raise ValueError("sandbox host path must exist") from exc
        if not resolved.is_dir():
            raise ValueError("sandbox host path must be a directory")
        return resolved

    @field_validator("environment")
    @classmethod
    def environment_is_allowlisted(cls, value: dict[str, str]) -> dict[str, str]:
        rejected = sorted(set(value).difference(_ENVIRONMENT_ALLOWLIST))
        if rejected:
            raise ValueError(f"environment variables are not allowlisted: {', '.join(rejected)}")
        return value

    @model_validator(mode="after")
    def boundaries_match_command(self) -> Self:
        if self.source_repository == self.worktree:
            raise ValueError("source repository and mutable worktree must be distinct")
        if self.command.timeout_seconds > self.policy.timeout_seconds:
            raise ValueError("command timeout cannot exceed the sandbox timeout")
        if self.command.network_required and self.policy.egress is EgressPolicy.NONE:
            raise ValueError("networked commands require dependency_proxy egress")
        return self


class SandboxResult(ContractModel):
    execution: SandboxExecution
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


class SandboxInfrastructureError(RuntimeError):
    """The Docker isolation boundary could not be created or inspected."""


class _OutputCollector:
    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._lock = threading.Lock()
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.limit_reached = threading.Event()
        self.complete = threading.Event()
        self.failed = False

    def consume(self, stream: Iterator[Any]) -> None:
        try:
            for frame in stream:
                stdout_chunk: bytes | None
                stderr_chunk: bytes | None
                if isinstance(frame, tuple) and len(frame) == 2:
                    stdout_chunk, stderr_chunk = frame
                elif isinstance(frame, bytes):
                    stdout_chunk, stderr_chunk = frame, None
                else:
                    continue
                if stdout_chunk:
                    self._append(stdout_chunk, stdout=True)
                if stderr_chunk:
                    self._append(stderr_chunk, stdout=False)
        except (OSError, ValueError, docker.errors.DockerException):
            self.failed = True
        finally:
            self.complete.set()

    def _append(self, chunk: bytes, *, stdout: bool) -> None:
        with self._lock:
            if stdout:
                self.stdout_bytes += len(chunk)
                remaining = self._policy.max_stdout_bytes - len(self._stdout)
                if remaining > 0:
                    self._stdout.extend(chunk[:remaining])
                if self.stdout_bytes > self._policy.max_stdout_bytes:
                    self.limit_reached.set()
            else:
                self.stderr_bytes += len(chunk)
                remaining = self._policy.max_stderr_bytes - len(self._stderr)
                if remaining > 0:
                    self._stderr.extend(chunk[:remaining])
                if self.stderr_bytes > self._policy.max_stderr_bytes:
                    self.limit_reached.set()
            if self.stdout_bytes + self.stderr_bytes > self._policy.max_total_output_bytes:
                self.limit_reached.set()

    def output(self) -> tuple[bytes, bytes]:
        with self._lock:
            return bytes(self._stdout), bytes(self._stderr)


class DockerSandboxRunner:
    """Run governed argv arrays in a closed, resource-limited Docker container."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        poll_interval_seconds: float = 0.025,
        stats_interval_seconds: float = 0.1,
    ) -> None:
        if poll_interval_seconds <= 0 or stats_interval_seconds <= 0:
            raise ValueError("sandbox polling intervals must be positive")
        self._client: Any = client or docker.from_env()
        self._poll_interval_seconds = poll_interval_seconds
        self._stats_interval_seconds = stats_interval_seconds
        self._active: dict[UUID, str] = {}
        self._cancelled: set[UUID] = set()
        self._lock = threading.Lock()

    def container_id(self, execution_id: UUID) -> str | None:
        with self._lock:
            return self._active.get(execution_id)

    def cancel(self, execution_id: UUID) -> bool:
        with self._lock:
            self._cancelled.add(execution_id)
            container_id = self._active.get(execution_id)
        if container_id is None:
            return False
        self.kill_container(container_id)
        return True

    def kill_container(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
            container.reload()
            if container.attrs.get("State", {}).get("Running", False):
                container.kill(signal="SIGKILL")
        except docker.errors.NotFound:
            return
        except docker.errors.APIError as exc:
            if getattr(exc, "status_code", None) not in {304, 409}:
                raise SandboxInfrastructureError("cannot terminate sandbox container") from exc

    def create(self, request: SandboxRequest) -> str:
        policy = request.policy
        existing = self._existing_container(request.execution_id)
        if existing is not None:
            container_id = str(existing.id)
            with self._lock:
                self._active[request.execution_id] = container_id
            return container_id
        environment = {
            "CI": "true",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
            "PYTHONUNBUFFERED": "1",
            "TZ": "UTC",
            **request.environment,
        }
        network_mode = "none"
        network_disabled = True
        if policy.egress is EgressPolicy.DEPENDENCY_PROXY:
            assert policy.dependency_proxy_network is not None
            assert policy.dependency_proxy_url is not None
            network_mode = policy.dependency_proxy_network
            network_disabled = False
            proxy = str(policy.dependency_proxy_url)
            environment.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "NO_PROXY": ""})
        try:
            mounts = [
                docker.types.Mount(
                    target="/source",
                    source=str(request.source_repository),
                    type="bind",
                    read_only=True,
                ),
                docker.types.Mount(
                    target="/workspace",
                    source=str(request.worktree),
                    type="bind",
                    read_only=False,
                ),
            ]
            container = self._client.containers.create(
                image=policy.image,
                command=list(request.command.argv),
                detach=True,
                stdin_open=False,
                tty=False,
                working_dir="/workspace",
                user=f"{policy.uid}:{policy.gid}",
                environment=environment,
                labels={
                    "autoswe.managed": "true",
                    "autoswe.execution_id": str(request.execution_id),
                    "autoswe.task_id": str(request.task_id),
                    "autoswe.attempt_id": str(request.attempt_id),
                },
                mounts=mounts,
                network_disabled=network_disabled,
                network_mode=network_mode,
                privileged=False,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                devices=[],
                nano_cpus=policy.cpu_nanos,
                mem_limit=policy.memory_bytes,
                memswap_limit=policy.memory_bytes,
                oom_kill_disable=False,
                pids_limit=policy.pids_limit,
                init=True,
                tmpfs={
                    "/tmp": "rw,noexec,nosuid,nodev,size=64m",  # noqa: S108
                    "/run": "rw,noexec,nosuid,nodev,size=16m",
                },
                log_config=docker.types.LogConfig(
                    type="local",
                    config={
                        "max-size": f"{max(1_048_576, policy.max_total_output_bytes * 2)}b",
                        "max-file": "2",
                    },
                ),
                auto_remove=False,
            )
        except docker.errors.DockerException as exc:
            raise SandboxInfrastructureError("cannot create isolated Docker container") from exc
        container_id = str(container.id)
        with self._lock:
            self._active[request.execution_id] = container_id
        return container_id

    def run(self, request: SandboxRequest) -> SandboxResult:
        container_id = self.create(request)
        try:
            return self.run_created(request, container_id)
        finally:
            self.remove_container(container_id)
            with self._lock:
                self._active.pop(request.execution_id, None)
                self._cancelled.discard(request.execution_id)

    def run_created(self, request: SandboxRequest, container_id: str) -> SandboxResult:
        if self.container_id(request.execution_id) not in {None, container_id}:
            raise SandboxInfrastructureError("execution is bound to another container")
        with self._lock:
            self._active[request.execution_id] = container_id
        try:
            container = self._client.containers.get(container_id)
            container.reload()
            status = str(container.attrs.get("State", {}).get("Status", ""))
            if status == "created":
                container.start()
            elif status not in {"running", "restarting"}:
                raise SandboxInfrastructureError(
                    f"sandbox container cannot start from status {status or 'unknown'}"
                )
            stream = container.attach(
                stdout=True,
                stderr=True,
                stream=True,
                logs=True,
                demux=True,
            )
        except docker.errors.DockerException as exc:
            raise SandboxInfrastructureError("cannot start isolated Docker container") from exc

        collector = _OutputCollector(request.policy)
        output_thread = threading.Thread(
            target=collector.consume,
            args=(stream,),
            daemon=True,
            name=f"sandbox-output-{request.execution_id}",
        )
        output_thread.start()
        started = time.monotonic()
        cpu_time_ms = 0
        peak_memory_bytes = 0
        peak_processes = 0
        network_sent = 0
        network_received = 0
        stat_samples = 0
        next_stats_at = started
        triggered: str | None = None
        try:
            while True:
                now = time.monotonic()
                if now >= next_stats_at:
                    sample = self._stats(container)
                    next_stats_at = now + self._stats_interval_seconds
                    if sample is not None:
                        stat_samples += 1
                        cpu_time_ms = max(cpu_time_ms, sample[0])
                        peak_memory_bytes = max(peak_memory_bytes, sample[1])
                        peak_processes = max(peak_processes, sample[2])
                        network_sent = max(network_sent, sample[3])
                        network_received = max(network_received, sample[4])
                elapsed = now - started
                with self._lock:
                    cancelled = request.execution_id in self._cancelled
                if cancelled:
                    triggered = "cancellation"
                elif collector.limit_reached.is_set():
                    triggered = "output_bytes"
                elif cpu_time_ms >= request.policy.cpu_time_limit_ms:
                    triggered = "cpu_time"
                elif elapsed >= request.policy.timeout_seconds:
                    triggered = "wall_time"
                if triggered is not None:
                    self.kill_container(container_id)
                try:
                    container.reload()
                except docker.errors.NotFound as exc:
                    raise SandboxInfrastructureError("sandbox container disappeared") from exc
                state = container.attrs.get("State", {})
                if not state.get("Running", False):
                    break
                time.sleep(self._poll_interval_seconds)
        finally:
            if not collector.complete.wait(timeout=1):
                _close_stream(stream)
            output_thread.join(timeout=0.5)
            _close_stream(stream)

        duration_ms = max(0, int((time.monotonic() - started) * 1_000))
        container.reload()
        state = container.attrs.get("State", {})
        exit_code_value = state.get("ExitCode")
        exit_code = int(exit_code_value) if isinstance(exit_code_value, int) else None
        oom_killed = bool(state.get("OOMKilled", False))
        exit_reason, limit_triggered = _exit_reason(
            triggered=triggered,
            oom_killed=oom_killed,
            exit_code=exit_code,
            peak_processes=peak_processes,
            pids_limit=request.policy.pids_limit,
        )
        stdout, stderr = collector.output()
        execution = SandboxExecution(
            execution_id=request.execution_id,
            task_id=request.task_id,
            cpu_time_ms=cpu_time_ms,
            peak_memory_bytes=peak_memory_bytes,
            peak_processes=peak_processes,
            processes_created=None,
            stdout_bytes=collector.stdout_bytes,
            stderr_bytes=collector.stderr_bytes,
            duration_ms=duration_ms,
            network_requests=0,
            network_bytes_sent=network_sent,
            network_bytes_received=network_received,
            exit_code=exit_code,
            exit_reason=exit_reason,
            limit_triggered=limit_triggered,
            measurement_source="docker_engine_stats_v1",
            measurement_complete=(
                stat_samples > 0
                and not collector.failed
                and request.policy.egress is EgressPolicy.NONE
            ),
        )
        return SandboxResult(
            execution=execution,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            stdout_truncated=collector.stdout_bytes > len(stdout),
            stderr_truncated=collector.stderr_bytes > len(stderr),
        )

    def remove_container(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
            container.remove(force=True, v=True)
        except docker.errors.NotFound:
            return
        except docker.errors.DockerException as exc:
            raise SandboxInfrastructureError("cannot clean up sandbox container") from exc

    def release(self, execution_id: UUID, container_id: str, *, remove: bool = True) -> None:
        if remove:
            self.remove_container(container_id)
        with self._lock:
            if self._active.get(execution_id) == container_id:
                self._active.pop(execution_id, None)
            self._cancelled.discard(execution_id)

    def _existing_container(self, execution_id: UUID) -> Any | None:
        try:
            containers = self._client.containers.list(
                all=True,
                filters={"label": f"autoswe.execution_id={execution_id}"},
            )
        except docker.errors.DockerException as exc:
            raise SandboxInfrastructureError("cannot reconcile sandbox containers") from exc
        if len(containers) > 1:
            raise SandboxInfrastructureError("multiple containers exist for one execution")
        return containers[0] if containers else None

    @staticmethod
    def _stats(container: Any) -> tuple[int, int, int, int, int] | None:
        try:
            stats = container.stats(stream=False, one_shot=True)
        except docker.errors.DockerException:
            return None
        return _parse_stats(stats)

def _exit_reason(
    *,
    triggered: str | None,
    oom_killed: bool,
    exit_code: int | None,
    peak_processes: int,
    pids_limit: int,
) -> tuple[str, str | None]:
    if triggered == "cancellation":
        return "CANCELLED", "cancellation"
    if triggered == "output_bytes":
        return "OUTPUT_LIMIT", "output_bytes"
    if triggered == "cpu_time":
        return "CPU_LIMIT", "cpu_time"
    if triggered == "wall_time":
        return "TIMEOUT", "wall_time"
    if oom_killed:
        return "MEMORY_LIMIT", "memory"
    if exit_code not in {None, 0} and peak_processes >= pids_limit:
        return "PID_LIMIT", "processes"
    if exit_code == 0:
        return "COMPLETED", None
    return "NON_ZERO_EXIT", None


def _close_stream(stream: Any) -> None:
    response = getattr(stream, "_response", None)
    response_close = getattr(response, "close", None)
    if callable(response_close):
        try:
            response_close()
            return
        except (OSError, ValueError, docker.errors.DockerException):
            pass
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except (OSError, ValueError, docker.errors.DockerException):
            pass


def _parse_stats(stats: Any) -> tuple[int, int, int, int, int] | None:
    if not isinstance(stats, dict):
        return None
    cpu_stats = stats.get("cpu_stats", {})
    memory_stats = stats.get("memory_stats", {})
    pids_stats = stats.get("pids_stats", {})
    if not cpu_stats or not memory_stats or not pids_stats:
        return None
    cpu_usage = cpu_stats.get("cpu_usage", {}) if isinstance(cpu_stats, dict) else {}
    total_usage = cpu_usage.get("total_usage", 0) if isinstance(cpu_usage, dict) else 0
    memory = 0
    if isinstance(memory_stats, dict):
        memory = int(memory_stats.get("max_usage") or memory_stats.get("usage") or 0)
    processes = int(pids_stats.get("current", 0)) if isinstance(pids_stats, dict) else 0
    sent = 0
    received = 0
    networks = stats.get("networks", {})
    if isinstance(networks, dict):
        for network in networks.values():
            if isinstance(network, dict):
                sent += int(network.get("tx_bytes", 0))
                received += int(network.get("rx_bytes", 0))
    return int(total_usage) // 1_000_000, memory, processes, sent, received
