"""Engine registration and discovery."""

from __future__ import annotations

from typing import Dict

from .adapters import HermesAdapter, LocalAdapter, OpenCodeAdapter, PrimeAdapter, ZenAdapter
from .adapters.base import EngineAdapter
from .config import HarnessConfig
from .store import Store


def build_registry(config: HarnessConfig, store: Store) -> Dict[str, EngineAdapter]:
    engines: list[EngineAdapter] = [
        OpenCodeAdapter(config), ZenAdapter(config), PrimeAdapter(config),
        HermesAdapter(config), LocalAdapter(config, store),
    ]
    return {engine.name: engine for engine in engines}
