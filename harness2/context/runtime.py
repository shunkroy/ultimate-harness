"""Deterministic-first ContextRuntime for the v1 executable-context slice."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Tuple

from .package import ContextPackage
from .schema import ContextExecutionResult, OperationContract, SemanticUnit


class ContextRuntimeError(ValueError):
    pass


_TRANSFORMS = {
    "upper": str.upper,
    "lower": str.lower,
    "title": str.title,
    "compress_whitespace": lambda text: re.sub(r"\s+", " ", text).strip(),
    "deduplicate_lines": lambda text: "\n".join(dict.fromkeys(line for line in text.splitlines() if line.strip())),
}


def _trace(event: str, **data: Any) -> Dict[str, Any]:
    return {"event": event, "at": datetime.now(timezone.utc).isoformat(), "data": data}


class ContextRuntime:
    def __init__(self):
        self._packages: Dict[str, ContextPackage] = {}

    def load(self, path: str) -> ContextPackage:
        package = ContextPackage.load(path)
        self._packages[package.ir.context_id] = package
        return package

    def inspect(self, context_id: str) -> Mapping[str, Any]:
        package = self._require(context_id)
        return {
            "context_id": package.ir.context_id,
            "name": package.ir.name,
            "version": package.ir.version,
            "pure": package.manifest["pure"],
            "operations": [item.as_dict() for item in package.ir.operations],
            "source": package.ir.source.as_dict(),
        }

    def execute(self, context_id: str, operation: str, inputs: Mapping[str, Any]) -> ContextExecutionResult:
        package = self._require(context_id)
        contract = next((item for item in package.ir.operations if item.name == operation), None)
        execution_id = uuid.uuid4().hex
        trace = [_trace("context.loaded", context_id=context_id, version=package.ir.version)]
        if contract is None:
            return ContextExecutionResult(
                execution_id, context_id, package.ir.version, operation, False,
                "none", error_code="operation_not_found", validated=False, trace=tuple(trace),
            )
        try:
            normalized = self._validate_inputs(contract, inputs)
        except ContextRuntimeError as exc:
            trace.append(_trace("input.invalid", detail=str(exc)))
            return ContextExecutionResult(
                execution_id, context_id, package.ir.version, operation, False,
                contract.implementation, error_code="invalid_input", validated=False,
                trace=tuple(trace),
            )
        trace.append(_trace("input.validated", fields=sorted(normalized)))
        if operation == "query":
            output, evidence = self._query(package, normalized["topic"])
            success, error = True, None
        elif operation == "transform":
            mode = normalized["mode"]
            fn = _TRANSFORMS.get(mode)
            if fn is None:
                output, evidence, success, error = None, (), False, "unsupported_transform"
            else:
                output, evidence, success, error = fn(normalized["text"]), (), True, None
        elif operation == "generate":
            matches, evidence = self._query(package, normalized["topic"])
            if not evidence:
                output, success, error = None, False, "insufficient_evidence"
            else:
                output = {
                    "topic": normalized["topic"],
                    "summary": " ".join(item["text"] for item in matches[:5]),
                    "claims": [item["text"] for item in matches[:5]],
                    "provenance_required": True,
                }
                success, error = True, None
        else:
            output, evidence, success, error = None, (), False, "unsupported_operation"
        trace.append(_trace("operation.executed", backend=contract.implementation, success=success))
        validated = bool(success and (operation != "generate" or evidence))
        trace.append(_trace("output.validated", validated=validated))
        return ContextExecutionResult(
            execution_id, context_id, package.ir.version, operation, success,
            contract.implementation, output, error, tuple(evidence), validated,
            tuple(trace),
        )

    def _require(self, context_id: str) -> ContextPackage:
        package = self._packages.get(context_id)
        if package is None:
            raise ContextRuntimeError(f"context is not loaded: {context_id}")
        return package

    @staticmethod
    def _validate_inputs(contract: OperationContract, values: Mapping[str, Any]) -> Dict[str, Any]:
        if set(values) != set(contract.inputs):
            raise ContextRuntimeError("input fields do not match operation contract")
        output: Dict[str, Any] = {}
        for name, expected in contract.inputs.items():
            value = values[name]
            if expected == "string" and not isinstance(value, str):
                raise ContextRuntimeError(f"{name} must be a string")
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                raise ContextRuntimeError(f"{name} must be an integer")
            output[name] = value
        return output

    @staticmethod
    def _query(package: ContextPackage, topic: str) -> tuple[list[Dict[str, Any]], tuple[Dict[str, Any], ...]]:
        terms = [item for item in re.findall(r"[\w'-]+", topic.lower()) if len(item) > 1]
        units: tuple[SemanticUnit, ...] = (
            package.ir.concepts + package.ir.rules + package.ir.procedures + package.ir.examples
        )
        scored: list[tuple[int, SemanticUnit]] = []
        for unit in units:
            lower = unit.text.lower()
            score = sum(lower.count(term) for term in terms)
            if score:
                scored.append((score, unit))
        scored.sort(key=lambda item: (-item[0], item[1].line_start, item[1].id))
        matches: list[Dict[str, Any]] = []
        evidence: list[Dict[str, Any]] = []
        for score, unit in scored[:20]:
            value = {
                "unit_id": unit.id, "kind": unit.kind, "text": unit.text,
                "score": score, "confidence": unit.confidence,
                "uncertainty": unit.uncertainty,
            }
            matches.append(value)
            evidence.append({
                "source_id": unit.source_id,
                "source_sha256": package.ir.source.sha256,
                "line_start": unit.line_start,
                "line_end": unit.line_end,
                "unit_id": unit.id,
            })
        return matches, tuple(evidence)
