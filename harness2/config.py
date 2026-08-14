"""Portable paths, executable discovery and least-leak engine environments."""

from __future__ import annotations

import hashlib
import json
import os
import ntpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from .platforms import PlatformInfo, PlatformKind, detect_platform
from .execution import MAX_HTTP_BODY_BYTES, MAX_OUTPUT_BYTES
from .security import ensure_private_dir
from . import secrets as secret_store


@dataclass(frozen=True)
class HarnessConfig:
    platform: PlatformInfo = field(default_factory=detect_platform)
    state_root: Optional[str] = None
    prime_repo: Optional[str] = None
    opencode_bin: Optional[str] = None
    hermes_bin: Optional[str] = None
    prime_bin: Optional[str] = None
    node_bin: Optional[str] = None
    python_bin: Optional[str] = None
    openssl_bin: Optional[str] = None
    stdout_limit: Optional[int] = None
    stderr_limit: Optional[int] = None
    http_body_limit: Optional[int] = None
    local_url: Optional[str] = None
    free_model: Optional[str] = None
    default_agent: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "local_url", self.local_url or self.platform.env.get(
            "HARNESS_LOCAL_URL", "http://127.0.0.1:8080/v1",
        ))
        object.__setattr__(self, "free_model", self.free_model or self.platform.env.get("HARNESS_DEFAULT_MODEL") or None)
        object.__setattr__(self, "default_agent", self.default_agent or self.platform.env.get("HARNESS_DEFAULT_AGENT") or None)
        state = str(Path(self.state_root).expanduser()) if self.state_root else str(self.platform.state_dir)
        object.__setattr__(self, "state_root", ntpath.normpath(state) if self.platform.is_windows else os.path.abspath(state))
        prime = self._configured_executable("prime", self.prime_bin)
        object.__setattr__(self, "prime_bin", prime)
        repo_override = self.prime_repo or self.platform.env.get("HARNESS_PRIME_REPO")
        if repo_override:
            repo = ntpath.normpath(repo_override) if self.platform.is_windows else os.path.abspath(os.path.expanduser(repo_override))
        elif prime and os.path.basename(prime).lower().startswith("prime-agent"):
            parent = os.path.dirname(prime)
            repo = parent if os.path.basename(parent) == "prime-agent" else str(self.platform.home / "prime-agent")
        else:
            repo = str(self.platform.home / "prime-agent")
        object.__setattr__(self, "prime_repo", repo)
        object.__setattr__(self, "opencode_bin", self._configured_executable("opencode", self.opencode_bin))
        object.__setattr__(self, "hermes_bin", self._configured_executable("hermes", self.hermes_bin))
        object.__setattr__(self, "node_bin", self._configured_executable("node", self.node_bin))
        configured_python = self.python_bin or self.platform.env.get("HARNESS_PYTHON_BIN")
        if configured_python:
            python = os.path.abspath(os.path.expanduser(configured_python))
            if not os.path.isfile(python):
                python = self.platform.discover("python") or os.sys.executable
        else:
            # The interpreter running Harness is authoritative. This preserves
            # venv/pipx installs when a different system Python appears first
            # on PATH and ensures detached workers can import this package.
            python = os.path.abspath(os.sys.executable)
        object.__setattr__(self, "python_bin", python)
        object.__setattr__(self, "openssl_bin", self._configured_executable("openssl", self.openssl_bin))
        object.__setattr__(self, "stdout_limit", self._execution_limit(
            "HARNESS_MAX_STDOUT_BYTES", self.stdout_limit, 16 * 1024 * 1024, MAX_OUTPUT_BYTES,
        ))
        object.__setattr__(self, "stderr_limit", self._execution_limit(
            "HARNESS_MAX_STDERR_BYTES", self.stderr_limit, 2 * 1024 * 1024, MAX_OUTPUT_BYTES,
        ))
        object.__setattr__(self, "http_body_limit", self._execution_limit(
            "HARNESS_MAX_HTTP_BODY_BYTES", self.http_body_limit, 4 * 1024 * 1024, MAX_HTTP_BODY_BYTES,
        ))

    def _configured_executable(self, name: str, value: Optional[str]) -> Optional[str]:
        if not value:
            return self.platform.discover(name)
        resolved = self.platform.executable(value)
        if resolved:
            return resolved
        expanded = os.path.expandvars(os.path.expanduser(value))
        return ntpath.normpath(expanded) if self.platform.is_windows else os.path.abspath(expanded)

    def _execution_limit(self, env_name: str, explicit: Optional[int], default: int, maximum: int) -> int:
        raw = explicit if explicit is not None else self.platform.env.get(env_name, default)
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{env_name} must be an integer") from exc
        if value < 1024 or value > maximum:
            raise ValueError(f"{env_name} must be between 1024 and {maximum}")
        return value

    @property
    def package_root(self) -> str:
        return str(Path(__file__).resolve().parent.parent)

    @property
    def harness_launcher(self) -> Optional[str]:
        override = self.platform.env.get("HARNESS_LAUNCHER")
        if override:
            return self.platform.executable(override)
        candidate = self.platform.discover("harness")
        if candidate:
            return candidate
        argv0 = os.path.abspath(os.path.expanduser(os.sys.argv[0]))
        if os.path.basename(argv0).lower() in {"harness", "harness.exe"} and os.path.isfile(argv0):
            return self.platform.executable(argv0)
        checkout = os.path.join(self.package_root, "bin", "harness")
        return self.platform.executable(checkout)

    @property
    def secrets_path(self) -> str:
        return self._join(str(self.state_root), "secrets.dpapi" if self.platform.is_windows else "secrets.json")

    @property
    def database_path(self) -> str:
        return self._join(str(self.state_root), "harness.db")

    @property
    def integrity_manifest(self) -> str:
        return self._join(str(self.state_root), "integrity.json")

    @property
    def prime_run_dir(self) -> str:
        return self._join(str(self.state_root), "run", "prime")

    @property
    def context_root(self) -> str:
        return self._join(str(self.state_root), "contexts")

    @property
    def context_jobs_dir(self) -> str:
        return self._join(str(self.state_root), "context-jobs")

    @property
    def object_store_root(self) -> str:
        return self._join(str(self.state_root), "objects")

    @property
    def object_store_key(self) -> str:
        return self._join(str(self.state_root), "object-store.key")

    @property
    def service_heartbeat(self) -> str:
        return self._join(str(self.state_root), "run", "service-heartbeat.json")

    @property
    def always_active_default(self) -> bool:
        value = self.platform.env.get("HARNESS_ALWAYS_ACTIVE", "true")
        return str(value).strip().lower() not in {"0", "false", "no", "off"}

    @property
    def prime_socket(self) -> str:
        if self.platform.is_windows:
            # Prime owns its Windows daemon endpoint; harness does not invent a
            # Unix-socket path on that platform.
            return ""
        return self._join(self.prime_run_dir, "daemon.sock")

    @property
    def prime_bundle(self) -> str:
        return self._join(str(self.prime_repo), "packages", "coding-agent", "dist", "bundle", "cli.js")

    @property
    def prime_launcher(self) -> Optional[str]:
        if self.prime_bin:
            return self.prime_bin
        candidate = os.path.join(str(self.prime_repo), "prime-agent.cmd" if self.platform.is_windows else "prime-agent.sh")
        return self.platform.executable(candidate)

    @property
    def prime_wrapper(self) -> str:
        return os.path.join(self.package_root, "harness2", "prime_wrapper.py")

    def _join(self, *parts: str) -> str:
        return (ntpath.join if self.platform.is_windows else os.path.join)(*parts)

    @property
    def hardened_prime_available(self) -> bool:
        return (
            self.platform.kind in {PlatformKind.LINUX, PlatformKind.TERMUX, PlatformKind.PROOT}
            and self.executable_available("node") and os.path.isfile(self.prime_bundle)
            and os.path.isfile(self.prime_wrapper)
        )

    def ensure(self) -> None:
        ensure_private_dir(str(self.state_root))
        ensure_private_dir(os.path.join(str(self.state_root), "run"))
        ensure_private_dir(self.prime_run_dir)
        ensure_private_dir(os.path.join(str(self.state_root), "logs"))
        ensure_private_dir(self.context_root)
        ensure_private_dir(self.context_jobs_dir)
        ensure_private_dir(self.object_store_root)

    def secrets(self) -> Dict[str, str]:
        return secret_store.load(self.secrets_path, windows=self.platform.is_windows)

    def save_secrets(self, values: Dict[str, str]) -> None:
        secret_store.save(self.secrets_path, values, windows=self.platform.is_windows)

    def credential(self, name: str) -> Optional[str]:
        return self.secrets().get(name) or self.platform.credentials.get(name)

    def command(self, name: str) -> list[str]:
        executable = {
            "opencode": self.opencode_bin,
            "prime": self.prime_launcher,
            "hermes": self.hermes_bin,
            "node": self.node_bin,
            "python": self.python_bin,
            "openssl": self.openssl_bin,
        }.get(name)
        executable = self.platform.executable(executable) if executable else None
        if not executable:
            raise FileNotFoundError(f"{name} executable is unavailable")
        return self.platform.command_prefix(executable)

    def executable_available(self, name: str) -> bool:
        try:
            self.command(name)
        except FileNotFoundError:
            return False
        return True

    def execution_profile(self) -> Dict[str, object]:
        values: Dict[str, object] = {
            "schema": "harness.execution-profile/v1",
            "platform": self.platform.platform_id,
            "state_root": str(self.state_root),
            "local_url": self.local_url,
            "default_agent": self.default_agent,
            "default_model": self.free_model,
            "limits": {
                "stdout_bytes": self.stdout_limit,
                "stderr_bytes": self.stderr_limit,
                "http_body_bytes": self.http_body_limit,
            },
            "executables": {
                name: executable for name, executable in {
                    "opencode": self.opencode_bin,
                    "prime": self.prime_launcher,
                    "hermes": self.hermes_bin,
                    "node": self.node_bin,
                    "python": self.python_bin,
                    "openssl": self.openssl_bin,
                }.items() if executable
            },
        }
        encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        values["sha256"] = hashlib.sha256(encoded).hexdigest()
        return values

    def clean_env(
        self,
        engine: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        daemon: bool = False,
    ) -> Dict[str, str]:
        directories: list[str] = []
        def add_directory(raw: str) -> None:
            if not raw:
                return
            try:
                value = os.path.realpath(os.path.expanduser(raw))
            except (OSError, TypeError, ValueError):
                return
            if os.path.isdir(value) and value not in directories:
                directories.append(value)

        for value in (self.opencode_bin, self.prime_launcher, self.hermes_bin, self.node_bin, self.python_bin):
            if value:
                add_directory(os.path.dirname(value))
        inherited_path = self.platform.env["PATH"] if "PATH" in self.platform.env else os.defpath
        for item in inherited_path.split(os.pathsep):
            add_directory(item)
        if self.platform.kind == PlatformKind.PROOT:
            for item in ("/usr/local/bin", "/usr/bin", "/bin"):
                add_directory(item)
        ensure_private_dir(str(self.state_root))
        temp_dir = ensure_private_dir(os.path.join(str(self.state_root), "tmp"))
        env = {
            "PATH": os.pathsep.join(directories),
            "HOME": str(self.platform.home),
            "DO_NOT_TRACK": "1",
            "PRIME_AGENT_TELEMETRY": "0",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        allowed_environment = (
            "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "WINDIR", "COMSPEC",
            "PATHEXT", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "NO_PROXY",
            "PREFIX", "TERMUX_VERSION", "PROOT_L2S_DIR", "PROOT_TMP_DIR", "PROOT_DISTRO",
        )
        for key in allowed_environment:
            value = self.platform.env.get(key)
            if value:
                env[key] = value
        if self.platform.is_windows:
            env["TEMP"] = temp_dir
            env["TMP"] = temp_dir
        else:
            env["TMPDIR"] = temp_dir
            env.setdefault("LANG", "C")
            env.setdefault("LC_ALL", "C")
        if engine in {"hermes", "local"}:
            return env
        keys = self.secrets()
        selected = provider or ((model or "").split("/", 1)[0] if "/" in (model or "") else "")
        mapping = {
            "opencode": "OPENCODE_API_KEY", "openai": "OPENAI_API_KEY",
            "google": "GEMINI_API_KEY", "gemini": "GEMINI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }
        if daemon and engine == "prime":
            providers = self.platform.env.get("HARNESS_PRIME_PROVIDERS", "openai").split(",")
            allowed = {mapping[p.strip()] for p in providers if p.strip() in mapping}
        else:
            allowed = set(mapping.values()) if daemon or not selected else ({mapping[selected]} if selected in mapping else set())
        for name in allowed:
            value = self.credential(name)
            if value:
                env[name] = value
        return env
