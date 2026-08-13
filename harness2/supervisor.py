"""Stdlib-only, PRoot-safe supervision helpers for the Prime daemon.

Responsibilities (each enforced by a dedicated helper, see docstrings):

* Private run-dir validation -- ``validate_run_dir`` / ``ensure_run_dir``:
  ``lstat``-based check that the directory is not a symlink, is owned by the
  current uid and has mode exactly ``0700``.
* Exact process matching -- ``read_cmdline`` / ``argv_matches`` /
  ``cmdline_matches`` / ``find_matching_pids``: verifies a pid against the
  literal ``argv`` recorded in ``/proc/<pid>/cmdline`` for the bundle path and
  the daemon socket path. No pattern kills; a pid is only ever signalled after
  this exact verification.
* Atomic pidfile I/O -- ``write_pidfile`` / ``read_pidfile``: write through a
  unique temp file + ``os.replace`` (atomic on POSIX), mode ``0600``; reads
  refuse symlinks (``O_NOFOLLOW`` / ``lstat`` pre-check).
* Start mutex -- ``FlockLock`` / ``MkdirLock`` / ``acquire_start_lock``: two
  interoperable acquisition strategies with one interface. ``flock`` is used
  when the filesystem supports it; ``mkdir``-based locking is the compatible
  fallback for filesystems where ``flock`` is unavailable (common under PRoot /
  Android FUSE), and can be forced via ``method``.
* Truthful socket probe -- ``socket_reachable``: a real ``connect(2)`` over
  ``AF_UNIX``, *not* a path-existence check. A bound-but-dead socket, a plain
  file, or a missing path all yield ``False``.
* Safe stale cleanup -- ``cleanup_stale`` / ``safe_unlink``: removes stale
  pidfile/socket entries with ``os.*`` calls only (never shell/``rm``), after
  ``lstat`` verification that the target is not a symlink; heals a stale
  pidfile when a live, exactly-matching daemon is found via ``/proc``.
* Start/stop -- ``start_daemon`` / ``stop_daemon`` / ``stop_verified_pid``:
  spawns the Node dist bundle with ``start_new_session=True`` and a private
  ``0600`` log; stop is ``SIGTERM`` -> bounded wait -> ``SIGKILL`` applied only
  to the exactly-verified pid.
* Status -- :class:`DaemonStatus` dataclass snapshot of everything above.

Constraints honoured: no systemd, no ``pkill``/pattern kills, no ``shell=True``,
no filesystem mutations outside the caller-supplied run dir.
"""

from __future__ import annotations

import errno
try:  # Native Windows has no fcntl; mkdir locks remain available.
    import fcntl
except ImportError:  # pragma: no cover - exercised by Windows CI
    fcntl = None  # type: ignore[assignment]
import os
import shutil
import signal
import socket
import stat
import subprocess
import time
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional

__all__ = [
    # constants
    "RUN_DIR_MODE",
    "PIDFILE_MODE",
    "DEFAULT_PIDFILE",
    "DEFAULT_SOCKET",
    "DEFAULT_LOG",
    # exceptions
    "SupervisorError",
    "RunDirError",
    "SecurityError",
    "ProcessVerifyError",
    "StartError",
    "LockTimeoutError",
    # run dir
    "validate_run_dir",
    "ensure_run_dir",
    # pidfile
    "write_pidfile",
    "read_pidfile",
    "safe_unlink",
    # process matching
    "read_cmdline",
    "argv_matches",
    "cmdline_matches",
    "find_matching_pids",
    "pid_alive",
    # socket probe
    "socket_reachable",
    # locks
    "FlockLock",
    "MkdirLock",
    "acquire_start_lock",
    # lifecycle
    "start_daemon",
    "stop_daemon",
    "stop_verified_pid",
    "cleanup_stale",
    "status",
    # status
    "DaemonStatus",
]

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

RUN_DIR_MODE = 0o700
PIDFILE_MODE = 0o600
LOG_MODE = 0o600

