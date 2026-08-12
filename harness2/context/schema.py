"""ContextIR and ContextPackage language-neutral schema objects."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple


CONTEXT_SCHEMA = "harness.context/v1"
IR_SCHEMA = "harness.context-ir/v1"


@dataclass(frozen=True)
class SourceRef:
    id: str
    path: str
    sha256: str
    media_type: str = "text/plain"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SemanticUnit:
    id: str
    kind: str
    text: str
    source_id: str
    line_start: int
    line_end: int
    confidence: float = 1.0
    uncertainty: str = "certain"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OperationContract:
    name: str
    operation_class: str
    inputs: Mapping[str, str]
    output: str
    implementation: str
    pure: bool = True
    permissions: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["inputs"] = dict(self.inputs)
        value["permissions"] = list(self.permissions)
        return value


@dataclass(frozen=True)
class ContextIR:
    context_id: str
    name: str
    version: str
    source: SourceRef
    concepts: Tuple[SemanticUnit, ...]
    rules: Tuple[SemanticUnit, ...]
    procedures: Tuple[SemanticUnit, ...]
    examples: Tuple[SemanticUnit, ...]
    operations: Tuple[OperationContract, ...]
    contradictions: Tuple[Mapping[str, Any], ...] = ()
    permissions: Tuple[str, ...] = ()
    state_schema: Mapping[str, Any] = field(default_factory=dict)
    schema: str = IR_SCHEMA

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "context_id": self.context_id,
            "name": self.name,
            "version": self.version,
            "source": self.source.as_dict(),
            "concepts": [item.as_dict() for item in self.concepts],
            "rules": [item.as_dict() for item in self.rules],
            "procedures": [item.as_dict() for item in self.procedures],
            "examples": [item.as_dict() for item in self.examples],
            "operations": [item.as_dict() for item in self.operations],
            "contradictions": [dict(item) for item in self.contradictions],
            "permissions": list(self.permissions),
            "state_schema": dict(self.state_schema),
        }


@dataclass(frozen=True)
class ContextExecutionResult:
    execution_id: str
    context_id: str
    context_version: str
    operation: str
    success: bool
    backend: str
    output: Any = None
    error_code: Optional[str] = None
    evidence: Tuple[Mapping[str, Any], ...] = ()
    validated: bool = False
    trace: Tuple[Mapping[str, Any], ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "context_id": self.context_id,
            "context_version": self.context_version,
            "operation": self.operation,
            "success": self.success,
            "backend": self.backend,
            "output": self.output,
            "error_code": self.error_code,
            "evidence": [dict(item) for item in self.evidence],
            "validated": self.validated,
            "trace": [dict(item) for item in self.trace],
        }
