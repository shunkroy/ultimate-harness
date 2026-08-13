"""Resource observation and deterministic checkpoint-before-pause decisions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ResourceAction(str, Enum):
    ALLOW = "allow"
    THROTTLE = "throttle"
    CHECKPOINT_AND_PAUSE = "checkpoint_and_pause"
    REFUSE = "refuse"


@dataclass(frozen=True)
class ResourceLimits:
    min_disk_free_bytes: int = 2 * 1024**3
    min_battery_percent: float = 20.0
    max_queue_length: int = 1000
    max_process_count: int = 1000


@dataclass(frozen=True)
class ResourceObservation:
    node_id: str
    disk_free_bytes: int
    memory_available_bytes: Optional[int] = None
    cpu_load: Optional[float] = None
    battery_percent: Optional[float] = None
    charging: Optional[bool] = None
    thermal_state: Optional[str] = None
    network_available: Optional[bool] = None
    process_count: Optional[int] = None
    queue_length: Optional[int] = None
    observed_at: float = 0.0
    observation_id: str = ""

    def __post_init__(self) -> None:
        if not self.node_id or self.disk_free_bytes < 0:
            raise ValueError("valid node and disk observation required")
        if self.observed_at and not math.isfinite(self.observed_at):
            raise ValueError("observed_at must be finite")
        if self.cpu_load is not None and (not math.isfinite(self.cpu_load) or self.cpu_load < 0):
            raise ValueError("cpu_load must be finite and nonnegative")
        if self.memory_available_bytes is not None and self.memory_available_bytes < 0:
            raise ValueError("memory availability cannot be negative")
        if self.battery_percent is not None and (
            not math.isfinite(self.battery_percent) or not 0 <= self.battery_percent <= 100
        ):
            raise ValueError("battery_percent must be between zero and 100")
        if self.process_count is not None and self.process_count < 0:
            raise ValueError("process_count cannot be negative")
        if self.queue_length is not None and self.queue_length < 0:
            raise ValueError("queue_length cannot be negative")
        if not self.observed_at:
            object.__setattr__(self, "observed_at", time.time())
        if not self.observation_id:
            object.__setattr__(self, "observation_id", uuid.uuid4().hex)


@dataclass(frozen=True)
class ResourceDecision:
    action: ResourceAction
    reasons: tuple[str, ...]
    checkpoint_required: bool


class ResourceGovernor:
    def __init__(self, store, limits: ResourceLimits = ResourceLimits()):
        self.store = store
        self.limits = limits

    @staticmethod
    def observe(node_id: str, state_root: str, *, queue_length: int | None = None) -> ResourceObservation:
        disk = shutil.disk_usage(state_root).free
        load = None
        try:
            load = float(os.getloadavg()[0])
        except (AttributeError, OSError):
            pass
        return ResourceObservation(node_id, disk, cpu_load=load, queue_length=queue_length)

    def record(self, value: ResourceObservation) -> ResourceObservation:
        payload = json.dumps({
            "node_id": value.node_id, "observed_at": value.observed_at,
            "cpu_load": value.cpu_load, "memory_available_bytes": value.memory_available_bytes,
            "disk_free_bytes": value.disk_free_bytes, "battery_percent": value.battery_percent,
            "charging": value.charging, "thermal_state": value.thermal_state,
            "network_available": value.network_available, "process_count": value.process_count,
            "queue_length": value.queue_length,
        }, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.store.connect() as con:
            con.execute(
                "INSERT INTO kernel_resource_observations("
                "observation_id,node_id,observed_at,cpu_load,memory_available_bytes,disk_free_bytes,"
                "battery_percent,charging,thermal_state,network_available,process_count,queue_length,"
                "payload_json,payload_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value.observation_id, value.node_id, value.observed_at, value.cpu_load,
                    value.memory_available_bytes, value.disk_free_bytes, value.battery_percent,
                    int(value.charging) if value.charging is not None else None,
                    value.thermal_state,
                    int(value.network_available) if value.network_available is not None else None,
                    value.process_count, value.queue_length, payload,
                    hashlib.sha256(payload.encode()).hexdigest(),
                ),
            )
        return value

    def evaluate(self, value: ResourceObservation, *, task_running: bool = False, critical: bool = False) -> ResourceDecision:
        reasons: list[str] = []
        action = ResourceAction.ALLOW
        if value.disk_free_bytes < self.limits.min_disk_free_bytes:
            reasons.append("disk_free_below_threshold")
            action = ResourceAction.CHECKPOINT_AND_PAUSE if task_running else ResourceAction.REFUSE
        if (
            value.battery_percent is not None
            and value.battery_percent < self.limits.min_battery_percent
            and value.charging is False and not critical
        ):
            reasons.append("battery_low_not_charging")
            action = ResourceAction.CHECKPOINT_AND_PAUSE if task_running else ResourceAction.REFUSE
        if value.queue_length is not None and value.queue_length > self.limits.max_queue_length:
            reasons.append("queue_limit_exceeded")
            action = ResourceAction.THROTTLE if action == ResourceAction.ALLOW else action
        if value.process_count is not None and value.process_count > self.limits.max_process_count:
            reasons.append("process_limit_exceeded")
            action = ResourceAction.THROTTLE if action == ResourceAction.ALLOW else action
        return ResourceDecision(
            action, tuple(reasons), action == ResourceAction.CHECKPOINT_AND_PAUSE,
        )
