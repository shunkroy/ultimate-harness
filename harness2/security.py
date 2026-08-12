"""Private state, secret handling and redaction primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import subprocess
import getpass
from pathlib import Path
from typing import Any, Dict, Iterable


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
_TOKEN_PATTERNS = (
    re.compile(r"\b(?:sk|pk|api|key)-[A-Za-z0-9_.-]{12,}\b", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9_.-]{12,}\b", re.I),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*[^\s,;]+"),
)


class SecurityViolation(RuntimeError):
    pass


def _harden_windows_acl(path: str) -> None:
    if os.name != "nt":
        return
    user = getpass.getuser()
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:(F)"],
            capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def ensure_private_dir(path: str) -> str:
    p = os.path.abspath(os.path.expanduser(path))
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        os.makedirs(p, mode=PRIVATE_DIR_MODE, exist_ok=False)
        os.chmod(p, PRIVATE_DIR_MODE)
        _harden_windows_acl(p)
        return p
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise SecurityViolation(f"private path is not a real directory: {p}")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise SecurityViolation(f"private directory has wrong owner: {p}")
    os.chmod(p, PRIVATE_DIR_MODE)
    _harden_windows_acl(p)
    return p


def _reject_symlink(path: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise SecurityViolation(f"refusing symlink: {path}")


def atomic_write_json(path: str, value: Dict[str, Any]) -> None:
    path = os.path.abspath(os.path.expanduser(path))
    parent = ensure_private_dir(os.path.dirname(path))
    _reject_symlink(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=parent)
    try:
        try:
            os.fchmod(fd, PRIVATE_FILE_MODE)
        except (AttributeError, OSError):
            pass
        payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
        try:
            os.chmod(path, PRIVATE_FILE_MODE)
        except OSError:
            pass
        _harden_windows_acl(path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def atomic_write_bytes(path: str, value: bytes) -> None:
    path = os.path.abspath(os.path.expanduser(path))
    parent = ensure_private_dir(os.path.dirname(path))
    _reject_symlink(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=parent)
    try:
        try:
            os.fchmod(fd, PRIVATE_FILE_MODE)
        except (AttributeError, OSError):
            pass
        os.write(fd, value)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
        try:
            os.chmod(path, PRIVATE_FILE_MODE)
        except OSError:
            pass
        _harden_windows_acl(path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def read_private_json(path: str) -> Dict[str, Any]:
    path = os.path.abspath(os.path.expanduser(path))
    _reject_symlink(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {}
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def task_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8", "surrogatepass")).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact(value: Any, limit: int = 600) -> str:
    text = str(value or "")
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    home = str(Path.home())
    text = text.replace(home, "~")
    return text[:limit]


def private_mode(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    if os.name == "nt":
        # Windows confidentiality comes from DPAPI for secrets and a hardened
        # user ACL for state. POSIX mode bits are not authoritative there.
        return True
    expected = PRIVATE_DIR_MODE if stat.S_ISDIR(st.st_mode) else PRIVATE_FILE_MODE
    return stat.S_IMODE(st.st_mode) == expected


def harden_paths(paths: Iterable[str]) -> list[str]:
    changed: list[str] = []
    for raw in paths:
        path = os.path.expanduser(raw)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        mode = PRIVATE_DIR_MODE if stat.S_ISDIR(st.st_mode) else PRIVATE_FILE_MODE
        if os.name == "nt":
            _harden_windows_acl(path)
            changed.append(path)
        elif stat.S_IMODE(st.st_mode) != mode:
            os.chmod(path, mode)
            changed.append(path)
    return changed


class PrivateTempFile:
    """Private temporary file removed on context exit."""

    def __init__(self, directory: str, data: bytes, suffix: str = ".txt"):
        self.directory = ensure_private_dir(directory)
        self.data = data
        self.suffix = suffix
        self.path: str | None = None

    def __enter__(self) -> str:
        fd, path = tempfile.mkstemp(prefix=".input-", suffix=self.suffix, dir=self.directory)
        self.path = path
        try:
            try:
                os.fchmod(fd, PRIVATE_FILE_MODE)
            except (AttributeError, OSError):
                pass
            os.write(fd, self.data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(path, PRIVATE_FILE_MODE)
        except OSError:
            pass
        _harden_windows_acl(path)
        return path

    def __exit__(self, *_):
        if self.path:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
