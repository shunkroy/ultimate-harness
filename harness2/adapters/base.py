"""Typed interface shared by all harness engines."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import EngineResult, EngineStatus, RunRequest


class EngineAdapter(ABC):
    name: str

    @abstractmethod
    def status(self) -> EngineStatus:
        raise NotImplementedError

    @abstractmethod
    def run(self, request: RunRequest) -> EngineResult:
        raise NotImplementedError
