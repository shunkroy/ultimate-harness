"""Empirical provider observations; no provider reputation is hard-coded."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProviderObservation:
    provider_id: str
    runtime_id: str
    capability_id: str
    success: bool
    latency_ms: int
    evidence_hash: str
    task_id: Optional[str] = None
    correctness_score: Optional[float] = None
    estimated_cost: Optional[float] = None
    failure_class: Optional[str] = None
    tool_use_score: Optional[float] = None
    privacy_class: str = "standard"
    offline_available: bool = False
    observed_at: float = 0.0
    observation_id: str = ""

    def __post_init__(self) -> None:
        if not self.provider_id or not self.runtime_id or not self.capability_id:
            raise ValueError("provider, runtime and capability are required")
        if self.latency_ms < 0:
            raise ValueError("latency cannot be negative")
        for value, name in (
            (self.correctness_score, "correctness_score"),
            (self.tool_use_score, "tool_use_score"),
        ):
            if value is not None and not 0 <= value <= 1:
                raise ValueError(f"{name} must be between zero and one")
        if len(self.evidence_hash) != 64 or any(char not in "0123456789abcdef" for char in self.evidence_hash):
            raise ValueError("evidence_hash must be SHA-256")
        if self.estimated_cost is not None and (
            not math.isfinite(self.estimated_cost) or self.estimated_cost < 0
        ):
            raise ValueError("estimated_cost must be finite and nonnegative")
        if not math.isfinite(self.observed_at or 0.0):
            raise ValueError("observed_at must be finite")
        if not self.observation_id:
            object.__setattr__(self, "observation_id", uuid.uuid4().hex)
        if not self.observed_at:
            object.__setattr__(self, "observed_at", time.time())


@dataclass(frozen=True)
class CapabilityScore:
    provider_id: str
    capability_id: str
    observations: int
    success_rate: float
    mean_correctness: Optional[float]
    mean_latency_ms: float
    mean_estimated_cost: Optional[float]


class ProviderIntelligence:
    def __init__(self, store):
        self.store = store

    def record(self, value: ProviderObservation) -> ProviderObservation:
        with self.store.connect() as con:
            con.execute(
                "INSERT INTO kernel_provider_observations("
                "observation_id,provider_id,runtime_id,capability_id,task_id,success,"
                "correctness_score,latency_ms,estimated_cost,failure_class,tool_use_score,"
                "privacy_class,offline_available,observed_at,evidence_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value.observation_id, value.provider_id, value.runtime_id,
                    value.capability_id, value.task_id, int(value.success),
                    value.correctness_score, value.latency_ms, value.estimated_cost,
                    value.failure_class, value.tool_use_score, value.privacy_class,
                    int(value.offline_available), value.observed_at, value.evidence_hash,
                ),
            )
        return value

    def scores(self, *, capability_id: str | None = None) -> tuple[CapabilityScore, ...]:
        where = " WHERE capability_id=?" if capability_id else ""
        values = (capability_id,) if capability_id else ()
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT provider_id,capability_id,COUNT(*) AS observations,"
                "AVG(success) AS success_rate,AVG(correctness_score) AS correctness,"
                "AVG(latency_ms) AS latency,AVG(estimated_cost) AS cost "
                "FROM kernel_provider_observations" + where
                + " GROUP BY provider_id,capability_id ORDER BY capability_id,provider_id",
                values,
            ).fetchall()
        return tuple(CapabilityScore(
            str(row["provider_id"]), str(row["capability_id"]), int(row["observations"]),
            float(row["success_rate"]),
            float(row["correctness"]) if row["correctness"] is not None else None,
            float(row["latency"]),
            float(row["cost"]) if row["cost"] is not None else None,
        ) for row in rows)
