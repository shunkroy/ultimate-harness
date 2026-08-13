"""Trusted-data contracts for native skills; candidate code remains inert."""

from __future__ import annotations

import hashlib
import json
import math
import ntpath
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class SkillContractError(ValueError):
    pass


class SkillState(str, Enum):
    DISCOVERED = "discovered"
    DRAFT = "draft"
    SANDBOXED = "sandboxed"
    TESTED = "tested"
    BENCHMARKED = "benchmarked"
    APPROVED = "approved"
    ACTIVE = "active"
    DEGRADED = "degraded"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class SkillCapability:
    capability_id: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.capability_id.strip():
            raise SkillContractError("capability_id must be non-empty")


@dataclass(frozen=True)
class SkillEvidence:
    evidence_id: str
    kind: str
    artifact_hash: str
    observed_at: str
    detail: str = ""
    subject_hash: str = ""
    producer_id: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_id.strip() or not self.kind.strip() or not self.observed_at.strip():
            raise SkillContractError("evidence identity, kind and timestamp are required")
        if len(self.artifact_hash) != 64 or any(char not in "0123456789abcdef" for char in self.artifact_hash):
            raise SkillContractError("evidence artifact_hash must be lowercase SHA-256")
        if len(self.subject_hash) != 64 or any(char not in "0123456789abcdef" for char in self.subject_hash):
            raise SkillContractError("evidence subject_hash must bind to a manifest")
        if not self.producer_id.strip():
            raise SkillContractError("evidence producer_id is required")


@dataclass(frozen=True)
class SkillTest:
    test_id: str
    passed: bool
    artifact_hash: str
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if not self.test_id.strip() or self.duration_ms < 0:
            raise SkillContractError("valid test identity and duration required")
        if len(self.artifact_hash) != 64 or any(char not in "0123456789abcdef" for char in self.artifact_hash):
            raise SkillContractError("test artifact_hash must be lowercase SHA-256")


@dataclass(frozen=True)
class SkillManifest:
    skill_id: str
    version: str
    created_by: str
    source_hash: str
    capabilities: tuple[SkillCapability, ...]
    permissions: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    entrypoint: Optional[str] = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        for value, name in (
            (self.skill_id, "skill_id"), (self.version, "version"),
            (self.created_by, "created_by"), (self.source_hash, "source_hash"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise SkillContractError(f"{name} must be non-empty")
        if self.schema_version != 1:
            raise SkillContractError("unsupported skill manifest schema")
        if len(self.source_hash) != 64 or any(char not in "0123456789abcdef" for char in self.source_hash):
            raise SkillContractError("source_hash must be lowercase SHA-256")
        if any(permission.strip() for permission in self.permissions):
            raise SkillContractError("candidate skills cannot self-grant permissions")
        if self.entrypoint is not None:
            drive, _ = ntpath.splitdrive(self.entrypoint)
            if drive or self.entrypoint.startswith(("/", "\\")) or ".." in self.entrypoint.replace("\\", "/").split("/"):
                raise SkillContractError("skill entrypoint must be a contained relative path")

    def canonical_json(self) -> str:
        return json.dumps({
            "schema_version": self.schema_version,
            "skill_id": self.skill_id,
            "version": self.version,
            "created_by": self.created_by,
            "source_hash": self.source_hash,
            "capabilities": [
                {
                    "capability_id": item.capability_id,
                    "input_schema": dict(item.input_schema),
                    "output_schema": dict(item.output_schema),
                }
                for item in self.capabilities
            ],
            "permissions": list(self.permissions),
            "dependencies": list(self.dependencies),
            "entrypoint": self.entrypoint,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @property
    def manifest_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SkillDescriptor:
    manifest: SkillManifest
    state: SkillState
    evidence: tuple[SkillEvidence, ...] = ()
    tests: tuple[SkillTest, ...] = ()
    benchmark_score: Optional[float] = None
    activated: bool = False

    def __post_init__(self) -> None:
        if self.benchmark_score is not None and (
            not math.isfinite(self.benchmark_score) or not 0 <= self.benchmark_score <= 1
        ):
            raise SkillContractError("benchmark_score must be finite and between zero and one")
        if self.activated != (self.state == SkillState.ACTIVE):
            raise SkillContractError("activated must exactly match ACTIVE state")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise SkillContractError("duplicate evidence identity")


@dataclass(frozen=True)
class SkillPromotionDecision:
    allowed: bool
    from_state: SkillState
    to_state: SkillState
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...] = ()
