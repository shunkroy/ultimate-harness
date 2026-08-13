"""Provider-neutral sandbox capability, request and denial contracts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from ..kernel.payloads import PayloadReference
from ..kernel.task_types import TaskTypeDescriptor


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class SandboxError(RuntimeError):
    pass


class SandboxUnavailable(SandboxError):
    pass


class IsolationLevel(str, Enum):
    NONE = "none"
    TEST_PROCESS = "test_process"
    OS_SANDBOX = "os_sandbox"


@dataclass(frozen=True)
class SandboxCapabilities:
    backend_id: str
    isolation_level: IsolationLevel
    filesystem_containment: bool = False
    network_denied: bool = False
    credential_isolation: bool = False
    process_containment: bool = False
    syscall_restriction: bool = False
    resource_limits_enforced: bool = False
    supported_platforms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.backend_id.strip():
            raise SandboxError("sandbox backend_id is required")


@dataclass(frozen=True)
class SandboxRequest:
    execution_id: str
    task_id: str
    attempt_id: str
    fence_token: int
    manifest_hash: str
    package_reference: PayloadReference
    entrypoint: str
    input_reference: PayloadReference
    timeout_seconds: int
    output_limit_bytes: int
    network_policy: str = "none"
    environment_allowlist: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.fence_token < 1:
            raise SandboxError("invalid sandbox request schema/fence")
        for value, name in (
            (self.execution_id, "execution_id"), (self.task_id, "task_id"),
            (self.attempt_id, "attempt_id"), (self.entrypoint, "entrypoint"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise SandboxError(f"{name} is required")
        if not _HEX64.fullmatch(self.manifest_hash):
            raise SandboxError("manifest_hash must be lowercase SHA-256")
        if self.timeout_seconds < 1 or self.output_limit_bytes < 1:
            raise SandboxError("sandbox bounds must be positive")
        if self.network_policy != "none":
            raise SandboxError("Checkpoint 3B skill execution denies network")


@dataclass(frozen=True)
class SandboxResult:
    execution_id: str
    task_id: str
    attempt_id: str
    fence_token: int
    manifest_hash: str
    backend_id: str
    status: str
    exit_code: int | None
    output: bytes
    output_sha256: str
    started_at: float
    finished_at: float
    isolation_level: IsolationLevel
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {
            "succeeded", "failed", "timed_out", "output_limit",
            "policy_denied", "backend_unavailable",
        }:
            raise SandboxError("unknown sandbox result status")
        if (
            not isinstance(self.output, bytes)
            or not _HEX64.fullmatch(self.output_sha256)
            or hashlib.sha256(self.output).hexdigest() != self.output_sha256
        ):
            raise SandboxError("invalid sandbox result output identity")
        if not self.task_id.strip() or not self.attempt_id.strip() or self.fence_token < 1:
            raise SandboxError("sandbox result lacks fenced request identity")
        if not _HEX64.fullmatch(self.manifest_hash):
            raise SandboxError("sandbox result manifest identity is invalid")
        if self.finished_at < self.started_at:
            raise SandboxError("sandbox result time moved backwards")


class SandboxBackend(Protocol):
    backend_id: str

    def probe(self) -> SandboxCapabilities: ...

    def execute(self, request: SandboxRequest) -> SandboxResult: ...


@dataclass(frozen=True)
class SandboxDecision:
    allowed: bool
    reason_code: str
    capabilities: SandboxCapabilities


class SandboxPolicy:
    """Require independently enforced OS isolation for production skill tasks."""

    @staticmethod
    def authorize(
        descriptor: TaskTypeDescriptor, capabilities: SandboxCapabilities, *,
        production: bool = True,
    ) -> SandboxDecision:
        if not descriptor.resources.sandbox_required:
            return SandboxDecision(True, "sandbox_not_required", capabilities)
        if production and capabilities.isolation_level != IsolationLevel.OS_SANDBOX:
            return SandboxDecision(False, "sandbox_os_isolation_required", capabilities)
        required = (
            capabilities.filesystem_containment,
            capabilities.network_denied,
            capabilities.credential_isolation,
            capabilities.process_containment,
            capabilities.resource_limits_enforced,
        )
        if not all(required):
            return SandboxDecision(False, "sandbox_capability_insufficient", capabilities)
        return SandboxDecision(True, "sandbox_authorized", capabilities)


class DisabledSandboxBackend:
    backend_id = "disabled"

    def probe(self) -> SandboxCapabilities:
        return SandboxCapabilities(self.backend_id, IsolationLevel.NONE)

    def execute(self, request: SandboxRequest) -> SandboxResult:
        raise SandboxUnavailable("sandbox_unavailable")
