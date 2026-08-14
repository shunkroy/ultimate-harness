"""Application services that bridge compatible frontends into the kernel."""

from __future__ import annotations

import os
import time
from dataclasses import replace

from .kernel.application import ApplicationKernel
from .kernel.catalog import build_catalog
from .legacy_bridge import (
    GovernedLegacyRuntimeDriver,
    LegacyPlanner,
    execution_request_from_legacy,
)
from .models import EngineResult, RunRequest
from .security import task_hash


class ForegroundExecutionService:
    """Make explicit provider runs use typed kernel lifecycle authoritatively.

    Automatic multi-provider fallback remains on the v2 compatibility path until
    execution plans can model and evidence every fallback step.
    """

    def __init__(self, store, engines, orchestrator, events, tasks):
        self.store = store
        self.engines = engines
        self.orchestrator = orchestrator
        self.events = events
        self.tasks = tasks

    def run(self, request: RunRequest):
        if request.engine == "auto" or request.dry_run:
            return self.orchestrator.run(request)

        started = time.time()
        request, decision = self.orchestrator.prepare(request)
        self.store.append_audit(
            "route.selected", decision.engine,
            {
                "task_hash": task_hash(request.prompt), "engine": decision.engine,
                "agent": decision.agent or "", "model": decision.model or "",
                "reason": decision.reason, "task_class": decision.task_class,
                "lifecycle": "kernel.v3",
            },
        )
        typed = execution_request_from_legacy(request)
        statuses = self.orchestrator.statuses()
        runtimes, capabilities = build_catalog(statuses)
        adapter = self.engines.get(decision.engine)
        if adapter is None:
            raise RuntimeError(f"runtime adapter unavailable: {decision.engine}")
        routed_request = replace(request, agent=decision.agent, model=decision.model)
        breaker_key = self.orchestrator.breaker_key(decision.engine, routed_request, decision)
        driver = GovernedLegacyRuntimeDriver(
            adapter, routed_request, self.orchestrator.breakers, breaker_key,
        )
        kernel = ApplicationKernel(
            self.tasks, runtimes, capabilities, {decision.engine: driver},
            LegacyPlanner(decision),
        )
        lifecycle = kernel.execute(
            typed, idempotency_key=f"foreground:{typed.task_id}",
            owner_id=f"foreground:{os.getpid()}", lease_seconds=request.timeout + 120,
            source="cli", reason="explicit provider execution",
            authority="user", max_attempts=1,
        )
        result = driver.last_result or EngineResult(
            decision.engine, False, error="runtime produced no compatible result",
            error_code=lifecycle.outcome.error_code if lifecycle.outcome else "kernel_error",
            exit_code=1,
        )
        result.metadata["kernel_task_id"] = typed.task_id
        result.metadata["kernel_plan_id"] = lifecycle.plan.plan_id if lifecycle.plan else ""
        try:
            run_id = self.store.record_run(request, decision, result, started)
        except Exception:
            # Kernel task/event state is authoritative. Legacy projection failure
            # cannot erase a completed typed lifecycle.
            result.metadata["legacy_run_projection"] = "failed"
            run_id = ""
        return decision, result, run_id
