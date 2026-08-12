"""PRoot supervisor loop for Prime and durable queue workers."""

from __future__ import annotations

import os
import signal
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .adapters.prime import PrimeAdapter
from .config import HarnessConfig
from .context.jobs import ContextJobManager
from .jobs import JobManager
from .security import atomic_write_json, ensure_private_dir, read_private_json, redact
from .store import Store
from . import supervisor


def rotate(path: str, max_bytes: int = 5 * 1024 * 1024, keep: int = 3) -> bool:
    try:
        if os.path.getsize(path) < max_bytes:
            return False
    except OSError:
        return False
    for index in range(keep - 1, 0, -1):
        src, dst = f"{path}.{index}", f"{path}.{index + 1}"
        if os.path.exists(src):
            os.replace(src, dst)
    os.replace(path, path + ".1")
    return True


@dataclass
class ServiceLoop:
    config: HarnessConfig
    store: Store
    prime: PrimeAdapter
    jobs: JobManager
    context_jobs: Optional[ContextJobManager] = None
    interval: int = 30
    running: bool = True
    boot_id: str = ""
    cycles: int = 0

    def __post_init__(self) -> None:
        if self.context_jobs is None:
            self.context_jobs = ContextJobManager(self.config, self.store)
        if not self.boot_id:
            self.boot_id = uuid.uuid4().hex

    @classmethod
    def bootstrap(
        cls,
        config: HarnessConfig,
        store: Store,
        prime: PrimeAdapter,
        jobs: JobManager,
        context_jobs: Optional[ContextJobManager] = None,
        interval: int = 30,
    ) -> "ServiceLoop":
        loop = cls(config, store, prime, jobs, context_jobs, interval=interval)
        loop._heartbeat("starting", None, None, None)
        return loop

    def stop(self, *_):
        self.running = False

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log_path = os.path.join(self.config.state_root, "logs", "service.log")
        failures = 0
        next_prime_attempt = 0.0
        self._heartbeat("active", None, None, None)
        while self.running:
            rotate(log_path)
            now = time.time()
            try:
                if self.config.hardened_prime_available:
                    status = self.prime.daemon_status()
                    if not status.healthy and now >= next_prime_attempt:
                        try:
                            self.prime.start(wait_socket=60)
                            failures = 0
                            self.store.append_audit("service.prime.started", "prime", {})
                        except Exception as exc:
                            failures += 1
                            delay = min(300, 2 ** min(failures, 8))
                            next_prime_attempt = now + delay
                            self.store.append_audit("service.prime.failed", "prime", {"backoff": delay, "error": exc})
                result = self.jobs.work_once()
                work_type = "run_job" if result else None
                work_id = result["id"] if result else None
                if result:
                    self.store.append_audit("service.job.worked", result["id"], {"status": result["status"]})
                context_result = self.context_jobs.work_once() if self.context_jobs else None
                if context_result:
                    work_type = "context_compile"
                    work_id = context_result["id"]
                self.cycles += 1
                self._heartbeat("active", work_type, work_id, None)
            except Exception as exc:
                self.store.append_audit("service.loop.error", "supervisor", {"error": exc})
                self.cycles += 1
                self._heartbeat("degraded", None, None, exc)
            time.sleep(max(1, self.interval))
        self._heartbeat("stopped", None, None, None)

    def _heartbeat(self, observed: str, work_type: Optional[str], work_id: Optional[str], error: Any) -> None:
        atomic_write_json(self.config.service_heartbeat, {
            "schema": "harness.service-heartbeat/v1",
            "service_pid": os.getpid(),
            "boot_id": self.boot_id,
            "desired_always_active": self.config.always_active_default,
            "observed_state": observed,
            "cycles": self.cycles,
            "heartbeat_at": time.time(),
            "last_work_type": work_type,
            "last_work_id": work_id,
            "last_error": redact(error or "", 200),
        })


def active_status(config: HarnessConfig, *, freshness: int = 120) -> Dict[str, Any]:
    heartbeat = read_private_json(config.service_heartbeat)
    pidfile = os.path.join(config.state_root, "run", "service.pid")
    try:
        pid = supervisor.read_pidfile(pidfile)
    except Exception:
        pid = None
    exact = bool(pid and service_process_matches(config, pid))
    heartbeat_pid = heartbeat.get("service_pid") if heartbeat else None
    age = max(0.0, time.time() - float(heartbeat.get("heartbeat_at", 0))) if heartbeat else None
    fresh = bool(heartbeat and heartbeat_pid == pid and age is not None and age <= freshness)
    active = bool(config.always_active_default and exact and fresh and heartbeat.get("observed_state") in {"active", "degraded"})
    if active:
        state = "active" if heartbeat.get("observed_state") == "active" else "degraded"
    elif exact and heartbeat:
        state = "stale"
    elif config.always_active_default:
        state = "configured"
    else:
        state = "disabled"
    return {
        "desired_always_active": config.always_active_default,
        "observed_state": state,
        "active": active,
        "service_pid": pid,
        "process_verified": exact,
        "heartbeat_fresh": fresh,
        "heartbeat_age": int(age) if age is not None else None,
        "cycles": int(heartbeat.get("cycles", 0)) if heartbeat else 0,
        "last_work_type": heartbeat.get("last_work_type") if heartbeat else None,
        "last_work_id": heartbeat.get("last_work_id") if heartbeat else None,
        "last_error": heartbeat.get("last_error", "") if heartbeat else "",
    }


def service_process_matches(config: HarnessConfig, pid: int) -> bool:
    argv = supervisor.read_cmdline(pid)
    if not argv or not supervisor.pid_alive(pid):
        return False
    try:
        module_index = argv.index("-m")
    except ValueError:
        module_index = -1
    if module_index >= 0 and argv[module_index + 1:module_index + 3] == ["harness2", "supervise"]:
        module_root = os.path.realpath(config.package_root)
        try:
            process_root = os.path.realpath(f"/proc/{pid}/cwd")
        except OSError:
            process_root = ""
        return process_root == module_root
    launcher = config.harness_launcher
    return bool(
        launcher and len(argv) >= 3
        and os.path.realpath(argv[0]) == os.path.realpath(config.python_bin or "")
        and os.path.realpath(argv[1]) == os.path.realpath(launcher)
        and argv[2] == "supervise"
    )
