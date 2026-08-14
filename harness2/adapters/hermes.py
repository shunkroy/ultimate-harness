"""Hermes worker adapter with a scrubbed environment."""

from __future__ import annotations

import time

from .base import EngineAdapter
from ..config import HarnessConfig
from ..models import CapabilityStatus, EngineResult, EngineStatus, RunRequest
from ..execution import (
    ProcessConfigurationError,
    ProcessRequest,
    ProcessSpawnError,
    prepare_working_directory,
    run_process,
    secret_environment_keys,
)
from .manifest import RuntimeManifest


class HermesAdapter(EngineAdapter):
    name = "hermes"

    def __init__(self, config: HarnessConfig):
        self.config = config

    def manifest(self) -> RuntimeManifest:
        return RuntimeManifest(
            id=self.name,
            capabilities=("reason.general", "parallel-worker", "messaging", "gateway"),
            auth_mechanisms=("env", "user-authorized"),
            auth_configured=False,
            cost_class="mixed",
            privacy_class="external",
            models=(),
            health_probe="status()",
            execution_contract="hermes chat -z <prompt> (argv-visible transport)",
            evidence=("disabled pending authorized provider",),
        )

    def status(self) -> EngineStatus:
        available = self.config.executable_available("hermes")
        enabled = available and self.config.platform.env.get(
            "HARNESS_HERMES_ENABLED", "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        return EngineStatus(
            self.name, available, enabled, enabled,
            CapabilityStatus.IMPLEMENTED if available else CapabilityStatus.DOCUMENTED,
            ("enabled; bounded invocation" if enabled else "installed but disabled pending authorized provider") if available else "binary missing",
            ("parallel-worker", "messaging", "gateway"),
            "argv-visible",
        )

    def run(self, request: RunRequest) -> EngineResult:
        started = time.monotonic()
        status = self.status()
        if not status.available:
            return EngineResult(self.name, False, error="Hermes binary is unavailable", error_code="unavailable", exit_code=1)
        if not status.enabled:
            return EngineResult(self.name, False, error="Hermes is disabled by policy", error_code="disabled", exit_code=2)
        try:
            cwd, cwd_identity = prepare_working_directory(
                request.cwd, getattr(request, "cwd_identity", None),
            )
            command = [*self.config.command("hermes"), "chat", "-z", request.prompt]
            env = self.config.clean_env("hermes")
            proc = run_process(ProcessRequest(
                tuple(command), cwd=cwd, cwd_identity=cwd_identity,
                env=env, timeout=request.timeout,
                stdout_limit=int(self.config.stdout_limit),
                stderr_limit=int(self.config.stderr_limit),
                private_argv_indices=(len(command) - 1,),
                secret_env_keys=secret_environment_keys(env),
            ))
        except ProcessConfigurationError as exc:
            return EngineResult(self.name, False, error=str(exc), error_code="invalid_execution", exit_code=2, duration=time.monotonic() - started)
        except (ProcessSpawnError, OSError) as exc:
            return EngineResult(self.name, False, error=str(exc), error_code="spawn_error", exit_code=1, duration=time.monotonic() - started)
        if proc.timed_out:
            return EngineResult(self.name, False, error=f"timed out after {request.timeout}s", error_code="timeout", exit_code=124, duration=proc.duration, metadata={"execution_config_sha256": proc.config_fingerprint})
        if proc.output_limited:
            return EngineResult(self.name, False, text=proc.stdout, error="provider output exceeded the configured byte limit", error_code="output_limit", exit_code=125, duration=proc.duration, metadata={"execution_config_sha256": proc.config_fingerprint})
        output = (proc.stdout or "").strip()
        error = None if proc.returncode == 0 else (proc.stderr or output or "Hermes failed").strip()
        return EngineResult(
            self.name, proc.returncode == 0 and bool(output), output,
            error, None if proc.returncode == 0 else "process_error",
            proc.returncode if proc.returncode else (0 if output else 2),
            proc.duration,
            metadata={"execution_config_sha256": proc.config_fingerprint},
        )