DEFAULT_PIDFILE = "daemon.pid"
DEFAULT_SOCKET = "daemon.sock"
DEFAULT_LOG = "daemon.log"
DEFAULT_LOCK_NAME = ".start.lock"

_PROBE_STEP = 0.1
_LOCK_STEP = 0.05

# Popen handles for daemons spawned by start_daemon. Kept so we can reap
# (waitpid) our own children once they die -- a stopped daemon would otherwise
# linger as a zombie (reparented to init only when we exit) and Python would
# raise ResourceWarning when the un-polled Popen is garbage-collected.
_spawned: "dict[int, subprocess.Popen]" = {}


def _register(proc: subprocess.Popen) -> None:
    _spawned[proc.pid] = proc


def _reap(pid: int) -> None:
    """Poll (and thereby reap) a daemon we spawned, if any."""
    proc = _spawned.pop(pid, None)
    if proc is not None:
        try:
            proc.poll()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class SupervisorError(Exception):
    """Base class for all supervisor failures."""


class RunDirError(SupervisorError, ValueError):
    """Run directory failed the private-directory validation."""


class SecurityError(SupervisorError):
    """Refusing an operation on a path that failed lstat safety checks."""


class ProcessVerifyError(SupervisorError):
    """A pid failed exact cmdline verification; it will not be signalled."""


class StartError(SupervisorError):
    """Daemon failed to start (spawn error, early exit, or socket timeout)."""


class LockTimeoutError(SupervisorError):
    """Could not acquire the start mutex within the deadline."""


# --------------------------------------------------------------------------
# Run directory validation
# --------------------------------------------------------------------------

