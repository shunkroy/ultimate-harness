"""Prime Agent durable/RLM worker adapter."""

from __future__ import annotations

import os
import time

from .base import EngineAdapter
from ..config import HarnessConfig
from ..events import parse_text
from ..execution import (
    ProcessConfigurationError,
    ProcessRequest,
    ProcessResult,
    ProcessSpawnError,
    canonical_working_directory,
    prepare_working_directory,
    run_process,
    secret_environment_keys,
)
from ..models import CapabilityStatus, EngineResult, EngineStatus, RunRequest
from .. import supervisor
from ..security import PrivateTempFile, ensure_private_dir


class PrimeAdapter(EngineAdapter):
    name = "prime"

    def __init__(self, config: HarnessConfig):
        self.config = config

    def daemon_status(self) -> supervisor.DaemonStatus:
        if not self.config.hardened_prime_available:
            raise supervisor.SupervisorError(
                f"hardened Prime supervision is unavailable on {self.config.platform.kind.value}; "
                "Prime one-shot mode remains available through its native CLI"
            )
        self.config.ensure()
        return supervisor.status(
            run_dir=self.config.prime_run_dir,
            bundle_path=self.config.prime_wrapper,
            socket_path=self.config.prime_socket,
            name="prime",
        )

    def status(self) -> EngineStatus:
        installed = self.config.executable_available("prime")
        try:
            daemon = self.daemon_status() if self.config.hardened_prime_available else None
            healthy = bool(daemon and daemon.healthy)
            detail = f"daemon pid {daemon.pid}" if healthy else (
                "installed; native lifecycle" if installed and not self.config.hardened_prime_available
                else "installed; daemon down"
            )
        except Exception as exc:
            healthy, detail = False, f"supervisor error: {exc}"
        return EngineStatus(
            self.name, installed, healthy, installed,
            CapabilityStatus.ACTIVE if healthy else (CapabilityStatus.IMPLEMENTED if installed else CapabilityStatus.DOCUMENTED),
            detail, ("durable-sessions", "ipython-kernel", "recursive-subagents", "scheduling"),
            "private-file",
        )

    def start(self, wait_socket: float = 60.0) -> supervisor.DaemonStatus:
        if not self.config.hardened_prime_available:
            raise supervisor.SupervisorError(
                "hardened Prime daemon supervision requires a POSIX platform and source dist bundle"
            )
        self.config.ensure()
        return supervisor.start_daemon(
            bundle_path=self.config.prime_wrapper,
            run_dir=self.config.prime_run_dir,
            socket_path=self.config.prime_socket,
            node=self.config.python_bin,
            extra_args=(
                "--bundle", self.config.prime_bundle, "--node", str(self.config.node_bin),
                "--cwd", str(self.config.prime_repo),
            ),
            wait_socket=wait_socket,
            env=self.config.clean_env("prime", daemon=True),
            cwd=self.config.prime_repo,
            name="prime",
        )

    def stop(self) -> supervisor.DaemonStatus:
        if not self.config.hardened_prime_available:
            raise supervisor.SupervisorError("Prime daemon is managed by its native CLI on this platform")
        return supervisor.stop_daemon(
            run_dir=self.config.prime_run_dir,
            bundle_path=self.config.prime_wrapper,
            socket_path=self.config.prime_socket,
            force=True,
            name="prime",
        )

    def run(self, request: RunRequest) -> EngineResult:
        started = time.monotonic()
        if not self.status().available:
            return EngineResult(
                self.name, False, error="Prime CLI is unavailable",
                error_code="unavailable", exit_code=1,
                duration=time.monotonic() - started,
            )
        try:
            requested_cwd, requested_identity = prepare_working_directory(
                request.cwd, getattr(request, "cwd_identity", None),
            )
        except ProcessConfigurationError as exc:
            return EngineResult(
                self.name, False, error=str(exc), error_code="invalid_execution",
                exit_code=2, duration=time.monotonic() - started,
            )
        if self.config.hardened_prime_available:
            try:
                daemon = self.daemon_status()
                if not daemon.healthy:
                    self.start()
            except Exception as exc:
                return EngineResult(self.name, False, error=f"Prime daemon start failed: {exc}", error_code="daemon_unavailable", exit_code=1, duration=time.monotonic() - started)
        try:
            process_cwd, process_identity = prepare_working_directory(
                str(self.config.prime_repo)
                if os.path.isdir(str(self.config.prime_repo)) else requested_cwd
            )
            argv = [*self.config.command("prime")]
            if self.config.hardened_prime_available:
                argv.append("--dist")
            argv += ["-p", "--mode", "json"]
            if request.provider:
                argv += ["--provider", request.provider]
            if request.model:
                argv += ["--model", request.model]
            argv += ["--cwd", requested_cwd]
            if self.config.hardened_prime_available:
                argv += ["--daemon-socket", self.config.prime_socket]
            temp_dir = ensure_private_dir(os.path.join(str(self.config.state_root), "tmp"))
            with PrivateTempFile(temp_dir, request.prompt.encode("utf-8")) as prompt_file:
                command = [
                    *argv, "@" + prompt_file,
                    "Execute the complete user request in the attached private task file.",
                ]
                env = self.config.clean_env(
                    "prime", provider=request.provider, model=request.model, daemon=True,
                )
                proc = run_process(ProcessRequest(
                    tuple(command), cwd=process_cwd, cwd_identity=process_identity, env=env,
                    timeout=request.timeout,
                    stdout_limit=int(self.config.stdout_limit),
                    stderr_limit=int(self.config.stderr_limit),
                    private_argv_indices=(len(command) - 2,),
                    secret_env_keys=secret_environment_keys(env),
                    additional_cwd_authorities=((requested_cwd, requested_identity),),
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
        parsed = parse_text("prime", proc.stdout or "", strict=True)
        error, code = parsed.error, parsed.error_code
        if proc.returncode != 0 and not error:
            error, code = (proc.stderr or "Prime exited unsuccessfully").strip()[-800:], "process_error"
        success = proc.returncode == 0 and parsed.success
        if not success and not error and not parsed.saw_terminal:
            error, code = "Prime event stream ended without agent_end", "truncated_stream"
        return EngineResult(
            self.name, success, parsed.text, error, code,
            0 if success else (proc.returncode or 1), time.monotonic() - started,
            parsed.session_id, parsed.raw_event_count,
            {
                "malformed_events": parsed.malformed_count,
                "execution_config_sha256": proc.config_fingerprint,
            },
        )

    def passthrough(self, args: list[str], timeout: int = 180) -> ProcessResult:
        if not self.config.prime_launcher:
            raise FileNotFoundError("Prime CLI is unavailable")
        argv = [*self.config.command("prime")]
        if self.config.hardened_prime_available:
            argv += ["--dist", *args, "--daemon-socket", self.config.prime_socket]
        else:
            argv += args
        cwd = canonical_working_directory(
            str(self.config.prime_repo) if os.path.isdir(str(self.config.prime_repo)) else None
        )
        env = self.config.clean_env("prime", daemon=True)
        return run_process(ProcessRequest(
            tuple(argv), cwd=cwd, env=env, timeout=timeout,
            stdout_limit=int(self.config.stdout_limit),
            stderr_limit=int(self.config.stderr_limit),
            secret_env_keys=secret_environment_keys(env),
        ))
