"""Language-neutral immutable payload and checkpoint reference contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol


MAX_PAYLOAD_BYTES = 10 * 1024 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$")


class PayloadError(ValueError):
    pass


class PayloadIntegrityError(PayloadError):
    pass


class PayloadUnavailable(PayloadError):
    pass


def _validate_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 32:
        raise PayloadError("payload nesting exceeds 32 levels")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PayloadError("payload numbers must be finite")
        return value
    if isinstance(value, (list, tuple)):
        return [_validate_json(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise PayloadError("payload keys must be bounded non-empty strings")
            result[key] = _validate_json(item, depth=depth + 1)
        return result
    raise PayloadError(f"unsupported payload value type: {type(value).__name__}")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def canonical_bytes(value: Mapping[str, Any], *, max_bytes: int = MAX_PAYLOAD_BYTES) -> bytes:
    if not isinstance(value, Mapping):
        raise PayloadError("canonical payload root must be an object")
    normalized = _validate_json(value)
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        raise PayloadError(f"payload exceeds {max_bytes} bytes")
    return encoded


def content_identity(*, schema_id: str, purpose: str, plaintext: bytes) -> str:
    if not _ID.fullmatch(schema_id) or not _ID.fullmatch(purpose):
        raise PayloadError("invalid schema or purpose identity")
    framed = (
        b"harness.payload/v1\0" + schema_id.encode() + b"\0"
        + purpose.encode() + b"\0" + plaintext
    )
    return hashlib.sha256(framed).hexdigest()


@dataclass(frozen=True)
class TaskPayload:
    task_type: str
    objective: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    constraints: Mapping[str, Any] = field(default_factory=dict)
    budget: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.task_type) or self.schema_version != 1:
            raise PayloadError("unsupported task payload type/schema")
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise PayloadError("task objective must be non-empty text")
        if len(self.objective.encode("utf-8")) > 1024 * 1024:
            raise PayloadError("task objective exceeds 1 MiB")
        # Deep-copy through validated JSON to sever caller mutation.
        object.__setattr__(self, "inputs", _freeze(_validate_json(copy.deepcopy(dict(self.inputs)))))
        object.__setattr__(self, "constraints", _freeze(_validate_json(copy.deepcopy(dict(self.constraints)))))
        object.__setattr__(self, "budget", _freeze(_validate_json(copy.deepcopy(dict(self.budget)))))

    @property
    def schema_id(self) -> str:
        return f"harness.task-payload/{self.schema_version}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema_id,
            "task_type": self.task_type,
            "objective": self.objective,
            "inputs": _thaw(self.inputs),
            "constraints": _thaw(self.constraints),
            "budget": _thaw(self.budget),
        }

    @property
    def canonical(self) -> bytes:
        return canonical_bytes(self.as_dict())

    @property
    def payload_id(self) -> str:
        return "payload-" + content_identity(
            schema_id=self.schema_id, purpose="task.input", plaintext=self.canonical,
        )

    @classmethod
    def from_bytes(cls, value: bytes) -> "TaskPayload":
        try:
            raw = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PayloadError("task payload is not canonical UTF-8 JSON") from exc
        if not isinstance(raw, dict) or raw.get("schema") != "harness.task-payload/1":
            raise PayloadError("unsupported task payload schema")
        expected = {"schema", "task_type", "objective", "inputs", "constraints", "budget"}
        if set(raw) != expected:
            raise PayloadError("task payload fields do not match schema")
        result = cls(
            str(raw["task_type"]), str(raw["objective"]),
            raw["inputs"], raw["constraints"], raw["budget"], 1,
        )
        if result.canonical != value:
            raise PayloadError("task payload is not in canonical form")
        return result


@dataclass(frozen=True)
class PayloadReference:
    reference_id: str
    backend_id: str
    object_key: str
    content_sha256: str
    size_bytes: int
    media_type: str
    schema_id: str
    purpose: str
    envelope_version: int
    key_id: str
    reference_mac: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.reference_id, "reference_id"), (self.backend_id, "backend_id"),
            (self.object_key, "object_key"), (self.media_type, "media_type"),
            (self.schema_id, "schema_id"), (self.purpose, "purpose"),
            (self.key_id, "key_id"),
        ):
            if not isinstance(value, str) or not value or len(value) > 256:
                raise PayloadError(f"invalid {name}")
        for value, name in ((self.content_sha256, "content_sha256"), (self.reference_mac, "reference_mac")):
            if not _HEX64.fullmatch(value):
                raise PayloadError(f"{name} must be lowercase SHA-256")
        if self.size_bytes < 0 or self.envelope_version < 1:
            raise PayloadError("invalid payload reference bounds")

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class CheckpointReference:
    checkpoint_id: str
    task_id: str
    attempt_id: str
    fence_token: int
    sequence: int
    payload: PayloadReference
    workflow_step: str
    resource_versions: Mapping[str, str]
    parent_checkpoint_hash: str
    integrity_hash: str
    created_at: float
    resumable: bool
    schema_version: int = 1


class AuthenticatedStorage(Protocol):
    backend_id: str

    def put(
        self, data: bytes, *, schema_id: str, purpose: str,
        binding: Mapping[str, Any], media_type: str = "application/json",
    ) -> PayloadReference: ...

    def get(self, reference: PayloadReference, *, binding: Mapping[str, Any]) -> bytes: ...

    def verify(self, reference: PayloadReference, *, binding: Mapping[str, Any]) -> bool: ...