def validate_run_dir(path: str) -> str:
    """Validate ``path`` as a private run dir.

    Uses ``lstat`` (never follows symlinks) and requires, on the directory
    itself: not a symlink, a directory, owned by the current uid, mode exactly
    ``0700``. Returns the abspath on success; raises :class:`RunDirError`.
    """
    path = os.path.abspath(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise RunDirError(f"run dir does not exist: {path}") from None
    except OSError as exc:
        raise RunDirError(f"cannot stat run dir {path}: {exc}") from exc

    if stat.S_ISLNK(st.st_mode):
        raise RunDirError(f"run dir must not be a symlink: {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise RunDirError(f"run dir is not a directory: {path}")
    if st.st_uid != os.getuid():
        raise RunDirError(
            f"run dir owner uid {st.st_uid} != current uid {os.getuid()}: {path}"
        )
    if stat.S_IMODE(st.st_mode) != RUN_DIR_MODE:
        raise RunDirError(
            f"run dir mode {oct(stat.S_IMODE(st.st_mode))} != 0700: {path}"
        )
    return path


def ensure_run_dir(path: str) -> str:
    """Create ``path`` if missing (mode 0700) and validate it."""
    path = os.path.abspath(path)
    try:
        os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path)
        os.chmod(path, RUN_DIR_MODE)  # pin exact 0700 regardless of umask
    return validate_run_dir(path)


# --------------------------------------------------------------------------
# Safe unlink / pidfile I/O
# --------------------------------------------------------------------------

def safe_unlink(path: str) -> None:
    """Unlink ``path`` only after an lstat safety check.

    Refuses symlinks and directories (raises :class:`SecurityError`); regular
    files, sockets and fifos are unlinked with plain ``os.unlink``. Never uses
    the shell or ``rm``. Missing paths are a no-op.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise SecurityError(f"refusing to unlink symlink: {path}")
    if stat.S_ISDIR(st.st_mode):
        raise SecurityError(f"refusing to unlink directory: {path}")
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISSOCK(st.st_mode)
            or stat.S_ISFIFO(st.st_mode)):
        raise SecurityError(f"refusing to unlink special file: {path}")
    os.unlink(path)


def write_pidfile(path: str, pid: int) -> None:
    """Atomically write ``pid`` to ``path`` with mode 0600.

    Writes a unique temp file in the same directory (``O_EXCL``, mode 0600),
    ``fsync``s it, then ``os.replace``s it into place -- atomic on POSIX.
    """
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(
        directory,
        f".{os.path.basename(path)}.{os.getpid()}.{os.urandom(4).hex()}.tmp",
    )
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PIDFILE_MODE)
    try:
        os.write(fd, f"{int(pid)}\n".encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, PIDFILE_MODE)  # pin exact 0600 regardless of umask
    os.replace(tmp, path)


def read_pidfile(path: str) -> Optional[int]:
    """Read a pidfile, returning the pid or ``None``.

    * Missing file -> ``None``.
    * Symlink anywhere in the check -> :class:`SecurityError` (``lstat``
      pre-check plus ``O_NOFOLLOW`` for the open).
    * Unparseable content -> ``None`` (treated as stale by cleanup).
    """
    path = os.path.abspath(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise SecurityError(f"refusing to read symlink pidfile: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SecurityError(f"refusing to read symlink pidfile: {path}") from exc
        raise
    try:
        with os.fdopen(fd, "r", encoding="ascii") as fh:
            data = fh.read().strip()
    except (OSError, ValueError):
        return None
    if not data:
        return None
    try:
        return int(data)
    except ValueError:
        return None  # corrupt pidfile -> stale


# --------------------------------------------------------------------------
# Exact process matching via /proc/<pid>/cmdline
# --------------------------------------------------------------------------

def _state_char(pid: int) -> Optional[bytes]:
    """Return the process state char from /proc/<pid>/stat, or None."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            data = fh.read(4096)
    except OSError:
        return None
    idx = data.rfind(b")")  # comm may contain spaces/parens; state follows ")"
    if idx == -1 or idx + 2 >= len(data):
        return None
    return data[idx + 2:idx + 3]


def pid_alive(pid: int) -> bool:
    """True iff ``pid`` exists and is not a zombie.

    Zombies (state ``Z``) count as dead: they cannot run, cannot be signalled
    meaningfully, and would otherwise hang the bounded stop waits. Detection:
    signal-0 probe (existence) plus a /proc state check.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return _state_char(pid) != b"Z"


def read_cmdline(pid: int) -> Optional[List[str]]:
    """Return the ``argv`` of ``pid`` as a list of strings.

    ``None`` when ``/proc/<pid>/cmdline`` cannot be read (pid gone, or /proc
    unavailable under PRoot); ``[]`` for a zombie (empty cmdline).
    """
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if not raw:
        return []
    parts = raw.split(b"\x00")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    return [p.decode("utf-8", "replace") for p in parts]


def _norm(p: str) -> str:
    """Normalize a path for exact comparison (realpath + normpath)."""
    if not p:
        return p
    try:
        return os.path.normpath(os.path.realpath(p))
    except (OSError, ValueError):
        return os.path.normpath(p)


def argv_matches(argv: Iterable[str], bundle_path: str, socket_path: str) -> bool:
    """Exact argv matching: bundle path AND socket path as literal entries.

    Each expected path is normalized with ``realpath``/``normpath``; an argv
    entry matches only when it normalizes to exactly the same string. A bare
    basename, a different path, or a path missing either entry does not match.
    """
    args = list(argv)
    target_bundle = _norm(bundle_path)
    target_socket = _norm(socket_path)
    has_bundle = any(_norm(a) == target_bundle for a in args)
    has_socket = any(_norm(a) == target_socket for a in args)
    return has_bundle and has_socket


def cmdline_matches(pid: int, bundle_path: str, socket_path: str) -> bool:
    """True iff pid is alive and its exact ``argv`` contains bundle + socket."""
    argv = read_cmdline(pid)
    if argv is None:
        return False
    return argv_matches(argv, bundle_path, socket_path)


def find_matching_pids(bundle_path: str, socket_path: str) -> List[int]:
    """Scan ``/proc`` for live pids whose exact argv matches bundle + socket.

    Returns a sorted list. Pids that vanish mid-scan or whose cmdline cannot be
    read are skipped; only exact matches are ever returned.
    """
    matches: List[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return matches
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if not pid_alive(pid):
            continue
        argv = read_cmdline(pid)
        if argv and argv_matches(argv, bundle_path, socket_path):
            matches.append(pid)
    return sorted(matches)


# --------------------------------------------------------------------------
# Truthful AF_UNIX socket reachability probe
# --------------------------------------------------------------------------

def socket_reachable(path: str, timeout: float = 1.0) -> bool:
    """True iff an ``AF_UNIX`` stream connect to ``path`` actually succeeds.

    This is a real ``connect(2)`` probe -- a path that merely exists is NOT
    reachable. Missing path (``ENOENT``), stale/dead socket inode
    (``ECONNREFUSED``), plain file, permission denied and timeouts all yield
    ``False``. The socket is always closed on exit.
    """
    if not path:
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(max(0.05, float(timeout)))
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Start mutex: flock-based and mkdir-based (compatible interface)
# --------------------------------------------------------------------------

class FlockLock:
    """Exclusive start mutex via ``flock(LOCK_EX)`` on a lock file (mode 0600).

    ``acquire()`` polls with ``LOCK_EX|LOCK_NB`` until ``timeout`` seconds,
    then raises :class:`LockTimeoutError`. Also usable as a context manager.
    """

    def __init__(self, path: str, timeout: float = 10.0) -> None:
        self.path = os.path.abspath(path)
        self.timeout = float(timeout)
        self._fd: Optional[int] = None

    def acquire(self) -> "FlockLock":
        if fcntl is None:
            raise OSError(errno.ENOSYS, "flock is unavailable on this platform")
        if self._fd is not None:
            return self  # re-entrant: already held
        directory = os.path.dirname(self.path) or "."
        if directory and not os.path.isdir(directory):
            raise SupervisorError(f"lock directory missing: {directory}")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError) as exc:
                    if not isinstance(exc, BlockingIOError) and exc.errno not in (
                        errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK,
                    ):
                        raise
                    if time.monotonic() >= deadline:
                        raise LockTimeoutError(
                            f"could not acquire flock lock {self.path} "
                            f"within {self.timeout:.1f}s"
                        ) from None
                    time.sleep(_LOCK_STEP)
            os.chmod(self.path, 0o600)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def is_held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "FlockLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover - defensive
        try:
            self.release()
        except Exception:
            pass


class MkdirLock:
    """Exclusive start mutex via atomic ``os.mkdir`` of a lock directory.

    Compatible alternative to :class:`FlockLock` for filesystems where
    ``flock`` is unavailable (PRoot / Android FUSE). Dead-holder recovery: a
    lock directory whose mtime is older than ``stale_after`` seconds is
    considered abandoned and removed (``os.rmdir``, only if empty).
    """

    def __init__(self, path: str, timeout: float = 10.0,
                 stale_after: float = 600.0) -> None:
        self.path = os.path.abspath(path)
        self.timeout = float(timeout)
        self.stale_after = float(stale_after)
        self._held = False

    @staticmethod
    def _steal_stale(path: str, stale_after: float) -> None:
        try:
            st = os.lstat(path)
        except OSError:
            return
        if stat.S_ISLNK(st.st_mode):
            raise SecurityError(f"refusing to remove symlink lock dir: {path}")
        if not stat.S_ISDIR(st.st_mode):
            return
        if st.st_mtime + stale_after < time.time():
            try:
                os.rmdir(path)  # only empty dirs; our lock dirs are bare
            except OSError:
                pass

    def acquire(self) -> "MkdirLock":
        if self._held:
            return self
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                os.mkdir(self.path)
                break
            except FileExistsError:
                self._steal_stale(self.path, self.stale_after)
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"could not acquire mkdir lock {self.path} "
                        f"within {self.timeout:.1f}s"
                    ) from None
                time.sleep(_LOCK_STEP)
        try:
            os.chmod(self.path, RUN_DIR_MODE)
        except OSError:
            pass
        self._held = True
        return self

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            os.rmdir(self.path)
        except FileNotFoundError:
            pass
        except OSError:
            # Non-empty or vanished race; caller may retry. Do not unlink
            # anything inside -- that would violate the lstat-only policy.
            self._held = True

    def is_held(self) -> bool:
        return self._held

    def __enter__(self) -> "MkdirLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def _lock_kind(path: str) -> Optional[str]:
    """Classify an existing lock object: ``"flock"`` (file), ``"mkdir"`` (dir)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(st.st_mode):
        return "mkdir"
    if stat.S_ISREG(st.st_mode):
        return "flock"
    raise SecurityError(f"unexpected lock object at {path}")


def acquire_start_lock(run_dir: str, name: str = DEFAULT_LOCK_NAME,
                       method: str = "auto", timeout: float = 10.0,
                       stale_after: float = 600.0):
    """Acquire the start mutex for ``run_dir``.

    ``method``:
      * ``"auto"`` -- try :class:`FlockLock`; fall back to :class:`MkdirLock`
        when the filesystem rejects ``flock`` (``EOPNOTSUPP``/``ENOSYS``/etc.).
      * ``"flock"`` -- prefer flock locking.
      * ``"mkdir"`` -- prefer mkdir locking.

    flock/mkdir compatibility: the two strategies are interchangeable on a
    shared lock name. Whichever lock object already occupies the path (a lock
    file for flock, a lock dir for mkdir) is respected -- a caller whose
    preferred method differs simply contends on the existing object, so
    mutual exclusion holds across mixed deployments. ``"auto"`` picks flock
    for new locks and adapts to an existing object otherwise.

    Returns the acquired lock object (``release()`` / context manager).
    """
    run_dir = ensure_run_dir(run_dir)
    path = os.path.join(run_dir, name)
    if fcntl is None and method in {"auto", "mkdir"}:
        return MkdirLock(path, timeout=timeout, stale_after=stale_after).acquire()
    if fcntl is None:
        raise OSError(errno.ENOSYS, "flock is unavailable on this platform")
    kind = _lock_kind(path)
    if kind == "mkdir":
        return MkdirLock(path, timeout=timeout, stale_after=stale_after).acquire()
    if kind == "flock":
        return FlockLock(path, timeout=timeout).acquire()
    # Nothing occupies the path yet: use the requested (or auto) method.
    if method == "mkdir":
        return MkdirLock(path, timeout=timeout, stale_after=stale_after).acquire()
    if method == "flock":
        return FlockLock(path, timeout=timeout).acquire()
    # auto
    try:
        return FlockLock(path, timeout=timeout).acquire()
    except OSError as exc:
        if exc.errno in (errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL,
                         errno.ENOTSUP):
            return MkdirLock(path, timeout=timeout, stale_after=stale_after
                             ).acquire()
        raise


# --------------------------------------------------------------------------
# Stale cleanup
# --------------------------------------------------------------------------

def cleanup_stale(run_dir: str, bundle_path: str, socket_path: str,
                  pidfile_name: str = DEFAULT_PIDFILE) -> List[str]:
    """Remove stale pidfile/socket entries; heal a recoverable pidfile.

    Live daemon determination (exact, never pattern-based):

    1. If the pidfile pid is alive AND its ``/proc`` argv exactly matches
       ``bundle_path`` + ``socket_path`` -> nothing is removed.
    2. Else scan ``/proc`` for exactly-matching live pids:
       * found -> rewrite the pidfile with that pid (heal), keep socket.
       * none -> stale: unlink the pidfile and the socket file via
         ``safe_unlink`` (lstat-guarded, no shell/rm).

    Returns a list of human-readable actions performed. Raises
    :class:`SecurityError` if any target is a symlink.
    """
    run_dir = os.path.abspath(run_dir)
    bundle_path = os.path.abspath(bundle_path)
    socket_path = os.path.abspath(socket_path)
    pidfile = os.path.join(run_dir, pidfile_name)

    actions: List[str] = []

    pid = read_pidfile(pidfile)
    live = (
        pid is not None
        and pid_alive(pid)
        and cmdline_matches(pid, bundle_path, socket_path)
    )

    if not live:
        matches = find_matching_pids(bundle_path, socket_path)
        if matches:
            write_pidfile(pidfile, matches[0])
            actions.append(f"healed pidfile with live pid {matches[0]}")
            live = True
        elif pid is not None:
            _reap(pid)  # our own dead child: reap it to avoid a zombie

    pidfile_existed = os.path.lexists(pidfile)
    if not live:
        if pidfile_existed:
            safe_unlink(pidfile)
            actions.append(f"removed stale pidfile {pidfile}")
        if os.path.lexists(socket_path):
            safe_unlink(socket_path)
            actions.append(f"removed stale socket {socket_path}")

    return actions


# --------------------------------------------------------------------------
# Verified stop
# --------------------------------------------------------------------------

def stop_verified_pid(pid: int, bundle_path: str, socket_path: str,
                      grace: float = 5.0, kill_wait: float = 2.0) -> str:
    """Stop ``pid``: SIGTERM -> bounded wait -> SIGKILL.

    The pid is verified against ``/proc`` exact argv (bundle + socket) BEFORE
    any signal is sent; a non-matching pid raises :class:`ProcessVerifyError`
    and is never signalled. Returns ``"none"`` (already dead), ``"term"``
    (exited on SIGTERM) or ``"kill"`` (required SIGKILL).
    """
    pid = int(pid)
    if not cmdline_matches(pid, bundle_path, socket_path):
        raise ProcessVerifyError(
            f"refusing to signal pid {pid}: exact cmdline match "
            f"(bundle={bundle_path}, socket={socket_path}) failed"
        )
    if not pid_alive(pid):
        _reap(pid)
        return "none"

    def send(sig: int) -> None:
        try:
            group_leader = hasattr(os, "getpgid") and os.getpgid(pid) == pid
        except (ProcessLookupError, PermissionError):
            group_leader = False
        if group_leader and hasattr(os, "killpg"):
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)

    send(signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, float(grace))
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            _reap(pid)
            return "term"
        time.sleep(_PROBE_STEP)

    try:
        send(signal.SIGKILL)
    except ProcessLookupError:
        _reap(pid)
        return "gone"
    except PermissionError as exc:
        raise SupervisorError(f"cannot SIGKILL pid {pid}: {exc}") from exc

    deadline = time.monotonic() + max(0.0, float(kill_wait))
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            _reap(pid)
            return "kill"
        time.sleep(_PROBE_STEP)
    return "kill"  # unkillable (D-state); reported, not retried forever


