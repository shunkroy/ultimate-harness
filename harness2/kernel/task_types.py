"""Explicit typed task contracts and language-neutral resource declarations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .payloads import PayloadError, TaskPayload, canonical_bytes


class UnknownTaskType(PayloadError):
    pass


class SideEffectClass(str, Enum):
    PURE = "pure"
    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    NON_IDEMPOTENT_WRITE = "non_idempotent_write"


@dataclass(frozen=True)
class TaskResourceRequirements:
    cpu_class: str = "standard"
    memory_bytes: int = 0
    disk_bytes: int = 0
    network: str = "none"
    gpu: bool = False
    provider_quota: str | None = None
    model_context_tokens: int = 0
    external_services: tuple[str, ...] = ()
    wall_timeout_seconds: int = 300
    output_limit_bytes: int = 1024 * 1024
    sandbox_required: bool = False

    def __post_init__(self) -> None:
        if self.cpu_class not in {"minimal", "standard", "intensive"}:
            raise PayloadError("unknown cpu resource class")
        if self.network not in {"none", "loopback", "provider", "restricted"}:
            raise PayloadError("unknown network resource policy")
        for value in (
            self.memory_bytes, self.disk_bytes, self.model_context_tokens,
            self.wall_timeout_seconds, self.output_limit_bytes,
        ):
            if not isinstance(value, int) or value < 0:
                raise PayloadError("resource values must be nonnegative integers")

    def as_dict(self) -> dict[str, Any]:
        return {
            "cpu_class": self.cpu_class,
            "memory_bytes": self.memory_bytes,
            "disk_bytes": self.disk_bytes,
            "network": self.network,
            "gpu": self.gpu,
            "provider_quota": self.provider_quota,
            "model_context_tokens": self.model_context_tokens,
            "external_services": list(self.external_services),
            "wall_timeout_seconds": self.wall_timeout_seconds,
            "output_limit_bytes": self.output_limit_bytes,
            "sandbox_required": self.sandbox_required,
        }


@dataclass(frozen=True)
class TaskTypeDescriptor:
    name: str
    version: int
    payload_schema: str
    output_schema: str
    required_capabilities: tuple[str, ...] = ()
    resources: TaskResourceRequirements = field(default_factory=TaskResourceRequirements)
    max_attempts: int = 1
    cancellation: str = "cooperative"
    resumable: bool = False
    side_effect_class: SideEffectClass = SideEffectClass.PURE

    def __post_init__(self) -> None:
        if not self.name or self.version < 1 or self.max_attempts < 1:
            raise PayloadError("invalid task type descriptor")
        if self.cancellation not in {"none", "cooperative", "immediate_before_effect"}:
            raise PayloadError("invalid cancellation semantics")

    @property
    def task_type(self) -> str:
        return f"{self.name}/v{self.version}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "payload_schema": self.payload_schema,
            "output_schema": self.output_schema,
            "required_capabilities": list(self.required_capabilities),
            "resources": self.resources.as_dict(),
            "max_attempts": self.max_attempts,
            "cancellation": self.cancellation,
            "resumable": self.resumable,
            "side_effect_class": self.side_effect_class.value,
        }

    @property
    def descriptor_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self.as_dict(), max_bytes=64 * 1024)).hexdigest()

    def validate(self, payload: TaskPayload) -> None:
        if payload.task_type != self.task_type or payload.schema_id != self.payload_schema:
            raise PayloadError("task payload does not match registered descriptor")
        if not set(self.required_capabilities).issubset(
            set(payload.constraints.get("required_capabilities", self.required_capabilities))
        ):
            raise PayloadError("task payload omits descriptor capabilities")


class TaskTypeRegistry:
    def __init__(self, descriptors=()):
        self._values: dict[str, TaskTypeDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: TaskTypeDescriptor) -> None:
        if descriptor.task_type in self._values:
            raise PayloadError(f"task type already registered: {descriptor.task_type}")
        self._values[descriptor.task_type] = descriptor

    def require(self, task_type: str) -> TaskTypeDescriptor:
        value = self._values.get(task_type)
        if value is None:
            raise UnknownTaskType(f"unknown task type: {task_type}")
        return value

    def all(self) -> tuple[TaskTypeDescriptor, ...]:
        return tuple(self._values[key] for key in sorted(self._values))


def default_task_types() -> TaskTypeRegistry:
    payload_schema = "harness.task-payload/1"
    return TaskTypeRegistry((
        TaskTypeDescriptor(
            "execution.generic", 1, payload_schema, "harness.execution-outcome/1",
            resources=TaskResourceRequirements(network="provider"), max_attempts=1,
            side_effect_class=SideEffectClass.NON_IDEMPOTENT_WRITE,
        ),
        TaskTypeDescriptor(
            "legacy.run", 1, payload_schema, "harness.legacy-run-result/1",
            resources=TaskResourceRequirements(network="provider"), max_attempts=3,
            side_effect_class=SideEffectClass.NON_IDEMPOTENT_WRITE,
        ),
        TaskTypeDescriptor(
            "context.compile", 1, payload_schema, "harness.context-package-ref/1",
            required_capabilities=("context.compile.text",),
            resources=TaskResourceRequirements(
                memory_bytes=128 * 1024**2, disk_bytes=32 * 1024**2,
                network="none", wall_timeout_seconds=120,
            ), max_attempts=2, resumable=False, side_effect_class=SideEffectClass.PURE,
        ),
        TaskTypeDescriptor(
            "skill.execute", 1, payload_schema, "harness.skill-result/1",
            resources=TaskResourceRequirements(
                network="none", sandbox_required=True, output_limit_bytes=1024 * 1024,
            ), max_attempts=1, resumable=True,
            side_effect_class=SideEffectClass.NON_IDEMPOTENT_WRITE,
        ),
    ))
