"""Read-only diagnostics and integrity checks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from .config import HarnessConfig
from .registry import build_registry
from .security import private_mode
from .store import Store
from .service import active_status
from .execution import ProcessRequest, ProcessSpawnError, run_process, secret_environment_keys


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "error"

    def as_dict(self):
        return asdict(self)


def _version(name: str, argv: list[str], env: Dict[str, str]) -> Check:
    try:
        proc = run_process(ProcessRequest(
            tuple(argv), env=env, cwd=os.getcwd(), timeout=20,
            stdout_limit=64 * 1024, stderr_limit=64 * 1024,
            secret_env_keys=secret_environment_keys(env),
        ))
        value = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr).strip() else "no output"
        ok = proc.returncode == 0 and not proc.timed_out and not proc.output_limited
        return Check(name, ok, value[:200])
    except (OSError, ValueError, ProcessSpawnError) as exc:
        return Check(name, False, str(exc))


def _optional_version(name: str, executable: Optional[str], env: Dict[str, str], config: HarnessConfig) -> Check:
    if not executable:
        return Check(name, True, "optional provider dependency unavailable", "warning")
    return _version_with_severity(
        name, config.platform.command_prefix(executable) + ["--version"], env, "warning",
    )


def _version_with_severity(name: str, argv: list[str], env: Dict[str, str], severity: str) -> Check:
    value = _version(name, argv, env)
    return Check(value.name, value.ok, value.detail, severity)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_integrity_manifest(path: str) -> tuple[Optional[Dict[str, str]], Optional[str]]:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return None, "integrity manifest missing"
    except (OSError, ValueError, TypeError) as exc:
        return None, f"integrity manifest unreadable: {exc}"
    artifacts = raw.get("artifacts") if isinstance(raw, dict) else None
    if not isinstance(artifacts, dict) or not artifacts:
        return None, "integrity manifest has no artifacts"
    values: Dict[str, str] = {}
    for name, digest in artifacts.items():
        if not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 64:
            return None, "integrity manifest has an invalid artifact entry"
        try:
            int(digest, 16)
        except ValueError:
            return None, "integrity manifest has an invalid SHA-256 digest"
        values[name] = digest.lower()
    return values, None


def integrity_artifacts(config: HarnessConfig) -> Dict[str, str]:
    values = {
        "harness.module": os.path.join(config.package_root, "harness2", "__init__.py"),
    }
    if config.harness_launcher:
        values["harness.launcher"] = config.harness_launcher
    if os.path.isfile(config.prime_wrapper) and os.path.isfile(config.prime_bundle):
        values["harness.prime_wrapper"] = config.prime_wrapper
        values["prime.bundle"] = config.prime_bundle
    return values


def core_integrity_artifacts(config: HarnessConfig) -> Dict[str, str]:
    package = os.path.join(config.package_root, "harness2")
    values: Dict[str, str] = {}
    if os.path.isdir(package):
        for root, dirs, files in os.walk(package):
            dirs[:] = sorted(item for item in dirs if item != "__pycache__")
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                relative = os.path.relpath(path, config.package_root).replace(os.sep, "/")
                values[f"harness.source:{relative}"] = path
    return values


def _integrity_checks(config: HarnessConfig) -> List[Check]:
    expected, error = load_integrity_manifest(config.integrity_manifest)
    if error:
        return [Check("integrity.manifest", False, error, "critical")]
    assert expected is not None
    paths = {**core_integrity_artifacts(config), **integrity_artifacts(config)}
    checks: List[Check] = []
    for name, pinned in expected.items():
        path = paths.get(name)
        if not path:
            checks.append(Check(name, False, "pinned artifact is not recognized", "critical"))
            continue
        try:
            actual = _sha256(path)
        except OSError as exc:
            checks.append(Check(name, False, f"artifact unavailable: {exc}", "critical"))
            continue
        checks.append(Check(name, actual == pinned, f"sha256:{actual}" + ("" if actual == pinned else f", expected:{pinned}"), "critical"))
    missing = sorted(set(paths) - set(expected))
    if missing:
        checks.append(Check("integrity.manifest", False, "missing pins: " + ", ".join(missing), "critical"))
    else:
        checks.append(Check("integrity.manifest", True, f"verified pins={len(expected)}", "critical"))
    return checks


def _external_guardian_check(state: str, process_marker: str) -> Check:
    try:
        with open(state, encoding="utf-8") as fh:
            payload = json.load(fh)
        last_scan = payload.get("last_scan") if isinstance(payload, dict) else None
        if not isinstance(last_scan, str):
            raise ValueError("last_scan missing")
        heartbeat = time.mktime(time.strptime(last_scan, "%Y-%m-%d %H:%M:%S"))
        age = time.time() - heartbeat
    except OSError:
        return Check("external.guardian", False, "guardian state missing", "critical")
    except (ValueError, TypeError) as exc:
        return Check("external.guardian", False, f"guardian state invalid: {exc}", "critical")
    alive = False
    try:
        proc = subprocess.run(["ps", "-eo", "args="], capture_output=True, text=True, timeout=10)
        alive = any(process_marker in line for line in proc.stdout.splitlines())
    except Exception:
        pass
    # A normal scan can take several minutes on the protected Android SDK and
    # archive trees. The guardian loop independently detects a truly hung scan
    # after its longer grace/staleness window.
    ok = alive and age <= 300
    return Check("external.guardian", ok, f"process={'up' if alive else 'down'}, heartbeat_age={int(age)}s", "critical")


def run_checks(config: HarnessConfig, store: Store) -> List[Check]:
    env = config.clean_env("local")
    python = config.python_bin or "python"
    checks: List[Check] = [
        _version("python", config.platform.command_prefix(python) + ["--version"], env),
        _optional_version("node", config.node_bin, env, config),
        _optional_version("opencode", config.opencode_bin, env, config),
        *_integrity_checks(config),
    ]
    for path in (config.state_root, config.database_path):
        checks.append(Check(f"mode:{path}", private_mode(path), oct(stat.S_IMODE(os.lstat(path).st_mode)) if os.path.lexists(path) else "missing"))
    ok, count, bad = store.verify_audit()
    checks.append(Check("audit.chain", ok, f"entries={count}" + (f", bad_seq={bad}" if bad else ""), "critical"))
    try:
        usage = shutil.disk_usage(config.state_root)
        free = usage.free / (1024 ** 3)
        checks.append(Check("disk.free", free >= 2, f"{free:.1f} GiB free", "warning" if free >= 2 else "critical"))
    except Exception as exc:
        checks.append(Check("disk.free", False, str(exc)))
    guardian_state = os.environ.get("HARNESS_GUARDIAN_STATE")
    guardian_marker = os.environ.get("HARNESS_GUARDIAN_PROCESS")
    if guardian_state and guardian_marker:
        checks.append(_external_guardian_check(guardian_state, guardian_marker))
    active = active_status(config)
    checks.append(Check(
        "runtime.always_active",
        bool(active["active"]) if active["desired_always_active"] else True,
        f"desired={active['desired_always_active']}, observed={active['observed_state']}, cycles={active['cycles']}",
        "critical" if active["desired_always_active"] else "warning",
    ))
    if os.path.isdir(os.path.join(str(config.prime_repo), ".git")):
        try:
            proc = subprocess.run(
                ["git", "-C", str(config.prime_repo), "status", "--porcelain"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            dirty = [line for line in proc.stdout.splitlines() if line.strip()]
            checks.append(Check("prime.source", proc.returncode == 0 and not dirty, "clean" if not dirty else f"dirty files={len(dirty)}", "warning"))
        except Exception as exc:
            checks.append(Check("prime.source", False, str(exc), "warning"))
    for name, adapter in build_registry(config, store).items():
        status = adapter.status()
        expected = status.healthy if status.status.value == "active" else status.available
        checks.append(Check(f"engine:{name}", expected, f"{status.status.value}: {status.detail}", "warning"))
    return checks


def summarize(checks: List[Check]) -> Dict[str, Any]:
    failures = [item for item in checks if not item.ok]
    return {
        "ok": not any(item.severity in ("error", "critical") for item in failures),
        "checks": [item.as_dict() for item in checks],
        "failures": len(failures),
    }
