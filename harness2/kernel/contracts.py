"""Language-neutral domain contracts for the provider-independent kernel."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple


class Maturity(str, Enum):
    IDEA = "idea"
    DESIGNED = "designed"
    PROTOTYPED = "prototyped"
    IMPLEMENTED = "implemented"
    TESTED = "tested"
    BENCHMARKED = "benchmarked"
    STABLE = "stable"
    DEPRECATED = "deprecated"


class EvidenceKind(str, Enum):
    USER_PROVIDED = "user_provided"
    LOCAL_OBSERVATION = "local_observation"
    TEST_VERIFIED = "test_verified"
    DOCUMENTATION = "documentation"
    WEB_RESEARCH = "web_research"
    MODEL_INFERENCE = "model_inference"
    SPECULATION = "speculation"


class Health(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    DISABLED = "disabled"


class ContractError(ValueError):
    pass


def _nonempty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field_name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class CapabilityEvidence:
    kind: EvidenceKind
    reference: str
    observed_at: str
    detail: str = ""
    expires_at: Optional[str] = None

    def __post_init__(self) -> None:
        _nonempty(self.reference, "reference")
        _nonempty(self.observed_at, "observed_at")

    def as_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["kind"] = self.kind.value
        return value


@dataclass(frozen=True)
class CapabilityDescriptor:
    id: str
    description: str
    maturity: Maturity
    providers: Tuple[str, ...]
    evidence: Tuple[CapabilityEvidence, ...] = ()
    verification_required: bool = True
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _nonempty(self.id, "capability id")
        _nonempty(self.description, "capability description")
        if not self.providers:
            raise ContractError("a capability must declare at least one provider")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "maturity": self.maturity.value,
            "providers": list(self.providers),
            "evidence": [item.as_dict() for item in self.evidence],
            "verification_required": self.verification_required,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
        }


@dataclass(frozen=True)
class RuntimeDescriptor:
    id: str
    kind: str
    display_name: str
    version: Optional[str]
    location: Optional[str]
    interface: str
    input_mode: str
    output_mode: str
    capabilities: Tuple[str, ...]
    auth_names: Tuple[str, ...] = ()
    limitations: Tuple[str, ...] = ()
    maturity: Maturity = Maturity.IMPLEMENTED
    enabled: bool = True
    health: Health = Health.UNKNOWN
    evidence: Tuple[CapabilityEvidence, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.id, "runtime id")
        _nonempty(self.kind, "runtime kind")
        _nonempty(self.interface, "runtime interface")

    def as_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["maturity"] = self.maturity.value
        value["health"] = self.health.value
        value["capabilities"] = list(self.capabilities)
        value["auth_names"] = list(self.auth_names)
        value["limitations"] = list(self.limitations)
        value["evidence"] = [item.as_dict() for item in self.evidence]
        return value


@dataclass(frozen=True)
class ExecutionRequest:
    task_id: str
    objective: str
    required_capabilities: Tuple[str, ...] = ()
    preferred_runtime: Optional[str] = None
    inputs: Mapping[str, Any] = field(default_factory=dict)
    constraints: Mapping[str, Any] = field(default_factory=dict)
    budget: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    task_id: str
    runtime_id: str
    capabilities: Tuple[str, ...]
    steps: Tuple[str, ...]
    verification: Tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ExecutionEvent:
    event_id: str
    task_id: str
    event: str
    occurred_at: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionOutcome:
    task_id: str
    plan_id: str
    success: bool
    output: Any = None
    error_code: Optional[str] = None
    evidence: Tuple[Mapping[str, Any], ...] = ()
    events: Tuple[ExecutionEvent, ...] = ()
