"""Portable paths, executable discovery and least-leak engine environments."""

from __future__ import annotations

import os
import ntpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from .platforms import PlatformInfo, PlatformKind, detect_platform
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
    local_url: str = field(default_factory=lambda: os.environ.get("HARNESS_LOCAL_URL", "http://127.0.0.1:8080/v1"))
    free_model: str = field(default_factory=lambda: os.environ.get("HARNESS_DEFAULT_MODEL", "openai/gpt-5.6-sol"))
    default_agent: str = field(default_factory=lambda: os.environ.get("HARNESS_DEFAULT_AGENT", "build"))

    def __post_init__(self) -> None:
        state = str(Path(self.state_root).expanduser()) if self.state_root else str(self.platform.state_dir)
        object.__setattr__(self, "state_root", ntpath.normpath(state) if self.platform.is_windows else os.path.abspath(state))
        prime = self.prime_bin or self.platform.discover("prime")
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
        object.__setattr__(self, "opencode_bin", self.opencode_bin or self.platform.discover("opencode"))
        object.__setattr__(self, "hermes_bin", self.hermes_bin or self.platform.discover("hermes"))
        object.__setattr__(self, "node_bin", self.node_bin or self.platform.discover("node"))
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
        object.__setattr__(self, "openssl_bin", self.openssl_bin or self.platform.discover("openssl"))

    @property
    def package_root(self) -> str:
        return str(Path(__file__).resolve().parent.parent)

    @property
    def harness_launcher(self) -> Optional[str]:
        override = self.platform.env.get("HARNESS_LAUNCHER") or os.environ.get("HARNESS_LAUNCHER")
        if override:
            path = os.path.abspath(os.path.expanduser(override))
            return path if os.path.isfile(path) else None
        candidate = self.platform.discover("harness")
        if candidate:
            return candidate
        argv0 = os.path.abspath(os.path.expanduser(os.sys.argv[0]))
        if os.path.basename(argv0).lower() in {"harness", "harness.exe"} and os.path.isfile(argv0):
            return argv0
        checkout = os.path.join(self.package_root, "bin", "harness")
        return checkout if os.path.isfile(checkout) else None

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
    def service_heartbeat(self) -> str:
        return self._join(str(self.state_root), "run", "service-heartbeat.json")

    @property
    def always_active_default(self) -> bool:
        value = self.platform.env.get("HARNESS_ALWAYS_ACTIVE") or os.environ.get("HARNESS_ALWAYS_ACTIVE", "true")
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
        return candidate if os.path.isfile(candidate) else None

    @property
    def prime_wrapper(self) -> str:
        return os.path.join(self.package_root, "harness2", "prime_wrapper.py")

    def _join(self, *parts: str) -> str:
        return (ntpath.join if self.platform.is_windows else os.path.join)(*parts)

    @property
    def hardened_prime_available(self) -> bool:
        return (
            self.platform.kind in {PlatformKind.LINUX, PlatformKind.TERMUX, PlatformKind.PROOT}
            and bool(self.node_bin) and os.path.isfile(self.prime_bundle)
            and os.path.isfile(self.prime_wrapper)
        )

    def ensure(self) -> None:
        ensure_private_dir(str(self.state_root))
        ensure_private_dir(os.path.join(str(self.state_root), "run"))
        ensure_private_dir(self.prime_run_dir)
        ensure_private_dir(os.path.join(str(self.state_root), "logs"))
        ensure_private_dir(self.context_root)
        ensure_private_dir(self.context_jobs_dir)

    def secrets(self) -> Dict[str, str]:
        return secret_store.load(self.secrets_path, windows=self.platform.is_windows)

    def save_secrets(self, values: Dict[str, str]) -> None:
        secret_store.save(self.secrets_path, values, windows=self.platform.is_windows)

    def credential(self, name: str) -> Optional[str]:
        return self.secrets().get(name) or os.environ.get(name)

    def command(self, name: str) -> list[str]:
        executable = {
            "opencode": self.opencode_bin,
            "prime": self.prime_launcher,
            "hermes": self.hermes_bin,
            "node": self.node_bin,
            "python": self.python_bin,
            "openssl": self.openssl_bin,
        }.get(name)
        if not executable:
            raise FileNotFoundError(f"{name} executable is unavailable")
        return self.platform.command_prefix(executable)

    def clean_env(
        self,
        engine: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        daemon: bool = False,
    ) -> Dict[str, str]:
        directories = []
        for value in (self.opencode_bin, self.prime_launcher, self.hermes_bin, self.node_bin, self.python_bin):
            if value:
                directory = os.path.dirname(value)
                if directory and directory not in directories:
                    directories.append(directory)
        if self.platform.kind == PlatformKind.PROOT:
            directories.extend(x for x in ("/usr/local/bin", "/usr/bin", "/bin") if x not in directories)
        else:
            for item in self.platform.env.get("PATH", os.environ.get("PATH", "")).split(os.pathsep):
                if item and item not in directories:
                    directories.append(item)
        env = {
            "PATH": os.pathsep.join(directories),
            "HOME": str(self.platform.home),
            "DO_NOT_TRACK": "1",
            "PRIME_AGENT_TELEMETRY": "0",
        }
        for key in ("LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
            value = self.platform.env.get(key) or os.environ.get(key)
            if value:
                env[key] = value
        if not self.platform.is_windows:
            env.setdefault("LANG", "C.UTF-8")
            env.setdefault("LC_ALL", "C.UTF-8")
        keys = self.secrets()
        selected = provider or ((model or "").split("/", 1)[0] if "/" in (model or "") else "")
        mapping = {
            "opencode": "OPENCODE_API_KEY", "openai": "OPENAI_API_KEY",
            "google": "GEMINI_API_KEY", "gemini": "GEMINI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }
        if engine in {"hermes", "local"}:
            return env
        if daemon and engine == "prime":
            providers = os.environ.get("HARNESS_PRIME_PROVIDERS", "openai").split(",")
            allowed = {mapping[p.strip()] for p in providers if p.strip() in mapping}
        else:
            allowed = set(mapping.values()) if daemon or not selected else ({mapping[selected]} if selected in mapping else set())
        for name in allowed:
            value = self.credential(name)
            if value:
                env[name] = value
        return env
