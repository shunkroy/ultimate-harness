"""Engine execution, fallback and circuit-breaker orchestration."""

from __future__ import annotations

import time
import random
from dataclasses import replace
from typing import Dict

from .adapters.base import EngineAdapter
from .circuit import CircuitBreaker
from .models import EngineResult, RoutingDecision, RunRequest
from .policy import PolicyRouter
from .store import Store


class Orchestrator:
    def __init__(self, engines: Dict[str, EngineAdapter], store: Store):
        self.engines = engines
        self.store = store
        self.breakers = CircuitBreaker(store)

    def statuses(self):
        return {name: engine.status() for name, engine in self.engines.items()}

    @staticmethod
    def breaker_key(engine: str, request: RunRequest, decision: RoutingDecision) -> str:
        return ":".join((engine, request.provider or "-", decision.model or "-"))

    def decide(self, request: RunRequest) -> RoutingDecision:
        return PolicyRouter(self.statuses()).decide(request)

    def run(self, request: RunRequest) -> tuple[RoutingDecision, EngineResult, str]:
        started = time.time()
        decision = self.decide(request)
        self.store.append_audit(
            "route.selected", decision.engine,
            {
                "task_hash": __import__("hashlib").sha256(request.prompt.encode()).hexdigest(),
                "engine": decision.engine, "agent": decision.agent or "",
                "model": decision.model or "", "reason": decision.reason,
                "task_class": decision.task_class,
            },
        )
        if request.dry_run:
            result = EngineResult(
                decision.engine, True, text="dry-run: no engine invoked", exit_code=0,
                metadata={"decision": decision.reason},
            )
            return decision, result, ""

        candidates = [decision.engine]
        if not request.no_fallback and request.engine == "auto":
            candidates.extend(x for x in decision.fallbacks if x not in candidates)

        last = EngineResult(decision.engine, False, error="no engine attempted", error_code="unavailable", exit_code=1)
        chosen = decision
        for engine_name in candidates:
            engine = self.engines.get(engine_name)
            if engine is None:
                continue
            current = decision if engine_name == decision.engine else replace(
                decision, engine=engine_name,
                agent=(decision.agent if engine_name in ("opencode", "zen") else None),
                reason=f"fallback after {last.engine}: {last.error_code or 'failure'}",
                fallbacks=(),
            )
            key = self.breaker_key(engine_name, request, current)
            view = self.breakers.before(key)
            if not view.allowed:
                last = EngineResult(engine_name, False, error="circuit is open", error_code="circuit_open", exit_code=1)
                continue
            routed = replace(request, engine=engine_name, agent=current.agent, model=current.model)
            result = engine.run(routed)
            transient = {"timeout", "process_error", "spawn_error", "daemon_unavailable", "unavailable", "local_error"}
            attempt = 0
            while (
                not result.success and result.error_code in transient
                and attempt < max(0, request.retries)
            ):
                attempt += 1
                time.sleep(min(2.0, (0.25 * (2 ** (attempt - 1))) + random.uniform(0, 0.15)))
                result = engine.run(routed)
            result.metadata["attempts"] = attempt + 1
            if result.success:
                self.breakers.success(key)
                chosen, last = current, result
                break
            self.breakers.failure(key, result.error or result.error_code or "engine failure")
            chosen, last = current, result

        run_id = self.store.record_run(request, chosen, last, started)
        return chosen, last, run_id