# --------------------------------------------------------------------------
# Status dataclass
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DaemonStatus:
    """Snapshot of daemon state for one bundle/socket pair."""

    name: str
    pid: Optional[int]
    pid_alive: bool
    pid_verified: bool
    socket_reachable: bool
    run_dir: str
    bundle_path: str
    socket_path: str
    pidfile_path: str
    log_path: Optional[str] = None
    error: Optional[str] = None
    stop_reason: Optional[str] = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        """A running daemon = verified pid (alive + exact argv match)."""
        return self.pid is not None and self.pid_alive and self.pid_verified

    @property
    def healthy(self) -> bool:
        """Running AND its daemon socket is actually reachable."""
        return self.running and self.socket_reachable

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "pid": self.pid,
            "pid_alive": self.pid_alive,
            "pid_verified": self.pid_verified,
            "socket_reachable": self.socket_reachable,
            "running": self.running,
            "healthy": self.healthy,
            "run_dir": self.run_dir,
            "bundle_path": self.bundle_path,
            "socket_path": self.socket_path,
            "pidfile_path": self.pidfile_path,
            "log_path": self.log_path,
            "error": self.error,
        }


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

def status(*, run_dir: str, bundle_path: str, socket_path: Optional[str] = None,
           pidfile_name: str = DEFAULT_PIDFILE, log_path: Optional[str] = None,
           name: Optional[str] = None) -> DaemonStatus:
    """Build a :class:`DaemonStatus` without touching any process."""
    run_dir = os.path.abspath(run_dir)
    bundle_path = os.path.abspath(bundle_path)
    socket_path = os.path.abspath(
        socket_path or os.path.join(run_dir, DEFAULT_SOCKET)
    )
    pidfile_path = os.path.join(run_dir, pidfile_name)
    log_path = (
        os.path.abspath(log_path) if log_path else os.path.join(run_dir, DEFAULT_LOG)
    )

    run_ok = True
    error: Optional[str] = None
    try:
        validate_run_dir(run_dir)
    except SupervisorError as exc:
        run_ok = False
        error = str(exc)

    pid: Optional[int] = None
    alive = False
    verified = False
    if run_ok:
        pid = read_pidfile(pidfile_path)
        if pid is not None:
            alive = pid_alive(pid)
            verified = alive and cmdline_matches(pid, bundle_path, socket_path)

    reachable = socket_reachable(socket_path) if run_ok else False

    if name is None:
        name = os.path.splitext(os.path.basename(bundle_path))[0]

    return DaemonStatus(
        name=name,
        pid=pid,
        pid_alive=alive,
        pid_verified=verified,
        socket_reachable=reachable,
        run_dir=run_dir,
        bundle_path=bundle_path,
        socket_path=socket_path,
        pidfile_path=pidfile_path,
        log_path=log_path,
        error=error,
    )


