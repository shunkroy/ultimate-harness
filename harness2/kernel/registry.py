"""In-memory typed registries with explicit duplicate/conflict handling."""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Protocol

from .contracts import CapabilityDescriptor, ExecutionOutcome, ExecutionPlan, ExecutionRequest, RuntimeDescriptor


class RegistryConflict(ValueError):
    pass


class RuntimeDriver(Protocol):
    def execute(self, request: ExecutionRequest, plan: ExecutionPlan) -> ExecutionOutcome: ...


class RuntimeRegistry:
    def __init__(self, descriptors: Iterable[RuntimeDescriptor] = ()):
        self._values: Dict[str, RuntimeDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: RuntimeDescriptor, *, replace: bool = False) -> None:
        if descriptor.id in self._values and not replace:
            raise RegistryConflict(f"runtime already registered: {descriptor.id}")
        self._values[descriptor.id] = descriptor

    def get(self, runtime_id: str) -> Optional[RuntimeDescriptor]:
        return self._values.get(runtime_id)

    def all(self) -> tuple[RuntimeDescriptor, ...]:
        return tuple(self._values[key] for key in sorted(self._values))

    def supporting(self, capability_id: str, *, enabled_only: bool = True) -> tuple[RuntimeDescriptor, ...]:
        values = (
            item for item in self._values.values()
            if capability_id in item.capabilities and (item.enabled or not enabled_only)
        )
        return tuple(sorted(values, key=lambda item: item.id))


class CapabilityRegistry:
    def __init__(self, capabilities: Iterable[CapabilityDescriptor] = ()):
        self._values: Dict[str, CapabilityDescriptor] = {}
        for capability in capabilities:
            self.register(capability)

    def register(self, capability: CapabilityDescriptor, *, replace: bool = False) -> None:
        if capability.id in self._values and not replace:
            raise RegistryConflict(f"capability already registered: {capability.id}")
        self._values[capability.id] = capability

    def get(self, capability_id: str) -> Optional[CapabilityDescriptor]:
        return self._values.get(capability_id)

    def all(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._values[key] for key in sorted(self._values))

    def validate(self, runtimes: RuntimeRegistry) -> tuple[str, ...]:
        errors: list[str] = []
        for capability in self._values.values():
            for provider in capability.providers:
                runtime = runtimes.get(provider)
                if runtime is None:
                    errors.append(f"{capability.id}: provider not registered: {provider}")
                elif capability.id not in runtime.capabilities:
                    errors.append(f"{capability.id}: provider {provider} does not declare capability")
        return tuple(sorted(errors))
