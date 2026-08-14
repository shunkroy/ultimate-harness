"""Engine execution, fallback and circuit-breaker orchestration."""

from __future__ import annotations

import time
import random
from dataclasses import replace
from typing import Dict, List

from .adapters.base import EngineAdapter
from .circuit import CircuitBreaker
from .events import FAILURE_UNKNOWN, normalize_failure
from .models import EngineResult, RoutingDecision, RunRequest
from .policy import PolicyRouter, canonicalize_request
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
        return self.prepare(request)[1]

    def prepare(self, request: RunRequest) -> tuple[RunRequest, RoutingDecision]:
        prepared = canonicalize_request(request)
        return prepared, PolicyRouter(self.statuses()).decide(prepared)

    def run(self, request: RunRequest) -> tuple[RoutingDecision, EngineResult, str]:
        request, decision = self.prepare(request)
        started = time.time()
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
                metadata={
                    "decision": decision.reason,
                    **self._route_metadata(request, decision, []),
                },
            )
            return decision, result, ""

        candidates = list(decision.candidates) if decision.candidates else [decision.engine]
        if not request.no_fallback and request.engine == "auto" and decision.fallbacks:
            candidates.extend(x for x in decision.fallbacks if x not in candidates)
        if request.no_fallback:
            candidates = candidates[:1]

        last = EngineResult(decision.engine, False, error="no engine attempted", error_code="unavailable", exit_code=1)
        chosen = decision
        tried: List[Dict[str, str]] = []
        for engine_name in candidates:
            engine = self.engines.get(engine_name)
            if engine is None:
                self.store.append_audit("route.skipped", engine_name, {"reason": "no adapter registered"})
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
                self.store.append_audit(
                    "route.skipped", engine_name,
                    {"reason": f"circuit open (state={view.state}, failures={view.failures})"},
                )
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
            normalized = normalize_failure(result.error_code, result.error) if not result.success else ""
            result.metadata["normalized_failure"] = normalized
            result.metadata["raw_error_code"] = result.error_code or ""
            tried.append({
                "engine": engine_name,
                "provider": request.provider or "",
                "model": current.model or "",
                "status": "succeeded" if result.success else "failed",
                "normalized_failure": normalized,
                "error_code": result.error_code or "",
                "attempts": str(attempt + 1),
            })
            if result.success:
                self.breakers.success(key)
                chosen, last = current, result
                break
            self.breakers.failure(key, result.error or result.error_code or "engine failure")
            self.store.append_audit(
                "route.failed", engine_name,
                {
                    "provider": request.provider or "", "model": current.model or "",
                    "error_code": result.error_code or "", "normalized_failure": normalized,
                    "attempts": attempt + 1,
                },
            )
            chosen, last = current, result

        last.metadata.update(self._route_metadata(request, chosen, tried))

        run_id = self.store.record_run(request, chosen, last, started)
        if run_id:
            self.store.append_audit(
                "route.completed", run_id,
                self._route_metadata(request, chosen, tried),
            )
        return chosen, last, run_id

    @staticmethod
    def _route_metadata(
        request: RunRequest, decision: RoutingDecision, tried: List[Dict[str, str]],
    ) -> Dict[str, object]:
        """Routing observability: candidates, skips, attempts and final route."""
        candidates = list(decision.candidates) if decision.candidates else [decision.engine]
        last_tried = tried[-1] if tried else {}
        return {
            "candidate_routes": candidates,
            "skipped_routes": [{"engine": name, "reason": reason} for name, reason in decision.skipped],
            "tried_routes": tried,
            "fell_back": bool(tried) and tried[0]["engine"] != decision.engine,
            "final_route": {
                "engine": decision.engine,
                "provider": request.provider or "",
                "model": decision.model or "",
                "normalized_failure": last_tried.get("normalized_failure", FAILURE_UNKNOWN),
                "fallback_reason": decision.reason,
            },
        }
