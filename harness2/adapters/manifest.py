"""Declarative runtime/provider manifest contract (seam).

Harness should not need to know every AI in existence; it needs to know the
contract an AI runtime must satisfy. This module defines that contract.

Today manifests are produced by the installed adapters (``manifest()``).
Tomorrow the same schema can be loaded from declarative manifest files so a
future engine that does not exist today can be registered without rewriting
the central router. Nothing in the routing logic branches on vendor names --
it consumes this data plus live status/health.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

from ..models import EngineStatus

#: Cost classes understood by the scoring seam.
COST_FREE = "free"
COST_SUBSCRIPTION = "subscription"
COST_PAID_API = "paid-api"
COST_PRIVATE = "private"
COST_MIXED = "mixed"
COST_UNKNOWN = "unknown"

#: Privacy classes.
PRIVACY_EXTERNAL = "external"
PRIVACY_LOCAL = "local"


@dataclass(frozen=True)
class RuntimeManifest:
    """Static contract of an engine/provider runtime.

    Fields:
        id: registry id (engine name).
        capabilities: capability tokens the runtime can satisfy.
        auth_mechanisms: supported authentication kinds (oauth, api-key,
            account-cli, env, none, user-authorized).
        auth_configured: whether authentication is currently configured.
        cost_class: free | subscription | paid-api | private | mixed | unknown.
        privacy_class: external | local.
        models: model ids known at registration; empty means the runtime
            discovers its catalog dynamically (preferred).
        context_limits: (min, max) tokens if known; empty when unknown.
        health_probe: name of the live health check contract.
        failure_normalization: whether failures are normalized (default True).
        execution_contract: human-readable invocation contract.
    """

    id: str
    capabilities: Tuple[str, ...] = ()
    auth_mechanisms: Tuple[str, ...] = ()
    auth_configured: bool = False
    cost_class: str = COST_UNKNOWN
    privacy_class: str = PRIVACY_EXTERNAL
    models: Tuple[str, ...] = ()
    context_limits: Tuple[int, int] = ()
    health_probe: str = "status()"
    failure_normalization: bool = True
    execution_contract: str = "engine.run(request) -> EngineResult"
    evidence: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "capabilities": list(self.capabilities),
            "auth_mechanisms": list(self.auth_mechanisms),
            "auth_configured": self.auth_configured,
            "cost_class": self.cost_class,
            "privacy_class": self.privacy_class,
            "models": list(self.models),
            "context_limits": list(self.context_limits),
            "health_probe": self.health_probe,
            "failure_normalization": self.failure_normalization,
            "execution_contract": self.execution_contract,
            "evidence": list(self.evidence),
        }


def manifest_from_status(status: EngineStatus, **overrides: object) -> RuntimeManifest:
    """Derive a minimal manifest from a live status (fallback registration)."""
    base = RuntimeManifest(
        id=status.name,
        capabilities=status.capabilities,
        auth_configured=bool(status.enabled and status.available),
        cost_class=status.cost_class,
    )
    if not overrides:
        return base
    values = {
        "id": base.id, "capabilities": base.capabilities,
        "auth_mechanisms": base.auth_mechanisms, "auth_configured": base.auth_configured,
        "cost_class": base.cost_class, "privacy_class": base.privacy_class,
        "models": base.models, "context_limits": base.context_limits,
        "health_probe": base.health_probe, "failure_normalization": base.failure_normalization,
        "execution_contract": base.execution_contract, "evidence": base.evidence,
    }
    values.update(overrides)
    return RuntimeManifest(**values)