# --------------------------------------------------------------------------
# Start / stop orchestration
# --------------------------------------------------------------------------

def start_daemon(*, bundle_path: str, run_dir: str,
                 socket_path: Optional[str] = None,
                 log_path: Optional[str] = None,
                 pidfile_name: str = DEFAULT_PIDFILE,
                 node: Optional[str] = None,
                 extra_args: Iterable[str] = (),
                 wait_socket: float = 10.0,
                 grace: float = 5.0,
                 kill_wait: float = 2.0,
                 lock_timeout: float = 10.0,
                 lock_method: str = "auto",
                  env: Optional[Dict[str, str]] = None,
                  cwd: Optional[str] = None,
                  name: Optional[str] = None) -> DaemonStatus:
    """Start (or re-attach to) the Prime daemon for ``bundle_path``.

    Single-instance guarantee: the start mutex serializes starters; if a live
    daemon with an exactly-matching argv is already running and its socket is
    reachable, the existing daemon is returned instead of spawning a second.

    Spawn details: ``[node, bundle, "--daemon-socket", socket, *extra_args]``
    via ``subprocess.Popen`` with ``start_new_session=True`` (own session, no
    controlling tty; immune to terminal SIGHUP), stdin ``/dev/null``, and
    stdout/stderr merged into a private log file created with mode 0600.

    Returns a :class:`DaemonStatus`. Raises :class:`StartError` if the daemon
    exits early or fails to open its socket within ``wait_socket`` seconds
    (in which case the spawned child is stopped and leftovers cleaned).
    """
    bundle_path = os.path.abspath(bundle_path)
    run_dir = ensure_run_dir(run_dir)
    socket_path = os.path.abspath(
        socket_path or os.path.join(run_dir, DEFAULT_SOCKET)
    )
    pidfile_path = os.path.join(run_dir, pidfile_name)
    log_path = os.path.abspath(log_path or os.path.join(run_dir, DEFAULT_LOG))
    node_bin = node or shutil.which("node") or "node"
    extra = list(extra_args)

    lock = acquire_start_lock(run_dir, method=lock_method, timeout=lock_timeout)
    try:
        # Re-check under the mutex: already running and healthy?
        current = status(
            run_dir=run_dir, bundle_path=bundle_path, socket_path=socket_path,
            pidfile_name=pidfile_name, log_path=log_path, name=name,
        )
        if current.running:
            if current.socket_reachable:
                return current
            raise StartError(
                f"daemon pid {current.pid} verified alive but socket "
                f"{socket_path} is not reachable; refusing to spawn a duplicate"
            )

        cleanup_stale(run_dir, bundle_path, socket_path,
                      pidfile_name=pidfile_name)

        argv = [node_bin, bundle_path, "--daemon-socket", socket_path, *extra]
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        log_fd = os.open(log_path, flags, LOG_MODE)
        os.chmod(log_path, LOG_MODE)  # private 0600 regardless of umask
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=os.path.abspath(cwd) if cwd else (os.path.dirname(bundle_path) or "."),
                env=env,
            )
        except OSError as exc:
            raise StartError(f"failed to spawn {argv[0]}: {exc}") from exc
        finally:
            os.close(log_fd)

        _register(proc)
        write_pidfile(pidfile_path, proc.pid)

        # Wait for the daemon to actually open its socket (truthful probe).
        ready = False
        deadline = time.monotonic() + max(0.0, float(wait_socket))
        while time.monotonic() < deadline:
            if socket_reachable(socket_path):
                ready = True
                break
            if proc.poll() is not None and not pid_alive(proc.pid):
                break
            time.sleep(_PROBE_STEP)

        rc = proc.poll()
        if not ready:
            if rc is None:
                # Child alive but never opened its socket: stop it, verified.
                try:
                    stop_verified_pid(proc.pid, bundle_path, socket_path,
                                      grace=grace, kill_wait=kill_wait)
                except ProcessVerifyError:
                    pass  # already gone / argv mismatch; cleanup handles it
            else:
                _reap(proc.pid)
            cleanup_stale(run_dir, bundle_path, socket_path,
                          pidfile_name=pidfile_name)
            raise StartError(
                f"daemon exited before opening socket (rc={rc}); "
                f"cleanup done"
            ) from None

        try:
            os.chmod(socket_path, 0o600)
        except OSError:
            pass

        return status(
            run_dir=run_dir, bundle_path=bundle_path, socket_path=socket_path,
            pidfile_name=pidfile_name, log_path=log_path, name=name,
        )
    finally:
        lock.release()


