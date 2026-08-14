"""Authoritative provider-neutral planning and execution boundary."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Mapping, Protocol

from .contracts import ExecutionOutcome, ExecutionPlan, ExecutionRequest, Health, Maturity
from .registry import CapabilityRegistry, RuntimeDriver, RuntimeRegistry
from .tasks import AttemptLease, TaskRecord, TaskRepository, TaskState


class KernelExecutionError(RuntimeError):
    pass


class Planner(Protocol):
    def plan(self, request: ExecutionRequest, runtimes: RuntimeRegistry) -> ExecutionPlan: ...


@dataclass(frozen=True)
class KernelExecutionResult:
    task: TaskRecord
    plan: ExecutionPlan | None
    outcome: ExecutionOutcome | None
    lease: AttemptLease | None
    replayed: bool = False


def outcome_hash(outcome: ExecutionOutcome) -> str:
    # Provider output is intentionally excluded from persistent task/event state.
    payload = json.dumps({
        "task_id": outcome.task_id,
        "plan_id": outcome.plan_id,
        "success": bool(outcome.success),
        "error_code": outcome.error_code,
        "evidence": [dict(item) for item in outcome.evidence],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ApplicationKernel:
    def __init__(
        self, tasks: TaskRepository, runtimes: RuntimeRegistry,
        capabilities: CapabilityRegistry, drivers: Mapping[str, RuntimeDriver],
        planner: Planner,
    ):
        self.tasks = tasks
        self.runtimes = runtimes
        self.capabilities = capabilities
        self.drivers = dict(drivers)
        self.planner = planner

    def _validate_plan(self, request: ExecutionRequest, plan: ExecutionPlan) -> None:
        if plan.task_id != request.task_id:
            raise KernelExecutionError("plan task identity mismatch")
        runtime = self.runtimes.get(plan.runtime_id)
        if runtime is None:
            raise KernelExecutionError(f"runtime is not registered: {plan.runtime_id}")
        if not runtime.enabled or runtime.health in {Health.DOWN, Health.DISABLED}:
            raise KernelExecutionError(f"runtime is not available: {plan.runtime_id}")
        required = set(request.required_capabilities)
        if not required.issubset(set(runtime.capabilities)):
            missing = ", ".join(sorted(required - set(runtime.capabilities)))
            raise KernelExecutionError(f"runtime lacks required capabilities: {missing}")
        if not required.issubset(set(plan.capabilities)):
            missing = ", ".join(sorted(required - set(plan.capabilities)))
            raise KernelExecutionError(f"plan omits required capabilities: {missing}")
        if not set(plan.capabilities).issubset(set(runtime.capabilities)):
            raise KernelExecutionError("plan declares capabilities absent from runtime")
        if request.preferred_runtime and request.preferred_runtime != plan.runtime_id:
            raise KernelExecutionError("plan violates explicit preferred runtime")
        for capability_id in required:
            capability = self.capabilities.get(capability_id)
            if capability is None or plan.runtime_id not in capability.providers:
                raise KernelExecutionError(f"capability is not evidenced for runtime: {capability_id}")
            if capability.verification_required and (
                capability.maturity not in {Maturity.TESTED, Maturity.BENCHMARKED, Maturity.STABLE}
                or not capability.evidence
            ):
                raise KernelExecutionError(f"capability lacks test evidence: {capability_id}")
        if plan.runtime_id not in self.drivers:
            raise KernelExecutionError(f"runtime driver is unavailable: {plan.runtime_id}")

    def execute(
        self, request: ExecutionRequest, *, idempotency_key: str,
        owner_id: str, max_attempts: int = 1, lease_seconds: int = 600,
        source: str = "user", reason: str = "explicit execution",
        authority: str = "user", priority: int = 0,
    ) -> KernelExecutionResult:
        task = self.tasks.submit(
            request, source=source, reason=reason, authority=authority,
            priority=priority, idempotency_key=idempotency_key,
            max_attempts=max_attempts,
        )
        if task.state in {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}:
            return KernelExecutionResult(task, None, None, None, replayed=True)
        task = self.tasks.prepare(task.task_id)
        try:
            plan = self.planner.plan(request, self.runtimes)
            self._validate_plan(request, plan)
        except Exception as exc:
            task = self.tasks.transition(
                task.task_id, TaskState.BLOCKED,
                reason_code="planning_or_policy_failure",
            )
            raise KernelExecutionError("task planning or policy validation failed") from exc
        lease = self.tasks.claim(
            task.task_id, plan, owner_id=owner_id, lease_seconds=lease_seconds,
        )
        try:
            outcome = self.drivers[plan.runtime_id].execute(request, plan)
            if outcome.task_id != request.task_id or outcome.plan_id != plan.plan_id:
                raise KernelExecutionError("runtime outcome identity mismatch")
        except Exception:
            outcome = ExecutionOutcome(
                task_id=request.task_id, plan_id=plan.plan_id,
                success=False, error_code="driver_exception",
            )
        digest = outcome_hash(outcome)
        task = self.tasks.complete(
            lease, success=outcome.success, outcome_hash=digest,
            error_code=outcome.error_code,
        )
        return KernelExecutionResult(task, plan, outcome, lease)
