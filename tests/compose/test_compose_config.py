from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
REQUIRED_SERVICES = {
    "api",
    "dispatcher",
    "workers",
    "postgres",
    "redis",
    "sandbox-manager",
    "docker-socket-proxy",
    "web",
}
PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    environment = os.environ | {
        "AUTOSWE_ADMIN_TOKEN": "a" * 40,
        "AUTOSWE_POSTGRES_PASSWORD": "compose-test-password",
        "AUTOSWE_UAMS_URL": "https://uams.example.invalid",
        "AUTOSWE_UAMS_TOKEN": "compose-uams-token",
        "AUTOSWE_MODEL_BASE_URL": "https://model.example.invalid/v1",
        "AUTOSWE_MODEL_API_KEY": "compose-model-token",
        "AUTOSWE_HOST_RUNTIME_ROOT": str(ROOT / ".compose-test-runtime"),
    }
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is required for Compose validation")
    completed = subprocess.run(  # noqa: S603 - resolved Docker path and fixed arguments
        (docker, "compose", "config", "--format", "json"),
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_required_services_have_health_resources_restart_and_security(
    compose: dict[str, Any],
) -> None:
    services = compose["services"]

    assert REQUIRED_SERVICES.issubset(services)
    for name in REQUIRED_SERVICES:
        service = services[name]
        assert service.get("healthcheck"), name
        assert service.get("restart") == "unless-stopped", name
        limits = service.get("deploy", {}).get("resources", {}).get("limits", {})
        assert limits.get("cpus") and limits.get("memory"), name
        assert service.get("security_opt") == ["no-new-privileges:true"], name
        assert service.get("cap_drop") == ["ALL"], name


def test_external_images_are_digest_pinned_and_builds_have_pinned_bases(
    compose: dict[str, Any],
) -> None:
    services = compose["services"]
    for name, service in services.items():
        if "build" not in service:
            assert PINNED.search(service["image"]), name

    dockerfile = (ROOT / "Dockerfile").read_text()
    assert dockerfile.count("FROM ") >= 2
    named_stages: set[str] = set()
    for line in dockerfile.splitlines():
        if line.startswith("FROM "):
            parts = line.split()
            image = parts[1]
            if image not in named_stages:
                assert PINNED.search(image), line
            if len(parts) >= 4 and parts[-2].casefold() == "as":
                named_stages.add(parts[-1])


def test_persistence_internal_networks_and_socket_proxy_boundary(
    compose: dict[str, Any],
) -> None:
    services = compose["services"]
    volumes = compose["volumes"]
    networks = compose["networks"]

    assert {"postgres-data", "redis-data"}.issubset(volumes)
    assert networks["control"]["internal"] is True
    assert networks["docker-api"]["internal"] is True
    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]
    assert "ports" not in services["docker-socket-proxy"]
    assert "ports" not in services["sandbox-manager"]
    socket_users = []
    for name, service in services.items():
        for mount in service.get("volumes", []):
            source = mount.get("source", "") if isinstance(mount, dict) else str(mount)
            if source == "/var/run/docker.sock":
                socket_users.append(name)
                assert mount.get("read_only") is True
    assert socket_users == ["docker-socket-proxy"]
    assert services["sandbox-manager"]["environment"]["DOCKER_HOST"].startswith("tcp://")


def test_uams_is_external_only_and_no_credentials_are_committed(
    compose: dict[str, Any],
) -> None:
    source_files = (
        (ROOT / "docker-compose.yml").read_text(),
        (ROOT / "docker-compose.observability.yml").read_text(),
        (ROOT / ".env.example").read_text(),
    )
    source = "\n".join(source_files)

    assert "uams:" not in source.casefold()
    assert "AUTOSWE_UAMS_URL" in source
    assert "AUTOSWE_UAMS_TOKEN" in source
    assert not re.search(r"lsv2_[A-Za-z0-9_]+", source)
    for line in source.splitlines():
        credential = re.match(
            r"(?i)^\s*[A-Z0-9_]*(api[_-]?key|admin[_-]?token|uams[_-]?token)\s*[:=]\s*(.*)$",
            line,
        )
        if credential is not None:
            value = credential.group(2).strip()
            assert not value or value.startswith("${"), line
    assert "LANGCHAIN_API_KEY=" not in source
    assert "OPENAI_API_KEY=" not in source
    assert "AUTOSWE_UAMS_TOKEN=compose-uams-token" not in source
    assert "uams" not in compose["services"]


def test_dependencies_are_health_gated_and_only_edge_ports_are_published(
    compose: dict[str, Any],
) -> None:
    services = compose["services"]

    assert set(services["api"]["depends_on"]) >= {"postgres", "redis", "sandbox-manager"}
    assert services["api"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["workers"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    published = {name for name, service in services.items() if service.get("ports")}
    assert published == {"api", "web"}


def test_redis_can_restart_with_private_appendonly_files(compose: dict[str, Any]) -> None:
    import docker

    client = docker.from_env()
    service = compose["services"]["redis"]
    volume = client.volumes.create()
    container = client.containers.create(
        service["image"],
        command=["redis-server", "--appendonly", "yes", "--save", ""],
        volumes={volume.name: {"bind": "/data", "mode": "rw"}},
        cap_drop=service["cap_drop"],
        cap_add=service.get("cap_add", []),
        security_opt=service["security_opt"],
        network_mode="none",
    )

    def wait_for_redis() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            container.reload()
            assert container.status == "running", container.logs().decode()
            try:
                result = container.exec_run(["redis-cli", "ping"])
            except docker.errors.APIError:
                pytest.fail(container.logs().decode())
            if result.exit_code == 0 and result.output.strip() == b"PONG":
                return
            time.sleep(0.1)
        pytest.fail("Redis did not become ready")

    try:
        container.start()
        wait_for_redis()
        assert container.exec_run(["redis-cli", "set", "restart-proof", "retained"]).exit_code == 0
        container.restart(timeout=5)
        wait_for_redis()
        assert (
            container.exec_run(["redis-cli", "get", "restart-proof"]).output.strip() == b"retained"
        )
    finally:
        container.remove(force=True)
        volume.remove()
        client.close()
