"""Small, provider-independent subprocess execution boundary.

The boundary deliberately accepts only explicit process configuration, captures a
bounded amount of output, and owns the lifetime of the process group it creates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from types import MappingProxyType
from typing import BinaryIO, Mapping


MAX_TIMEOUT_SECONDS = 3_600.0
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_HTTP_BODY_BYTES = 64 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_OUTPUT_BYTES = 1024 * 1024

_READ_CHUNK_BYTES = 64 * 1024
_POLL_SECONDS = 0.01
_PIPE_EOF_GRACE_SECONDS = 1.0
_TERMINATE_GRACE_SECONDS = 0.20
_PRIVATE = "<private>"
_SECRET_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


class ProcessConfigurationError(ValueError):
    """A process request is malformed or exceeds a boundary limit."""


class ProcessSpawnError(RuntimeError):
    """The operating system could not create the requested process."""


class BodyLimitExceeded(ValueError):
    """A streamed response body exceeded its configured byte limit."""

    def __init__(self, byte_limit: int) -> None:
        self.byte_limit = byte_limit
        super().__init__(f"body exceeds {byte_limit} bytes")


class BodyDeadlineExceeded(TimeoutError):
    """A streamed response exceeded its monotonic total deadline."""


WorkingDirectoryIdentity = tuple[int, int]


def _directory_identity(path: str) -> WorkingDirectoryIdentity:
    try:
        metadata = os.stat(path)
    except (OSError, TypeError, ValueError) as exc:
        raise ProcessConfigurationError("cwd must be an existing directory") from exc
    if not os.path.isdir(path):
        raise ProcessConfigurationError("cwd must be an existing directory")
    return int(metadata.st_dev), int(metadata.st_ino)


def prepare_working_directory(
    path: str | os.PathLike[str] | None = None,
    expected_identity: WorkingDirectoryIdentity | None = None,
) -> tuple[str, WorkingDirectoryIdentity]:
    """Resolve a directory once, or verify an already prepared path by identity."""

    candidate = os.getcwd() if path is None else os.fspath(path)
    if expected_identity is not None:
        if (
            not isinstance(candidate, str) or not os.path.isabs(candidate)
            or not isinstance(expected_identity, tuple) or len(expected_identity) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in expected_identity)
        ):
            raise ProcessConfigurationError("prepared cwd authority is invalid")
        actual = _directory_identity(candidate)
        if actual != expected_identity:
            raise ProcessConfigurationError("cwd identity changed after authorization")
        return candidate, actual
    try:
        resolved = Path(candidate).expanduser().resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise ProcessConfigurationError("cwd must be an existing directory") from exc
    if not resolved.is_dir():
        raise ProcessConfigurationError("cwd must be an existing directory")
    result = str(resolved)
    return result, _directory_identity(result)


def canonical_working_directory(path: str | os.PathLike[str] | None = None) -> str:
    """Compatibility helper returning a newly prepared canonical directory."""
    return prepare_working_directory(path)[0]


def secret_environment_keys(env: Mapping[str, str]) -> tuple[str, ...]:
    """Return names whose values must never enter fingerprints or repr output."""
    return tuple(sorted(
        key for key in env
        if any(marker in key.upper() for marker in _SECRET_MARKERS)
    ))


def _bounded_number(name: str, value: object, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProcessConfigurationError(f"{name} must be a number")
    result = float(value)
    if not 0 < result <= maximum:
        raise ProcessConfigurationError(f"{name} must be in (0, {maximum:g}]")
    return result


def _bounded_bytes(name: str, value: object, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProcessConfigurationError(f"{name} must be an integer")
    if not 0 < value <= maximum:
        raise ProcessConfigurationError(f"{name} must be in [1, {maximum}]")
    return value


@dataclass(frozen=True, repr=False)
class ProcessRequest:
    """Validated, immutable configuration for :func:`run_process`.

    ``private_argv_indices`` and ``secret_env_keys`` replace the corresponding
    values with a fixed marker for fingerprinting and representation. This makes
    fingerprints stable across secret rotation and ephemeral private paths.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | os.PathLike[str] = field(default_factory=os.getcwd)
    cwd_identity: WorkingDirectoryIdentity | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    stdout_limit: int = DEFAULT_OUTPUT_BYTES
    stderr_limit: int = DEFAULT_OUTPUT_BYTES
    private_argv_indices: tuple[int, ...] = ()
    secret_env_keys: tuple[str, ...] = ()
    additional_cwd_authorities: tuple[tuple[str, WorkingDirectoryIdentity], ...] = ()
    config_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ProcessConfigurationError("argv must be a non-empty tuple")
        if any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in self.argv):
            raise ProcessConfigurationError("argv entries must be non-empty strings without NUL")

        if not isinstance(self.env, Mapping):
            raise ProcessConfigurationError("env must be a string mapping")
        copied_env: dict[str, str] = {}
        for key, value in self.env.items():
            if (
                not isinstance(key, str)
                or not key
                or "=" in key
                or "\x00" in key
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise ProcessConfigurationError("env must contain valid string keys and values")

            copied_env[key] = value

        if not isinstance(self.private_argv_indices, tuple) or any(
            isinstance(index, bool) or not isinstance(index, int)
            for index in self.private_argv_indices
        ):
            raise ProcessConfigurationError("private_argv_indices must be a tuple of integers")
        private_indices = tuple(sorted(set(self.private_argv_indices)))
        if any(index < 0 or index >= len(self.argv) for index in private_indices):
            raise ProcessConfigurationError("private argv index is out of range")

        if not isinstance(self.secret_env_keys, tuple) or any(
            not isinstance(key, str) or not key for key in self.secret_env_keys
        ):
            raise ProcessConfigurationError("secret_env_keys must be a tuple of non-empty strings")
        secret_keys = tuple(sorted(set(self.secret_env_keys)))
        if any(key not in copied_env for key in secret_keys):
            raise ProcessConfigurationError("secret env key is not present in env")

        if (
            not isinstance(self.additional_cwd_authorities, tuple)
            or len(self.additional_cwd_authorities) > 8
        ):
            raise ProcessConfigurationError("additional cwd authorities must be a bounded tuple")
        additional: list[tuple[str, WorkingDirectoryIdentity]] = []
        for authority in self.additional_cwd_authorities:
            if not isinstance(authority, tuple) or len(authority) != 2:
                raise ProcessConfigurationError("additional cwd authority is invalid")
            path, identity = authority
            verified_path, verified_identity = prepare_working_directory(path, identity)
            additional.append((verified_path, verified_identity))

        cwd, cwd_identity = prepare_working_directory(self.cwd, self.cwd_identity)

        timeout = _bounded_number("timeout", self.timeout, MAX_TIMEOUT_SECONDS)
        stdout_limit = _bounded_bytes("stdout_limit", self.stdout_limit, MAX_OUTPUT_BYTES)
        stderr_limit = _bounded_bytes("stderr_limit", self.stderr_limit, MAX_OUTPUT_BYTES)

        object.__setattr__(self, "env", MappingProxyType(copied_env))
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "cwd_identity", cwd_identity)
        object.__setattr__(self, "timeout", timeout)
        object.__setattr__(self, "stdout_limit", stdout_limit)
        object.__setattr__(self, "stderr_limit", stderr_limit)
        object.__setattr__(self, "private_argv_indices", private_indices)
        object.__setattr__(self, "secret_env_keys", secret_keys)
        object.__setattr__(self, "additional_cwd_authorities", tuple(additional))
        object.__setattr__(self, "config_fingerprint", self._fingerprint())

    def _redacted_argv(self) -> tuple[str, ...]:
        private = frozenset(self.private_argv_indices)
        return tuple(_PRIVATE if index in private else value for index, value in enumerate(self.argv))

    def _redacted_env(self) -> tuple[tuple[str, str], ...]:
        secret = frozenset(self.secret_env_keys)
        return tuple((key, _PRIVATE if key in secret else self.env[key]) for key in sorted(self.env))

    def _fingerprint(self) -> str:
        payload = {
            "schema": 1,
            "argv": self._redacted_argv(),
            "env": self._redacted_env(),
            "cwd": self.cwd,
            "cwd_identity": self.cwd_identity,
            "timeout": self.timeout,
            "stdout_limit": self.stdout_limit,
            "stderr_limit": self.stderr_limit,
            "additional_cwd_authorities": self.additional_cwd_authorities,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def __repr__(self) -> str:
        return (
            "ProcessRequest("
            f"argv={self._redacted_argv()!r}, env={dict(self._redacted_env())!r}, "
            f"cwd={self.cwd!r}, cwd_identity={self.cwd_identity!r}, timeout={self.timeout!r}, "
            f"stdout_limit={self.stdout_limit!r}, stderr_limit={self.stderr_limit!r}, "
            f"private_argv_indices={self.private_argv_indices!r}, "
            f"secret_env_keys={self.secret_env_keys!r}, "
            f"additional_cwd_authorities={self.additional_cwd_authorities!r}, "
            f"config_fingerprint={self.config_fingerprint!r})"
        )


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool
    output_limited: bool
    config_fingerprint: str


class _BoundedPipeReader(threading.Thread):
    def __init__(self, pipe: BinaryIO, limit: int, limited: threading.Event) -> None:
        super().__init__(daemon=True)
        self.pipe = pipe
        self.limit = limit
        self.limited = limited
        self.data = bytearray()

    def run(self) -> None:
        total = 0
        try:
            while True:
                chunk = self.pipe.read(_READ_CHUNK_BYTES)
                if not chunk:
                    return
                total += len(chunk)
                room = self.limit - len(self.data)
                if room > 0:
                    self.data.extend(chunk[:room])
                if total > self.limit:
                    self.limited.set()
        except (OSError, ValueError):
            # The controller may close a pipe after group cleanup to unblock us.
            return


def _terminate_process_tree(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass
        time.sleep(_TERMINATE_GRACE_SECONDS)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
    else:  # Best effort: Windows process groups do not provide POSIX tree semantics.
        break_signal = getattr(signal, "CTRL_BREAK_EVENT", None)
        if break_signal is not None:
            try:
                proc.send_signal(break_signal)
            except OSError:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass


def run_process(request: ProcessRequest) -> ProcessResult:
    """Execute a validated request and return bounded, decoded output."""

    if not isinstance(request, ProcessRequest):
        raise ProcessConfigurationError("request must be a ProcessRequest")

    # Recheck the exact authorized directory identity immediately before spawn.
    # Popen is still pathname-based, but no later path resolution may silently
    # retarget execution to a different directory.
    prepare_working_directory(request.cwd, request.cwd_identity)
    for path, identity in request.additional_cwd_authorities:
        prepare_working_directory(path, identity)
    popen_kwargs: dict[str, object] = {
        "cwd": request.cwd,
        "env": dict(request.env),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "bufsize": 0,
        "close_fds": True,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    started = time.monotonic()
    try:
        proc = subprocess.Popen(request.argv, **popen_kwargs)  # type: ignore[arg-type]
    except (OSError, ValueError) as exc:
        error_code = getattr(exc, "errno", None)
        suffix = f" (errno {error_code})" if error_code is not None else ""
        raise ProcessSpawnError(f"failed to spawn process{suffix}") from exc

    assert proc.stdout is not None and proc.stderr is not None
    limited = threading.Event()
    stdout_reader = _BoundedPipeReader(proc.stdout, request.stdout_limit, limited)
    stderr_reader = _BoundedPipeReader(proc.stderr, request.stderr_limit, limited)
    timed_out = False
    try:
        stdout_reader.start()
        stderr_reader.start()

        cleaned_up = False
        deadline = started + request.timeout
        while proc.poll() is None:
            if limited.is_set():
                _terminate_process_tree(proc)
                cleaned_up = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_tree(proc)
                cleaned_up = True
                break
            limited.wait(_POLL_SECONDS)

        # A short wait allows ordinary buffered output to reach EOF. If it does
        # not, a descendant inherited a pipe; clean the group before returning.
        stdout_reader.join(_PIPE_EOF_GRACE_SECONDS)
        stderr_reader.join(_PIPE_EOF_GRACE_SECONDS)
        if (stdout_reader.is_alive() or stderr_reader.is_alive()) and not cleaned_up:
            _terminate_process_tree(proc)
            cleaned_up = True

        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(proc)
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass

        duration = time.monotonic() - started
        return ProcessResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=bytes(stdout_reader.data).decode("utf-8", errors="replace"),
            stderr=bytes(stderr_reader.data).decode("utf-8", errors="replace"),
            duration=duration,
            timed_out=timed_out,
            output_limited=limited.is_set(),
            config_fingerprint=request.config_fingerprint,
        )
    finally:
        # Cancellation and unexpected controller exceptions must not orphan the
        # isolated process group or leave prompt-bearing pipes open.
        if proc.poll() is None or stdout_reader.is_alive() or stderr_reader.is_alive():
            _terminate_process_tree(proc)
        try:
            proc.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        if stdout_reader.ident is not None:
            stdout_reader.join(1.0)
        if stderr_reader.ident is not None:
            stderr_reader.join(1.0)


def read_bounded_body(
    stream: BinaryIO,
    *,
    byte_limit: int,
    chunk_size: int = _READ_CHUNK_BYTES,
    deadline: float | None = None,
) -> bytes:
    """Read a binary HTTP/body stream, raising before retaining over the limit."""

    limit = _bounded_bytes("byte_limit", byte_limit, MAX_HTTP_BODY_BYTES)
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ProcessConfigurationError("chunk_size must be a positive integer")
    body = bytearray()
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise BodyDeadlineExceeded("body read exceeded its total deadline")
        chunk = stream.read(min(chunk_size, limit + 1 - len(body)))
        if deadline is not None and time.monotonic() >= deadline:
            raise BodyDeadlineExceeded("body read exceeded its total deadline")
        if not chunk:
            return bytes(body)
        if not isinstance(chunk, bytes):
            raise TypeError("body stream must return bytes")
        body.extend(chunk)
        if len(body) > limit:
            raise BodyLimitExceeded(limit)


__all__ = [
    "BodyLimitExceeded",
    "BodyDeadlineExceeded",
    "canonical_working_directory",
    "prepare_working_directory",
    "DEFAULT_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_HTTP_BODY_BYTES",
    "MAX_OUTPUT_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "ProcessConfigurationError",
    "ProcessRequest",
    "ProcessResult",
    "ProcessSpawnError",
    "read_bounded_body",
    "run_process",
    "secret_environment_keys",
]
