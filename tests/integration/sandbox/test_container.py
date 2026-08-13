from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from uuid import uuid4

import docker
import pytest

from execution.repositories import CommandSpec
from execution.sandbox.policy import SandboxPolicy
from execution.sandbox.runner import DockerSandboxRunner, SandboxRequest


@pytest.fixture(scope="module")
def pinned_runner_image() -> str:
    try:
        image = docker.from_env().images.get("autonomous-swe-backend:latest")
    except docker.errors.DockerException as exc:
        pytest.skip(f"controlled local Docker image is unavailable: {exc}")
    digests = image.attrs.get("RepoDigests", [])
    if not digests:
        pytest.skip("controlled local Docker image has no immutable digest")
    return str(digests[0])


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir(mode=0o755)
    worktree.mkdir(mode=0o777)
    return source, worktree


def request(
    image: str,
    repositories: tuple[Path, Path],
    argv: tuple[str, ...],
    **policy_overrides: object,
) -> SandboxRequest:
    source, worktree = repositories
    values: dict[str, object] = {
        "image": image,
        "uid": max(1, os.getuid()),
        "gid": max(1, os.getgid()),
        "cpu_nanos": 500_000_000,
        "cpu_time_limit_ms": 30_000,
        "memory_bytes": 128 * 1024 * 1024,
        "pids_limit": 32,
        "timeout_seconds": 10,
        "max_stdout_bytes": 1_024,
        "max_stderr_bytes": 1_024,
        "max_total_output_bytes": 2_048,
    }
    values.update(policy_overrides)
    policy = SandboxPolicy.model_validate(values)
    return SandboxRequest(
        execution_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        attempt_id=uuid4(),
        source_repository=source,
        worktree=worktree,
        command=CommandSpec(
            argv=argv,
            timeout_seconds=min(10, policy.timeout_seconds),
        ),
        policy=policy,
    )


def test_network_is_disabled_by_default(
    pinned_runner_image: str,
    repositories: tuple[Path, Path],
) -> None:
    runner = DockerSandboxRunner()
    spec = request(
        pinned_runner_image,
        repositories,
        (
            "python",
            "-c",
            "import socket; socket.create_connection(('1.1.1.1', 53), .25)",
        ),
    )

    result = runner.run(spec)

    assert result.execution.exit_code != 0
    assert result.execution.network_requests == 0
    assert result.execution.network_bytes_sent == 0
    assert result.execution.network_bytes_received == 0


