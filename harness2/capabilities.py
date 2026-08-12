"""Evidence-backed capability registry."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Callable, Dict, Iterable, Optional

from .models import CapabilityStatus


@dataclass(frozen=True)
class Capability:
    name: str
    status: CapabilityStatus
    engine: str
    description: str
    evidence: str = ""

    def as_dict(self):
        value = asdict(self)
        value["status"] = self.status.value
        return value


def registry(engine_statuses: Dict[str, object]) -> list[Capability]:
    def healthy(name: str) -> bool:
        return bool(getattr(engine_statuses.get(name), "healthy", False))

    def available(name: str) -> bool:
        return bool(getattr(engine_statuses.get(name), "available", False))

    values = [
        Capability("control-plane", CapabilityStatus.ACTIVE if healthy("opencode") else CapabilityStatus.IMPLEMENTED, "opencode", "Expert-routed coding and research", "live OpenCode probe"),
        Capability("zen-model-gateway", CapabilityStatus.ACTIVE if healthy("zen") else CapabilityStatus.IMPLEMENTED, "zen", "Curated OpenCode Zen models", "credential + OpenCode probe"),
        Capability("durable-rlm", CapabilityStatus.ACTIVE if healthy("prime") else CapabilityStatus.IMPLEMENTED, "prime", "Persistent IPython/RLM sessions", "Prime socket + exact PID probe"),
        Capability("recursive-subagents", CapabilityStatus.IMPLEMENTED if available("prime") else CapabilityStatus.DOCUMENTED, "prime", "Prime recursive subagent calls", "Prime v0.7.2 installed"),
        Capability("messaging-worker", CapabilityStatus.IMPLEMENTED if available("hermes") else CapabilityStatus.DOCUMENTED, "hermes", "Hermes messaging and parallel work", "bounded binary probe"),
        Capability("local-private", CapabilityStatus.ACTIVE if healthy("local") else CapabilityStatus.IMPLEMENTED, "local", "Loopback-only local inference", "disabled by default"),
        Capability("encrypted-jobs", CapabilityStatus.ACTIVE, "harness", "Crash-recoverable encrypted job payloads", "crypto and queue tests"),
        Capability("circuit-breakers", CapabilityStatus.ACTIVE, "harness", "Persistent failure isolation", "unit tests"),
        Capability("audit-ledger", CapabilityStatus.ACTIVE, "harness", "Hash-chained metadata-only ledger", "chain verification"),
        Capability("acp-transport", CapabilityStatus.DOCUMENTED, "prime", "Agent Client Protocol integration", "Prime exposes --mode acp; harness adapter not activated"),
    ]
    return values
