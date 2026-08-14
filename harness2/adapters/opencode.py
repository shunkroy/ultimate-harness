"""OpenCode and OpenCode Zen provider adapters."""

from __future__ import annotations

from dataclasses import replace
import os
import time

from .base import EngineAdapter
from ..config import HarnessConfig
from ..events import parse_text
from ..execution import (
    ProcessConfigurationError,
    ProcessRequest,
    ProcessSpawnError,
    prepare_working_directory,
    run_process,
    secret_environment_keys,
)
from ..models import CapabilityStatus, EngineResult, EngineStatus, RunRequest
from ..security import PrivateTempFile, ensure_private_dir
from .manifest import COST_MIXED, RuntimeManifest

#: Zen free-model catalog discovered from the installed runtime
#: (``opencode models``, 2026-08-14). Kept as manifest *data*, not router
#: logic; the runtime remains the authoritative discovery source.
ZEN_FREE_MODELS: tuple[str, ...] = (
    "opencode/big-pickle",
    "opencode/deepseek-v4-flash-free",
    "opencode/hy3-free",
    "opencode/laguna-s-2.1-free",
    "opencode/mimo-v2.5-free",
    "opencode/nemotron-3-ultra-free",
    "opencode/nemotron-3.5-lightning-free",
)


class OpenCodeAdapter(EngineAdapter):
    name = "opencode"

    def __init__(self, config: HarnessConfig):
        self.config = config

    def manifest(self) -> RuntimeManifest:
        return RuntimeManifest(
            id=self.name,
            capabilities=("reason.general", "coding", "research", "expert-routing", "tools"),
            auth_mechanisms=("api-key", "oauth", "env", "user-authorized"),
            auth_configured=True,
            cost_class=COST_MIXED,
            privacy_class="external",
            models=(),
            health_probe="status()",
            execution_contract="opencode run --format json (private-file prompt)",
        )

    def status(self) -> EngineStatus:
        available = self.config.executable_available("opencode")
        detail = "headless JSON runner ready" if available else f"missing: {self.config.opencode_bin}"
        return EngineStatus(
            self.name, available, available, True,
            CapabilityStatus.ACTIVE if available else CapabilityStatus.IMPLEMENTED,
            detail, ("coding", "research", "expert-routing", "tools"),
            "private-file",
        )

    def run(self, request: RunRequest) -> EngineResult:
        started = time.monotonic()
        model = request.model or self.config.free_model
        agent = request.agent or self.config.default_agent
        if not self.status().available:
            return EngineResult(
                self.name, False, error="OpenCode executable is unavailable",
                error_code="unavailable", exit_code=1,
                duration=time.monotonic() - started,
            )
        try:
            cwd, cwd_identity = prepare_working_directory(
                request.cwd, getattr(request, "cwd_identity", None),
            )
            argv = [*self.config.command("opencode"), "run", "--format", "json"]
            if agent:
                argv += ["--agent", agent]
            if model:
                argv += ["-m", model]
            if request.untrusted:
                argv.append("--pure")
            argv += ["--dir", cwd]
            temp_dir = ensure_private_dir(os.path.join(str(self.config.state_root), "tmp"))
            with PrivateTempFile(temp_dir, request.prompt.encode("utf-8")) as prompt_file:
                command = [
                    *argv,
                    "Execute the complete user request in the attached private task file.",
                    "--file", prompt_file,
                ]
                env = self.config.clean_env("opencode", provider=request.provider, model=model)
                proc = run_process(ProcessRequest(
                    tuple(command), env=env, cwd=cwd, cwd_identity=cwd_identity,
                    timeout=request.timeout,
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
            return EngineResult(
                self.name, False, error=f"timed out after {request.timeout}s",
                error_code="timeout", exit_code=124, duration=proc.duration,
                metadata={"execution_config_sha256": proc.config_fingerprint},
            )
        if proc.output_limited:
            return EngineResult(
                self.name, False, text=proc.stdout,
                error="provider output exceeded the configured byte limit",
                error_code="output_limit", exit_code=125, duration=proc.duration,
                metadata={"execution_config_sha256": proc.config_fingerprint},
            )
        parsed = parse_text("opencode", proc.stdout or "", strict=True)
        error = parsed.error
        code = parsed.error_code
        if proc.returncode != 0 and not error:
            error = (proc.stderr or "OpenCode exited unsuccessfully").strip()[-800:]
            code = "process_error"
        success = proc.returncode == 0 and parsed.success
        if not success and not error and not parsed.saw_terminal:
            error, code = "OpenCode event stream ended without a terminal event", "truncated_stream"
        return EngineResult(
            self.name, success, parsed.text, error, code,
            0 if success else (proc.returncode or 1), time.monotonic() - started,
            parsed.session_id, parsed.raw_event_count,
            {
                "agent": agent, "model": model,
                "malformed_events": parsed.malformed_count,
                "execution_config_sha256": proc.config_fingerprint,
            },
        )


class ZenAdapter(OpenCodeAdapter):
    name = "zen"
    DEFAULT_MODEL = "opencode/claude-sonnet-5"

    def manifest(self) -> RuntimeManifest:
        key = bool(self.config.credential("OPENCODE_API_KEY"))
        return RuntimeManifest(
            id=self.name,
            capabilities=("reason.general", "coding", "research", "curated-models"),
            auth_mechanisms=("api-key",),
            auth_configured=key,
            cost_class="free",
            privacy_class="external",
            models=ZEN_FREE_MODELS,
            health_probe="status()",
            execution_contract="opencode run --format json via Zen provider",
            evidence=("catalog discovered from installed runtime 2026-08-14",),
        )

    def status(self) -> EngineStatus:
        base = super().status()
        key = bool(self.config.credential("OPENCODE_API_KEY"))
        healthy = base.healthy and key
        return EngineStatus(
            self.name, base.available, healthy, key,
            CapabilityStatus.ACTIVE if healthy else CapabilityStatus.IMPLEMENTED,
            "Zen key configured" if key else "Zen key not configured",
            ("curated-models", "coding", "research"),
            "private-file",
        )

    def run(self, request: RunRequest) -> EngineResult:
        if not self.config.credential("OPENCODE_API_KEY"):
            return EngineResult(self.name, False, error="OpenCode Zen key is not configured", error_code="missing_credential", exit_code=1)
        model = request.model or self.DEFAULT_MODEL
        if not model.startswith("opencode/"):
            return EngineResult(self.name, False, error="Zen model must use the opencode/<model> namespace", error_code="invalid_model", exit_code=2)
        routed = replace(request, model=model, provider="opencode")
        result = super().run(routed)
        result.engine = self.name
        return result
