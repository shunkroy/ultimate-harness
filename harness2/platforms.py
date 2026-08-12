"""Cross-platform discovery and process-launch primitives.

The orchestration core is shell-neutral. Platform-specific behavior is
concentrated here for Windows, macOS, Linux, native Termux and Ubuntu PRoot.
"""

from __future__ import annotations

import ipaddress
import os
import platform as _platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlparse


class PlatformKind(str, Enum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    TERMUX = "termux"
    PROOT = "proot"


EXECUTABLE_ENV = {
    "harness": "HARNESS_LAUNCHER",
    "opencode": "HARNESS_OPENCODE_BIN",
    "prime": "HARNESS_PRIME_BIN",
    "hermes": "HARNESS_HERMES_BIN",
    "node": "HARNESS_NODE_BIN",
    "python": "HARNESS_PYTHON_BIN",
    "openssl": "HARNESS_OPENSSL_BIN",
}


@dataclass(frozen=True)
class PlatformInfo:
    kind: PlatformKind
    home: Path
    state_dir: Path
    config_dir: Path
    cache_dir: Path
    runtime_dir: Path
    env: Mapping[str, str] = field(repr=False, compare=False)

    @property
    def is_windows(self) -> bool:
        return self.kind == PlatformKind.WINDOWS

    @property
    def is_posix(self) -> bool:
        return not self.is_windows

    @property
    def supports_unix_supervision(self) -> bool:
        return self.kind in {
            PlatformKind.MACOS, PlatformKind.LINUX,
            PlatformKind.TERMUX, PlatformKind.PROOT,
        }

    @property
    def is_android(self) -> bool:
        return self.kind in {PlatformKind.TERMUX, PlatformKind.PROOT}

    def candidates(self, name: str) -> list[str]:
        home = self.home
        values: Dict[str, list[str]] = {
            "harness": [
                str(home / ".local" / "bin" / executable_name("harness", self)),
            ],
            "opencode": [
                str(home / ".opencode" / "bin" / executable_name("opencode", self)),
                str(home / ".local" / "bin" / executable_name("opencode", self)),
            ],
            "prime": [
                str(home / "prime-agent" / ("prime-agent.cmd" if self.is_windows else "prime-agent.sh")),
                str(home / ".local" / "bin" / executable_name("prime-agent", self)),
            ],
            "hermes": [
                str(home / ".local" / "bin" / executable_name("hermes", self)),
                str(home / ".hermes" / "bin" / executable_name("hermes", self)),
            ],
            "node": [
                str(Path(self.env.get("ProgramFiles", "C:/Program Files")) / "nodejs" / "node.exe")
                if self.is_windows else "/usr/bin/node",
            ],
            "python": [sys.executable],
            "openssl": [
                str(Path(self.env.get("ProgramFiles", "C:/Program Files")) / "Git" / "usr" / "bin" / "openssl.exe")
                if self.is_windows else "/usr/bin/openssl",
                "/opt/homebrew/bin/openssl",
                "/usr/local/bin/openssl",
            ],
        }
        return values.get(name, [])

    def discover(self, name: str, required: bool = False) -> Optional[str]:
        override = self.env.get(EXECUTABLE_ENV.get(name, ""), "")
        if override:
            candidate = os.path.expandvars(os.path.expanduser(override))
            if _usable_executable(candidate, self):
                return os.path.abspath(candidate)
            if required:
                raise FileNotFoundError(f"configured {name} executable is unavailable: {candidate}")
            return None
        search_names = {
            "harness": ("harness",),
            "opencode": ("opencode",),
            "prime": ("prime-agent", "prime"),
            "hermes": ("hermes",),
            "node": ("node", "nodejs"),
            "python": ("python3", "python"),
            "openssl": ("openssl",),
        }.get(name, (name,))
        path = self.env.get("PATH") or os.environ.get("PATH", "")
        for command in search_names:
            found = shutil.which(command, path=path)
            if found:
                return os.path.abspath(found)
        for candidate in self.candidates(name):
            if _usable_executable(candidate, self):
                return os.path.abspath(candidate)
        if required:
            raise FileNotFoundError(f"could not discover executable: {name}")
        return None

    def command_prefix(self, executable: str) -> list[str]:
        suffix = Path(executable).suffix.lower()
        if self.is_windows and suffix in {".cmd", ".bat"}:
            comspec = self.env.get("COMSPEC", os.environ.get("COMSPEC", "cmd.exe"))
            return [comspec, "/d", "/s", "/c", executable]
        return [executable]

    def background_kwargs(self, hide_window: bool = True) -> Dict[str, object]:
        if self.is_windows:
            flags = 0x00000200 | 0x00000008  # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
            if hide_window:
                flags |= 0x08000000  # CREATE_NO_WINDOW
            return {"creationflags": flags}
        return {"start_new_session": True}


def executable_name(name: str, info: PlatformInfo) -> str:
    return name + (".exe" if info.is_windows else "")


def _usable_executable(path: str, info: PlatformInfo) -> bool:
    if not os.path.isfile(path):
        return False
    if info.is_windows:
        return Path(path).suffix.lower() in {".exe", ".cmd", ".bat", ".com", ".ps1", ""}
    return os.access(path, os.X_OK)


def detect_platform(
    *,
    sys_platform: Optional[str] = None,
    os_name: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[str] = None,
    release: Optional[str] = None,
) -> PlatformInfo:
    raw_values = dict(os.environ if env is None else env)
    sensitive_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    values = {
        key: value for key, value in raw_values.items()
        if not any(marker in key.upper() for marker in sensitive_markers)
    }
    system = sys.platform if sys_platform is None else sys_platform
    family = os.name if os_name is None else os_name
    kernel = _platform.release() if release is None else release
    home_path = Path(home or raw_values.get("HOME") or raw_values.get("USERPROFILE") or str(Path.home())).expanduser()

    prefix = raw_values.get("PREFIX", "").lower()
    proot_hint = any(key in raw_values for key in ("PROOT_L2S_DIR", "PROOT_TMP_DIR", "PROOT_DISTRO")) or "proot" in kernel.lower()
    termux_hint = "com.termux" in prefix or "TERMUX_VERSION" in raw_values

    if family == "nt" or system.startswith("win"):
        kind = PlatformKind.WINDOWS
    elif system == "darwin":
        kind = PlatformKind.MACOS
    elif proot_hint:
        kind = PlatformKind.PROOT
    elif termux_hint or system.startswith("android"):
        kind = PlatformKind.TERMUX
    else:
        kind = PlatformKind.LINUX

    legacy = home_path / ".harness2"
    state_override = raw_values.get("HARNESS2_HOME")
    if state_override:
        state = Path(state_override).expanduser()
    elif legacy.exists():
        state = legacy
    elif kind == PlatformKind.WINDOWS:
        state = Path(raw_values.get("LOCALAPPDATA", str(home_path / "AppData" / "Local"))) / "Harness2"
    elif kind == PlatformKind.MACOS:
        state = home_path / "Library" / "Application Support" / "Harness2"
    elif kind in {PlatformKind.TERMUX, PlatformKind.PROOT}:
        state = legacy
    else:
        state = Path(raw_values.get("XDG_STATE_HOME", str(home_path / ".local" / "state"))) / "harness2"

    if kind == PlatformKind.WINDOWS:
        config = Path(raw_values.get("APPDATA", str(home_path / "AppData" / "Roaming"))) / "Harness2"
        cache = Path(raw_values.get("LOCALAPPDATA", str(home_path / "AppData" / "Local"))) / "Harness2" / "Cache"
    elif kind == PlatformKind.MACOS:
        config = home_path / "Library" / "Application Support" / "Harness2"
        cache = home_path / "Library" / "Caches" / "Harness2"
    else:
        config = Path(raw_values.get("XDG_CONFIG_HOME", str(home_path / ".config"))) / "harness2"
        cache = Path(raw_values.get("XDG_CACHE_HOME", str(home_path / ".cache"))) / "harness2"
    runtime = state / "run"
    return PlatformInfo(kind, home_path, state, config, cache, runtime, values)


def is_loopback_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname
        if host is None:
            return False
        if host.lower() == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False
