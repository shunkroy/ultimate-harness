"""Compatibility projection from current adapters into kernel descriptors."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from ..models import EngineStatus
from .contracts import (
    CapabilityDescriptor,
    CapabilityEvidence,
    EvidenceKind,
    Health,
    Maturity,
    RuntimeDescriptor,
)
from .registry import CapabilityRegistry, RuntimeRegistry


_CAPABILITY_DESCRIPTIONS = {
    "reason.general": "General model-assisted reasoning",
    "code.execute": "Read, modify, or analyze code through a governed runtime",
    "research.general": "General research and synthesis",
    "agent.durable": "Durable session or long-running agent execution",
    "agent.recursive": "Bounded recursive subagent execution",
    "agent.parallel": "Parallel worker delegation",
    "message.send": "External messaging through an authorized gateway",
    "reason.private": "Private loopback-only model reasoning",
    "job.encrypted": "Encrypted crash-recoverable background jobs",
    "failure.circuit_breaker": "Persistent engine failure isolation",
    "audit.hash_linked": "Hash-linked metadata audit events",
    "integrity.verify": "Pinned artifact integrity verification",
    "provider.discover.cli": "Bounded declarative CLI provider discovery",
    "context.compile.text": "Compile structured text into a verified context package",
    "context.execute.query": "Execute provenance-bearing context queries",
    "context.execute.transform": "Execute allowlisted deterministic text transforms",
    "context.execute.generate": "Produce source-supported context evidence briefs",
    "runtime.always_active": "Process bounded maintenance without a chat event",
}

_LEGACY_CAPABILITIES = {
    "opencode": ("reason.general", "code.execute", "research.general"),
    "zen": ("reason.general", "code.execute", "research.general"),
    "prime": ("reason.general", "code.execute", "agent.durable", "agent.recursive"),
    "hermes": ("reason.general", "agent.parallel", "message.send"),
    "local": ("reason.private",),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def runtime_from_engine(name: str, status: EngineStatus, *, location: str | None = None) -> RuntimeDescriptor:
    if not status.enabled:
        health = Health.DISABLED
    elif status.healthy:
        health = Health.HEALTHY
    elif status.available:
        health = Health.DEGRADED
    else:
        health = Health.DOWN
    maturity = Maturity.IMPLEMENTED if status.available else Maturity.DESIGNED
    evidence = CapabilityEvidence(
        EvidenceKind.LOCAL_OBSERVATION, location or f"adapter:{name}", _now(), status.detail,
    )
    return RuntimeDescriptor(
        name, "adapter", name, None, location, "python_adapter",
        status.prompt_transport, "normalized_result", _LEGACY_CAPABILITIES.get(name, ()),
        (), (), maturity, status.enabled, health, (evidence,),
    )


def build_catalog(statuses: Mapping[str, EngineStatus]) -> tuple[RuntimeRegistry, CapabilityRegistry]:
    runtimes = RuntimeRegistry(runtime_from_engine(name, status) for name, status in statuses.items())
    providers: dict[str, list[str]] = {}
    for runtime in runtimes.all():
        for capability in runtime.capabilities:
            providers.setdefault(capability, []).append(runtime.id)

    harness_capabilities = (
        "job.encrypted", "failure.circuit_breaker", "audit.hash_linked",
        "integrity.verify", "provider.discover.cli", "context.compile.text",
        "context.execute.query", "context.execute.transform",
        "context.execute.generate", "runtime.always_active",
    )
    harness = RuntimeDescriptor(
        "harness", "kernel", "Harness Core", None, None, "python_kernel",
        "typed_object", "typed_object", harness_capabilities,
        (), (), Maturity.TESTED, True, Health.HEALTHY,
        (CapabilityEvidence(EvidenceKind.TEST_VERIFIED, "tests/", _now(), "local automated test suite"),),
    )
    runtimes.register(harness)
    for capability in harness_capabilities:
        providers.setdefault(capability, []).append("harness")

    harness_evidence = CapabilityEvidence(
        EvidenceKind.TEST_VERIFIED, "tests/", _now(), "local automated test suite",
    )
    capabilities = CapabilityRegistry()
    for capability_id, provider_ids in sorted(providers.items()):
        evidence = (harness_evidence,) if provider_ids == ["harness"] else ()
        maturity = Maturity.TESTED if evidence else Maturity.IMPLEMENTED
        capabilities.register(CapabilityDescriptor(
            capability_id,
            _CAPABILITY_DESCRIPTIONS.get(capability_id, capability_id),
            maturity,
            tuple(sorted(provider_ids)),
            evidence,
            verification_required=True,
        ))
    return runtimes, capabilities
