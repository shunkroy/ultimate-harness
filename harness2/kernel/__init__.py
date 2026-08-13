"""Provider-independent Harness Kernel contracts and registries."""

from .contracts import (
    CapabilityDescriptor,
    CapabilityEvidence,
    EvidenceKind,
    ExecutionEvent,
    ExecutionOutcome,
    ExecutionPlan,
    ExecutionRequest,
    Maturity,
    RuntimeDescriptor,
)
from .application import ApplicationKernel, KernelExecutionError, KernelExecutionResult
from .event_bus import EventBus, EventConflict, EventValidationError, TypedEvent
from .migrations import MigrationDriftError, Migrator, SchemaTooNewError
from .tasks import (
    ALLOWED_TRANSITIONS,
    AttemptLease,
    InvalidTransition,
    StaleLeaseError,
    TaskIdempotencyConflict,
    TaskRecord,
    TaskRepository,
    TaskState,
)
from .provider_intelligence import CapabilityScore, ProviderIntelligence, ProviderObservation
from .resources import ResourceAction, ResourceDecision, ResourceGovernor, ResourceLimits, ResourceObservation
from .payloads import (
    AuthenticatedStorage,
    CheckpointReference,
    PayloadError,
    PayloadIntegrityError,
    PayloadReference,
    TaskPayload,
)
from .task_types import (
    SideEffectClass,
    TaskResourceRequirements,
    TaskTypeDescriptor,
    TaskTypeRegistry,
    UnknownTaskType,
    default_task_types,
)
from .execution_state import ExecutionStateRepository, SourceSnapshot
from .registry import CapabilityRegistry, RuntimeRegistry
from .catalog import build_catalog, runtime_from_engine

__all__ = [
    "ALLOWED_TRANSITIONS", "ApplicationKernel", "AttemptLease",
    "CapabilityDescriptor", "CapabilityEvidence", "CapabilityRegistry",
    "EventBus", "EventConflict", "EventValidationError", "EvidenceKind",
    "ExecutionEvent", "ExecutionOutcome", "ExecutionPlan", "ExecutionRequest",
    "InvalidTransition", "KernelExecutionError", "KernelExecutionResult",
    "MigrationDriftError", "Migrator", "Maturity", "RuntimeDescriptor",
    "CapabilityScore", "ProviderIntelligence", "ProviderObservation",
    "ResourceAction", "ResourceDecision", "ResourceGovernor", "ResourceLimits",
    "ResourceObservation",
    "AuthenticatedStorage", "CheckpointReference", "PayloadError",
    "PayloadIntegrityError", "PayloadReference", "TaskPayload",
    "SideEffectClass", "TaskResourceRequirements", "TaskTypeDescriptor",
    "TaskTypeRegistry", "UnknownTaskType", "default_task_types",
    "ExecutionStateRepository", "SourceSnapshot",
    "RuntimeRegistry", "SchemaTooNewError", "StaleLeaseError",
    "TaskIdempotencyConflict", "TaskRecord", "TaskRepository", "TaskState",
    "TypedEvent",
    "build_catalog", "runtime_from_engine",
]
