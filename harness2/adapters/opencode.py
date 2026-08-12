"""OpenCode and OpenCode Zen provider adapters."""

from __future__ import annotations

import os
import subprocess
import time

from .base import EngineAdapter
from ..config import HarnessConfig
from ..events import parse_text
from ..models import CapabilityStatus, EngineResult, EngineStatus, RunRequest
from ..security import PrivateTempFile, ensure_private_dir


class OpenCodeAdapter(EngineAdapter):
    name = "opencode"

    def __init__(self, config: HarnessConfig):
        self.config = config

    def status(self) -> EngineStatus:
        executable = self.config.opencode_bin
        available = bool(executable and os.path.isfile(executable))
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
        argv = [*self.config.command("opencode"), "run", "--format", "json"]
        if agent:
            argv += ["--agent", agent]
        if model:
            argv += ["-m", model]
        if request.untrusted:
            argv.append("--pure")
        if request.cwd:
            argv += ["--dir", request.cwd]
        temp_dir = ensure_private_dir(os.path.join(str(self.config.state_root), "tmp"))
        try:
            with PrivateTempFile(temp_dir, request.prompt.encode("utf-8")) as prompt_file:
                proc = subprocess.run(
                    [*argv, "Execute the complete user request in the attached private task file.", "--file", prompt_file],
                    env=self.config.clean_env("opencode", provider=request.provider, model=model),
                    cwd=request.cwd or os.getcwd(),
                    capture_output=True,
                    text=True,
                    timeout=request.timeout,
                )
        except subprocess.TimeoutExpired:
            return EngineResult(self.name, False, error=f"timed out after {request.timeout}s", error_code="timeout", exit_code=124, duration=time.monotonic() - started)
        except OSError as exc:
            return EngineResult(self.name, False, error=str(exc), error_code="spawn_error", exit_code=1, duration=time.monotonic() - started)
        parsed = parse_text("opencode", proc.stdout or "")
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
            {"agent": agent, "model": model, "malformed_events": parsed.malformed_count},
        )


class ZenAdapter(OpenCodeAdapter):
    name = "zen"
    DEFAULT_MODEL = "opencode/claude-sonnet-5"

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
        routed = RunRequest(**{**request.__dict__, "model": model, "provider": "opencode"})
        result = super().run(routed)
        result.engine = self.name
        return result
