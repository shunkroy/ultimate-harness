"""Compatibility bridges at the application boundary, outside the kernel."""

from __future__ import annotations

import uuid
import random
import time
from dataclasses import replace

from .kernel.contracts import ExecutionOutcome, ExecutionPlan, ExecutionRequest
from .models import EngineResult, RoutingDecision, RunRequest


def _runtime_evidence(result: EngineResult, plan: ExecutionPlan, *, attempts: int | None = None):
    value = {
        "kind": "runtime_observation", "runtime_id": plan.runtime_id,
        "success": result.success,
        "duration_ms": int(result.duration * 1000),
    }
    fingerprint = result.metadata.get("execution_config_sha256")
    if (
        isinstance(fingerprint, str) and len(fingerprint) == 64
        and all(character in "0123456789abcdef" for character in fingerprint.lower())
    ):
        value["execution_config_sha256"] = fingerprint.lower()
    if attempts is not None:
        value["attempts"] = attempts
    return (value,)


def execution_request_from_legacy(request: RunRequest, *, task_id: str | None = None) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=task_id or uuid.uuid4().hex,
        objective=request.prompt,
        # Compatibility runs are policy-selected but do not claim capability
        # evidence. New v3 callers must declare evidence-backed requirements.
        required_capabilities=(),
        preferred_runtime=request.engine if request.engine != "auto" else None,
        inputs={},
        constraints={
            "engine": request.engine, "agent": request.agent, "model": request.model,
            "provider": request.provider, "timeout": request.timeout, "cwd": request.cwd,
            "cwd_identity": list(getattr(request, "cwd_identity", ()) or ()),
            "sensitive": request.sensitive, "untrusted": request.untrusted,
            "no_fallback": request.no_fallback, "dry_run": request.dry_run,
            "retries": request.retries,
        },
        budget={"timeout_seconds": request.timeout, "retry_limit": request.retries},
    )


class LegacyPlanner:
    """Project an already-authorized legacy decision into a typed plan."""

    def __init__(self, decision: RoutingDecision):
        self.decision = decision

    def plan(self, request: ExecutionRequest, runtimes) -> ExecutionPlan:
        runtime = runtimes.get(self.decision.engine)
        if runtime is None:
            raise RuntimeError(f"runtime not registered: {self.decision.engine}")
        required = tuple(
            capability for capability in request.required_capabilities
            if capability in runtime.capabilities
        )
        return ExecutionPlan(
            plan_id=uuid.uuid4().hex, task_id=request.task_id,
            runtime_id=self.decision.engine,
            capabilities=required,
            steps=("invoke.runtime",), verification=("validate.typed_outcome",),
            reason=self.decision.reason,
        )


class LegacyRuntimeDriver:
    def __init__(self, adapter, request: RunRequest):
        self.adapter = adapter
        self.request = request
        self.last_result: EngineResult | None = None

    def execute(self, request: ExecutionRequest, plan: ExecutionPlan) -> ExecutionOutcome:
        routed = replace(
            self.request, engine=plan.runtime_id,
            agent=request.constraints.get("agent") or self.request.agent,
            model=request.constraints.get("model") or self.request.model,
        )
        self.last_result = self.adapter.run(routed)
        return ExecutionOutcome(
            task_id=request.task_id, plan_id=plan.plan_id,
            success=self.last_result.success,
            output=self.last_result.text,
            error_code=self.last_result.error_code,
            evidence=_runtime_evidence(self.last_result, plan),
        )


class GovernedLegacyRuntimeDriver(LegacyRuntimeDriver):
    """Preserve bounded v2 retry/circuit behavior behind one typed attempt."""

    TRANSIENT = frozenset({
        "timeout", "process_error", "spawn_error", "daemon_unavailable",
        "unavailable", "local_error",
    })

    def __init__(self, adapter, request: RunRequest, breaker, breaker_key: str):
        super().__init__(adapter, request)
        self.breaker = breaker
        self.breaker_key = breaker_key

    def execute(self, request: ExecutionRequest, plan: ExecutionPlan) -> ExecutionOutcome:
        view = self.breaker.before(self.breaker_key)
        if not view.allowed:
            self.last_result = EngineResult(
                plan.runtime_id, False, error="circuit is open",
                error_code="circuit_open", exit_code=1,
            )
        else:
            routed = replace(
                self.request, engine=plan.runtime_id,
                agent=request.constraints.get("agent") or self.request.agent,
                model=request.constraints.get("model") or self.request.model,
            )
            self.last_result = self.adapter.run(routed)
            retries = 0
            while (
                not self.last_result.success
                and self.last_result.error_code in self.TRANSIENT
                and retries < max(0, self.request.retries)
            ):
                retries += 1
                time.sleep(min(2.0, (0.25 * (2 ** (retries - 1))) + random.uniform(0, 0.15)))
                self.last_result = self.adapter.run(routed)
            self.last_result.metadata["attempts"] = retries + 1
            if self.last_result.success:
                self.breaker.success(self.breaker_key)
            else:
                self.breaker.failure(
                    self.breaker_key,
                    self.last_result.error or self.last_result.error_code or "runtime failure",
                )
        return ExecutionOutcome(
            task_id=request.task_id, plan_id=plan.plan_id,
            success=self.last_result.success,
            output=self.last_result.text,
            error_code=self.last_result.error_code,
            evidence=_runtime_evidence(
                self.last_result, plan,
                attempts=int(self.last_result.metadata.get("attempts", 1)),
            ),
        )
