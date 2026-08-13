from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, Field, model_validator

from domain.models import ContractModel

PinnedImage = Annotated[
    str,
    Field(pattern=r"^[^\s@:]+(?:[/:][^\s@]+)*@sha256:[0-9a-f]{64}$", max_length=500),
]


class EgressPolicy(StrEnum):
    NONE = "none"
    DEPENDENCY_PROXY = "dependency_proxy"


class SandboxPolicy(ContractModel):
    """Closed container policy; unsafe Docker flags have no permitted value."""

    schema_version: Literal["1.0"] = "1.0"
    image: PinnedImage
    uid: int = Field(ge=1, le=2_147_483_647)
    gid: int = Field(ge=1, le=2_147_483_647)
    cpu_nanos: int = Field(ge=10_000_000, le=256_000_000_000)
    cpu_time_limit_ms: int = Field(ge=1, le=604_800_000)
    memory_bytes: int = Field(ge=16 * 1024 * 1024, le=1_099_511_627_776)
    pids_limit: int = Field(ge=1, le=32_768)
    timeout_seconds: int = Field(ge=1, le=604_800)
    max_stdout_bytes: int = Field(ge=1, le=1_073_741_824)
    max_stderr_bytes: int = Field(ge=1, le=1_073_741_824)
    max_total_output_bytes: int = Field(ge=1, le=2_147_483_648)
    egress: EgressPolicy = EgressPolicy.NONE
    dependency_proxy_url: AnyHttpUrl | None = None
    dependency_proxy_network: str | None = None
    privileged: Literal[False] = False
    host_network: Literal[False] = False
    rootfs_read_only: Literal[True] = True
    drop_all_capabilities: Literal[True] = True
    no_new_privileges: Literal[True] = True
    devices: tuple[str, ...] = Field(default=(), max_length=0)
    additional_mounts: tuple[str, ...] = Field(default=(), max_length=0)
    capabilities: tuple[str, ...] = Field(default=(), max_length=0)

    @model_validator(mode="after")
    def closed_boundaries_are_consistent(self) -> Self:
        if self.max_total_output_bytes > self.max_stdout_bytes + self.max_stderr_bytes:
            raise ValueError("total output limit cannot exceed the sum of stream limits")
        if self.egress is EgressPolicy.NONE:
            if self.dependency_proxy_url is not None or self.dependency_proxy_network is not None:
                raise ValueError("dependency proxy settings require dependency_proxy egress")
            return self
        if self.dependency_proxy_url is None or self.dependency_proxy_network is None:
            raise ValueError("dependency proxy URL and network are required")
        if self.dependency_proxy_url.host != "autoswe-dependency-proxy":
            raise ValueError("dependency proxy must use the fixed internal service")
        if self.dependency_proxy_network != "autoswe-dependency-egress":
            raise ValueError("dependency proxy must use the fixed internal network")
        return self