def stop_daemon(*, run_dir: str, bundle_path: str,
                socket_path: Optional[str] = None,
                pidfile_name: str = DEFAULT_PIDFILE,
                grace: float = 5.0,
                kill_wait: float = 2.0,
                force: bool = False,
                name: Optional[str] = None) -> DaemonStatus:
    """Stop the daemon for ``bundle_path``.

    Only exactly-verified pids are signalled:

    1. pidfile pid: alive AND exact argv match -> SIGTERM -> bounded wait ->
       SIGKILL (``stop_verified_pid``). An unrelated pid in the pidfile is
       never signalled -- it is treated as stale and cleaned up instead.
    2. ``force=True``: additionally scan ``/proc`` for any exactly-matching
       live pid and stop it (still exact-match verified, never a pattern kill).

    Returns the final :class:`DaemonStatus` after cleanup.
    """
    run_dir = os.path.abspath(run_dir)
    bundle_path = os.path.abspath(bundle_path)
    socket_path = os.path.abspath(
        socket_path or os.path.join(run_dir, DEFAULT_SOCKET)
    )
    pidfile_path = os.path.join(run_dir, pidfile_name)
    log_path = os.path.join(run_dir, DEFAULT_LOG)

    stop_reason: Optional[str] = None
    pid = read_pidfile(pidfile_path)
    if (
        pid is not None
        and pid_alive(pid)
        and cmdline_matches(pid, bundle_path, socket_path)
    ):
        stop_reason = stop_verified_pid(
            pid, bundle_path, socket_path, grace=grace, kill_wait=kill_wait
        )
    elif force:
        for candidate in find_matching_pids(bundle_path, socket_path):
            stop_reason = stop_verified_pid(
                candidate, bundle_path, socket_path,
                grace=grace, kill_wait=kill_wait,
            )
            break

    try:
        cleanup_stale(run_dir, bundle_path, socket_path,
                      pidfile_name=pidfile_name)
    except SecurityError:
        pass  # symlinked leftovers: surface in status error below

    st = status(
        run_dir=run_dir, bundle_path=bundle_path, socket_path=socket_path,
        pidfile_name=pidfile_name, log_path=log_path, name=name,
    )
    return replace(st, stop_reason=stop_reason)
