"""Typed interface shared by all harness engines."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import EngineResult, EngineStatus, RunRequest
from .manifest import RuntimeManifest, manifest_from_status


class EngineAdapter(ABC):
    name: str

    @abstractmethod
    def status(self) -> EngineStatus:
        raise NotImplementedError

    @abstractmethod
    def run(self, request: RunRequest) -> EngineResult:
        raise NotImplementedError

    def manifest(self) -> RuntimeManifest:
        """Declarative registration contract; adapters override to enrich.

        The router never needs to know every vendor: it consumes this data
        plus live status/health. Future declarative manifest files can reuse
        the same schema.
        """
        return manifest_from_status(self.status())
