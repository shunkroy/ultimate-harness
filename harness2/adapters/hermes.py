"""Hermes worker adapter with a scrubbed environment."""

from __future__ import annotations

import os
import subprocess
import time

from .base import EngineAdapter
from ..config import HarnessConfig
from ..models import CapabilityStatus, EngineResult, EngineStatus, RunRequest


class HermesAdapter(EngineAdapter):
    name = "hermes"

    def __init__(self, config: HarnessConfig):
        self.config = config

    def status(self) -> EngineStatus:
        available = bool(self.config.hermes_bin and os.path.isfile(self.config.hermes_bin))
        enabled = available and os.environ.get("HARNESS_HERMES_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        return EngineStatus(
            self.name, available, enabled, enabled,
            CapabilityStatus.IMPLEMENTED if available else CapabilityStatus.DOCUMENTED,
            ("enabled; bounded invocation" if enabled else "installed but disabled pending authorized provider") if available else "binary missing",
            ("parallel-worker", "messaging", "gateway"),
            "argv-visible",
        )

    def run(self, request: RunRequest) -> EngineResult:
        started = time.monotonic()
        if not self.status().available:
            return EngineResult(self.name, False, error="Hermes binary is unavailable", error_code="unavailable", exit_code=1)
        try:
            proc = subprocess.run(
                [*self.config.command("hermes"), "chat", "-z", request.prompt],
                env=self.config.clean_env("hermes"),
                cwd=request.cwd or os.getcwd(),
                capture_output=True,
                text=True,
                timeout=request.timeout,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(self.name, False, error=f"timed out after {request.timeout}s", error_code="timeout", exit_code=124, duration=time.monotonic() - started)
        except OSError as exc:
            return EngineResult(self.name, False, error=str(exc), error_code="spawn_error", exit_code=1, duration=time.monotonic() - started)
        output = (proc.stdout or "").strip()
        error = None if proc.returncode == 0 else (proc.stderr or output or "Hermes failed").strip()
        return EngineResult(
            self.name, proc.returncode == 0 and bool(output), output,
            error, None if proc.returncode == 0 else "process_error",
            proc.returncode if proc.returncode else (0 if output else 2),
            time.monotonic() - started,
        )
