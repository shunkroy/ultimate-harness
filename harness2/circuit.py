"""Persistent engine/provider/model circuit breakers."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .store import Store


@dataclass(frozen=True)
class CircuitView:
    key: str
    state: str
    failures: int
    allowed: bool
    cooldown: float


class CircuitBreaker:
    def __init__(self, store: Store, threshold: int = 3, base_cooldown: float = 30.0, max_cooldown: float = 600.0):
        self.store = store
        self.threshold = max(1, threshold)
        self.base_cooldown = max(1.0, base_cooldown)
        self.max_cooldown = max(self.base_cooldown, max_cooldown)

    def before(self, key: str) -> CircuitView:
        value = self.store.circuit(key)
        state = value["state"]
        if state == "open" and value.get("opened_at") is not None:
            if time.time() - float(value["opened_at"]) >= float(value["cooldown"]):
                state = "half_open"
                value["state"] = state
                self.store.save_circuit(value)
        return CircuitView(key, state, int(value["failures"]), state != "open", float(value["cooldown"]))

    def success(self, key: str) -> None:
        value = self.store.circuit(key)
        value.update(state="closed", failures=0, opened_at=None, cooldown=self.base_cooldown, last_error="")
        self.store.save_circuit(value)

    def failure(self, key: str, error: str) -> CircuitView:
        value = self.store.circuit(key)
        failures = int(value["failures"]) + 1
        value["failures"] = failures
        value["last_error"] = error
        if failures >= self.threshold or value["state"] == "half_open":
            value["state"] = "open"
            value["opened_at"] = time.time()
            value["cooldown"] = min(self.max_cooldown, max(self.base_cooldown, float(value["cooldown"]) * 2))
        self.store.save_circuit(value)
        return self.before(key)
