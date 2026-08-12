"""Shared typed models for Harness v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class CapabilityStatus(str, Enum):
    DOCUMENTED = "documented"
    PLANNED = "planned"
    TESTED = "tested"
    IMPLEMENTED = "implemented"
    ACTIVE = "active"


@dataclass(frozen=True)
class RunRequest:
    prompt: str
    engine: str = "auto"
    agent: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    timeout: int = 240
    cwd: Optional[str] = None
    sensitive: bool = False
    untrusted: bool = False
    no_fallback: bool = False
    dry_run: bool = False
    retries: int = 1


@dataclass
class EngineResult:
    engine: str
    success: bool
    text: str = ""
    error: Optional[str] = None
    error_code: Optional[str] = None
    exit_code: int = 0
    duration: float = 0.0
    session_id: Optional[str] = None
    raw_event_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineStatus:
    name: str
    available: bool
    healthy: bool
    enabled: bool
    status: CapabilityStatus
    detail: str = ""
    capabilities: Tuple[str, ...] = ()
    prompt_transport: str = "unknown"


@dataclass(frozen=True)
class RoutingDecision:
    engine: str
    agent: Optional[str]
    model: Optional[str]
    reason: str
    fallbacks: Tuple[str, ...] = ()
    task_class: str = "general"