def test_container_boundary_mounts_source_read_only_and_drops_host_privilege(
    pinned_runner_image: str,
    repositories: tuple[Path, Path],
) -> None:
    runner = DockerSandboxRunner()
    spec = request(
        pinned_runner_image,
        repositories,
        ("python", "-c", "print('boundary')"),
    )
    container_id = runner.create(spec)
    try:
        container = docker.from_env().containers.get(container_id)
        container.reload()
        host_config = container.attrs["HostConfig"]
        mounts = {mount["Destination"]: mount for mount in container.attrs["Mounts"]}

        assert host_config["Privileged"] is False
        assert host_config["ReadonlyRootfs"] is True
        assert host_config["NetworkMode"] == "none"
        assert host_config["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in host_config["SecurityOpt"]
        assert host_config["PidsLimit"] == spec.policy.pids_limit
        assert host_config["Memory"] == spec.policy.memory_bytes
        assert host_config["NanoCpus"] == spec.policy.cpu_nanos
        assert mounts["/source"]["RW"] is False
        assert mounts["/workspace"]["RW"] is True
    finally:
        runner.release(spec.execution_id, container_id)


def test_output_truncation_is_byte_accurate(
    pinned_runner_image: str,
    repositories: tuple[Path, Path],
) -> None:
    result = DockerSandboxRunner().run(
        request(
            pinned_runner_image,
            repositories,
            (
                "python",
                "-c",
                "import sys; sys.stdout.buffer.write(b'abcdefghijklmnopqrst'); sys.stdout.flush()",
            ),
            max_stdout_bytes=10,
            max_stderr_bytes=100,
            max_total_output_bytes=110,
        )
    )

    assert result.stdout == "abcdefghij"
    assert result.stdout_truncated is True
    assert result.execution.stdout_bytes == 20
    assert len(result.stdout.encode()) == 10
    assert result.execution.exit_reason == "OUTPUT_LIMIT"
    assert result.execution.limit_triggered == "output_bytes"


def test_timeout_and_cancellation_have_explicit_exit_reasons(
    pinned_runner_image: str,
    repositories: tuple[Path, Path],
) -> None:
    timeout_result = DockerSandboxRunner().run(
        request(
            pinned_runner_image,
            repositories,
            ("python", "-c", "import time; time.sleep(5)"),
            timeout_seconds=1,
        )
    )
    assert timeout_result.execution.exit_reason == "TIMEOUT"
    assert timeout_result.execution.limit_triggered == "wall_time"

    runner = DockerSandboxRunner()
    cancellation_request = request(
        pinned_runner_image,
        repositories,
        ("python", "-c", "import time; time.sleep(30)"),
    )

    async def cancel_running() -> object:
        running = asyncio.create_task(asyncio.to_thread(runner.run, cancellation_request))
        deadline = time.monotonic() + 5
        while runner.container_id(cancellation_request.execution_id) is None:
            if time.monotonic() >= deadline:
                pytest.fail("container ID was not made observable")
            await asyncio.sleep(0.02)
        runner.cancel(cancellation_request.execution_id)
        return await running

    cancelled = asyncio.run(cancel_running())
    assert cancelled.execution.exit_reason == "CANCELLED"  # type: ignore[union-attr]
    assert cancelled.execution.limit_triggered == "cancellation"  # type: ignore[union-attr]


def test_actual_usage_telemetry_is_reported(
    pinned_runner_image: str,
    repositories: tuple[Path, Path],
) -> None:
    result = DockerSandboxRunner().run(
        request(
            pinned_runner_image,
            repositories,
            ("python", "-c", "import time; print('telemetry'); time.sleep(1.5)"),
        )
    )
    usage = result.execution

    assert usage.cpu_time_ms >= 0
    assert usage.peak_memory_bytes > 0
    assert usage.peak_processes >= 1
    assert usage.processes_created is None
    assert usage.stdout_bytes == len(b"telemetry\n")
    assert usage.stderr_bytes == 0
    assert usage.duration_ms > 0
    assert usage.exit_code == 0
    assert usage.exit_reason == "COMPLETED"
    assert usage.measurement_source == "docker_engine_stats_v1"


def test_cpu_and_memory_limits_produce_explicit_reasons(
    pinned_runner_image: str,
    repositories: tuple[Path, Path],
) -> None:
    cpu = DockerSandboxRunner().run(
        request(
            pinned_runner_image,
            repositories,
            ("python", "-c", "while True: pass"),
            cpu_nanos=1_000_000_000,
            cpu_time_limit_ms=100,
            timeout_seconds=5,
        )
    )
    assert cpu.execution.exit_reason == "CPU_LIMIT"
    assert cpu.execution.limit_triggered == "cpu_time"

    memory = DockerSandboxRunner().run(
        request(
            pinned_runner_image,
            repositories,
            ("python", "-c", "bytearray(256 * 1024 * 1024)"),
            memory_bytes=32 * 1024 * 1024,
        )
    )
    assert memory.execution.exit_reason == "MEMORY_LIMIT"
    assert memory.execution.limit_triggered == "memory"


def test_pid_limit_produces_an_explicit_reason(
    pinned_runner_image: str,
    repositories: tuple[Path, Path],
) -> None:
    program = (
        "exec(\"import subprocess, sys, time\\n"
        "children = []\\n"
        "try:\\n"
        "    for _ in range(20):\\n"
        "        children.append(subprocess.Popen(['sleep', '5']))\\n"
        "except OSError:\\n"
        "    time.sleep(1.5)\\n"
        "    sys.exit(7)\")"
    )
    result = DockerSandboxRunner().run(
        request(
            pinned_runner_image,
            repositories,
            ("python", "-c", program),
            pids_limit=4,
            timeout_seconds=5,
        )
    )

    assert result.execution.exit_reason == "PID_LIMIT"
    assert result.execution.limit_triggered == "processes"
