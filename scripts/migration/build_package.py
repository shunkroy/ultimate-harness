#!/usr/bin/env python3
"""Build a fail-closed, offline Harness migration package."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import platform
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


PERSISTENT = {"harness.db", "integrity.json", "jobs", "contexts", "context-jobs", "objects"}
SECRET_FILES = {"secrets.json", "secrets.dpapi", "job.key", "object-store.key"}
EXCLUDED = {"run", "tmp", "logs", "cache", "caches", "__pycache__", ".pytest_cache"}
DATABASE_TRANSIENT = {"harness.db-wal", "harness.db-shm", "harness.db-journal"}
OPENSSL_CIPHER = ("enc", "-aes-256-ctr", "-salt", "-pbkdf2", "-iter", "200000")
ENVELOPE_MAGIC = b"H2M1"
ENVELOPE_MAC_BYTES = 32
MAX_GIT_OBJECTS = 100_000
MAX_GIT_BLOB_BYTES = 64 * 1024 * 1024
MAX_GIT_SCAN_BYTES = 2 * 1024 * 1024 * 1024
STRONG_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
)


class PackageError(RuntimeError):
    pass


def run(
    argv: list[str], *, cwd: Path | None = None, capture: bool = True,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, cwd=cwd, check=True, text=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or exc.stdout or "").strip()[-1000:]
        raise PackageError(f"command failed: {Path(argv[0]).name}{': ' + detail if detail else ''}") from exc


def run_bytes(argv: list[str], *, cwd: Path, timeout: int = 60) -> bytes:
    try:
        completed = subprocess.run(
            argv, cwd=cwd, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PackageError(f"command failed: {Path(argv[0]).name}") from exc
    return completed.stdout


def optional_git(repo: Path, *args: str) -> str:
    try:
        return run(["git", *args], cwd=repo, timeout=30).stdout.strip()
    except PackageError:
        return ""


def sanitize_repository_url(value: str) -> str:
    if not value:
        return "unconfigured"
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https", "ssh"} and parsed.hostname:
        host = parsed.hostname
        if parsed.port:
            host += f":{parsed.port}"
        return parsed._replace(netloc=host, params="", query="", fragment="").geturl()
    if re.match(r"^[^@\s]+@[^:\s]+:.+$", value):
        return value.split("@", 1)[1]
    return value if "@" not in value else "redacted-remote"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def private_write(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def reject_symlinks(root: Path, label: str) -> None:
    if root.is_symlink():
        raise PackageError(f"{label} is a symlink")
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            candidate = Path(base, name)
            if candidate.is_symlink():
                raise PackageError(f"symlink refused in {label}: {candidate.relative_to(root)}")


def validate_inputs(args: argparse.Namespace) -> tuple[Path, Path, Path, Path, str]:
    raw_repo = Path(args.repo).expanduser()
    raw_state = Path(args.state).expanduser()
    if raw_repo.is_symlink() or raw_state.is_symlink():
        raise PackageError("repo and state root must not be symlinks")
    repo = raw_repo.resolve()
    state_root = raw_state.resolve()
    output = Path(args.output).expanduser().absolute()
    key_file = Path(args.key_file).expanduser().absolute()
    if not repo.is_dir() or not (repo / ".git").exists():
        raise PackageError("repo is not a Git worktree")
    if not state_root.is_dir():
        raise PackageError("state root is not a directory")
    if output.exists():
        raise PackageError("output already exists")
    if not output.parent.is_dir():
        raise PackageError("output parent does not exist")
    if output.parent.is_symlink():
        raise PackageError("output parent must not be a symlink")
    if is_within(output, repo) or is_within(output, state_root):
        raise PackageError("output must be outside repo and state root")
    if is_within(key_file, output) or is_within(key_file, repo) or is_within(key_file, state_root):
        raise PackageError("key file must be outside package, repository, and state root")
    parsed = urlparse(args.ci_url)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.netloc
        or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise PackageError("CI URL must be an absolute credential-free HTTP(S) URL")

    reject_symlinks(state_root, "state root")
    entries = {item.name for item in state_root.iterdir()}
    unknown = sorted(entries - PERSISTENT - SECRET_FILES - EXCLUDED - DATABASE_TRANSIENT)
    if unknown:
        raise PackageError("unknown state-root entries refused: " + ", ".join(unknown))

    dirty = run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo).stdout
    if dirty.strip():
        raise PackageError("repository is dirty")
    head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip().lower()
    sealed = args.sealed_sha.strip().lower()
    if len(sealed) != 40 or any(ch not in "0123456789abcdef" for ch in sealed):
        raise PackageError("sealed SHA must be a full 40-character commit SHA")
    if head != sealed:
        raise PackageError("sealed SHA does not match HEAD")
    baseline = (getattr(args, "baseline_sha", "") or "").strip().lower()
    if baseline:
        if len(baseline) != 40 or any(ch not in "0123456789abcdef" for ch in baseline):
            raise PackageError("baseline SHA must be a full 40-character commit SHA")
        run(["git", "cat-file", "-e", f"{baseline}^{{commit}}"], cwd=repo, timeout=30)
        run(["git", "merge-base", "--is-ancestor", baseline, head], cwd=repo, timeout=30)

    records = run(["git", "ls-files", "-s", "-z"], cwd=repo).stdout.split("\0")
    for record in filter(None, records):
        mode, _object_id, _stage_path = record.split(None, 2)
        rel = _stage_path.split("\t", 1)[-1]
        source = repo / rel
        if mode == "120000" or source.is_symlink():
            raise PackageError(f"tracked symlink refused: {rel}")
        if mode == "160000" or not source.is_file():
            raise PackageError(f"unsupported tracked entry refused: {rel}")

    if not bool(getattr(args, "require_service_paused", False)):
        raise PackageError("--require-service-paused is mandatory for a migration snapshot")

    pidfiles = [state_root / "run" / "service.pid"]
    heartbeat = state_root / "run" / "service-heartbeat.json"
    pids: list[int] = []
    for pidfile in pidfiles:
        if not pidfile.exists():
            continue
        try:
            pids.append(int(pidfile.read_text(encoding="ascii").strip()))
        except (OSError, ValueError) as exc:
            raise PackageError("cannot prove service is paused: invalid pid file") from exc
    if heartbeat.exists():
        try:
            heartbeat_value = json.loads(heartbeat.read_text(encoding="utf-8"))
            heartbeat_pid = heartbeat_value.get("service_pid")
            if heartbeat_pid is not None:
                pids.append(int(heartbeat_pid))
        except (OSError, ValueError, AttributeError, json.JSONDecodeError) as exc:
            raise PackageError("cannot prove service is paused: invalid heartbeat") from exc
    for pid in set(pids):
        if pid <= 0:
            raise PackageError("cannot prove service is paused: invalid pid")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise PackageError("cannot prove service is paused") from exc
        else:
            raise PackageError(f"live service refused (pid {pid})")
    proc_root = Path("/proc")
    if proc_root.is_dir():
        expected_home = str(state_root).encode("utf-8", "surrogatepass")
        for entry in proc_root.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                command = (entry / "cmdline").read_bytes()[:64 * 1024]
                environment = (entry / "environ").read_bytes()[:1024 * 1024]
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            if (
                b"harness2" in command and b"supervise" in command
                and (b"HARNESS2_HOME=" + expected_home + b"\0") in environment
            ):
                raise PackageError(f"unrecorded live Harness service refused (pid {entry.name})")
    validate_quiescent_records(state_root)
    return repo, state_root, output, key_file, head


def validate_quiescent_records(state_root: Path) -> None:
    database = state_root / "harness.db"
    if database.is_file():
        try:
            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                if "jobs" in tables:
                    rows = connection.execute(
                        "SELECT id,status FROM jobs WHERE status NOT IN ('succeeded','dead','cancelled')"
                    ).fetchall()
                    if rows:
                        raise PackageError("nonterminal legacy jobs must be resolved before migration")
                if "kernel_tasks" in tables:
                    rows = connection.execute(
                        "SELECT task_id,state FROM kernel_tasks WHERE state NOT IN ('completed','failed','cancelled')"
                    ).fetchall()
                    if rows:
                        raise PackageError("nonterminal typed tasks must be resolved before migration")
        except sqlite3.Error as exc:
            raise PackageError(f"cannot validate quiescent database state: {exc}") from exc
    context_jobs = state_root / "context-jobs"
    if context_jobs.is_dir():
        for path in sorted(context_jobs.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PackageError(f"context job metadata is unreadable: {path.name}") from exc
            if not isinstance(value, dict) or str(value.get("status", "")).lower() not in {
                "completed", "succeeded", "failed", "dead", "cancelled",
            }:
                raise PackageError(f"nonterminal context job must be resolved: {path.name}")


def state_fingerprint(state_root: Path) -> str:
    digest = hashlib.sha256(b"harness2-state-snapshot/v1\0")
    selected = (PERSISTENT - {"harness.db"}) | SECRET_FILES
    for top in sorted(selected):
        source = state_root / top
        if not source.exists():
            continue
        paths = [source] if source.is_file() else [source, *sorted(source.rglob("*"))]
        for path in paths:
            relative = path.relative_to(state_root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            metadata = path.lstat()
            digest.update(int(metadata.st_mode).to_bytes(8, "big", signed=False))
            digest.update(int(getattr(metadata, "st_uid", 0)).to_bytes(8, "big", signed=False))
            digest.update(int(getattr(metadata, "st_gid", 0)).to_bytes(8, "big", signed=False))
            digest.update(int(metadata.st_size).to_bytes(8, "big", signed=False))
            digest.update(int(metadata.st_mtime_ns).to_bytes(16, "big", signed=True))
            if stat.S_ISLNK(metadata.st_mode):
                raise PackageError(f"symlink appeared during state snapshot: {path}")
            if stat.S_ISDIR(metadata.st_mode):
                digest.update(b"D")
            elif stat.S_ISREG(metadata.st_mode):
                digest.update(b"F")
                with path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
            else:
                raise PackageError(f"unsupported state entry during snapshot: {path}")
    return digest.hexdigest()


@contextmanager
def state_snapshot_guard(state_root: Path):
    """Block SQLite writers while file-state stability is checked around copies."""

    database = state_root / "harness.db"
    connection: sqlite3.Connection | None = None
    root_fd: int | None = None
    try:
        if os.name == "posix":
            root_fd = os.open(
                state_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        if root_fd is not None:
            try:
                database_metadata = os.stat("harness.db", dir_fd=root_fd, follow_symlinks=False)
                if not stat.S_ISREG(database_metadata.st_mode):
                    raise PackageError("harness.db became a non-regular entry")
                has_database = True
            except FileNotFoundError:
                has_database = False
        else:
            has_database = database.is_file()
        if has_database:
            guarded_database = state_entry_path(state_root, root_fd, "harness.db")
            connection = sqlite3.connect(guarded_database, timeout=0.25)
            connection.execute("BEGIN IMMEDIATE")
        yield root_fd
        if root_fd is not None:
            opened = os.fstat(root_fd)
            current = state_root.stat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise PackageError("state root identity changed during migration snapshot")
    except (OSError, sqlite3.Error) as exc:
        raise PackageError(f"cannot acquire quiescent SQLite snapshot guard: {exc}") from exc
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()
        if root_fd is not None:
            os.close(root_fd)


def state_entry_path(state_root: Path, root_fd: int | None, name: str) -> Path:
    if root_fd is not None and Path("/proc/self/fd").is_dir():
        return Path(f"/proc/self/fd/{root_fd}/{name}")
    return state_root / name


def ensure_key(path: Path) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise PackageError("key parent must be a real directory")
    if os.name != "nt":
        parent = path.parent.stat()
        if parent.st_mode & 0o077:
            raise PackageError("key parent directory must not be group/world accessible")
        if hasattr(os, "getuid") and parent.st_uid != os.getuid():
            raise PackageError("key parent directory has the wrong owner")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise PackageError("key file must be a regular non-symlink file")
        value = path.read_bytes()
        if not re.fullmatch(rb"[0-9a-f]{64}\n?", value):
            raise PackageError("existing migration key must be a 256-bit lowercase hex value")
        if os.name != "nt":
            mode = path.stat().st_mode & 0o777
            if mode != 0o600:
                raise PackageError("existing migration key must have mode 0600")
            if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
                raise PackageError("existing migration key has the wrong owner")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (secrets.token_hex(32) + "\n").encode("ascii"))
    finally:
        os.close(fd)


def read_key(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PackageError("migration key is unavailable") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise PackageError("migration key is not a regular file")
        if os.name != "nt":
            if (metadata.st_mode & 0o777) != 0o600:
                raise PackageError("migration key must have mode 0600")
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise PackageError("migration key has the wrong owner")
        value = os.read(fd, 4097)
    finally:
        os.close(fd)
    if not re.fullmatch(rb"[0-9a-f]{64}\n?", value):
        raise PackageError("migration key must be a 256-bit lowercase hex value")
    return value


def mac_key(key: bytes) -> bytes:
    return hmac.new(key, b"harness2-migration-mac/v1", hashlib.sha256).digest()


@contextmanager
def private_key_file(key: bytes):
    fd, name = tempfile.mkstemp(prefix=".harness2-migration-key-")
    path = Path(name)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        os.write(fd, key)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        yield path
    finally:
        if fd >= 0:
            os.close(fd)
        path.unlink(missing_ok=True)


def _copy_open_regular(source_fd: int, destination: Path, label: object) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_fd = -1
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise PackageError(f"non-regular file refused: {label}")
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        while True:
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            view = memoryview(block)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise PackageError(f"destination write failed: {destination}")
                view = view[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
        ):
            raise PackageError(f"source file changed while copied: {label}")
    except Exception:
        if destination_fd >= 0:
            os.close(destination_fd)
            destination_fd = -1
        destination.unlink(missing_ok=True)
        raise
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
    os.chmod(destination, 0o600)


def copy_regular(source: Path, destination: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise PackageError(f"non-regular file refused: {source}") from exc
    try:
        _copy_open_regular(source_fd, destination, source)
    finally:
        os.close(source_fd)


def snapshot_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
        with closing(sqlite3.connect(f"file:{destination}?mode=ro&immutable=1", uri=True)) as check:
            rows = check.execute("PRAGMA integrity_check").fetchall()
            foreign_key_errors = check.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        raise PackageError(f"SQLite snapshot failed: {exc}") from exc
    if rows != [("ok",)]:
        raise PackageError("SQLite full integrity_check failed")
    if foreign_key_errors:
        raise PackageError("SQLite foreign_key_check failed")
    os.chmod(destination, 0o600)


def copy_tree(source: Path, destination: Path) -> int:
    if os.name == "posix" and hasattr(os, "fwalk"):
        return copy_tree_by_descriptor(source, destination)
    count = 0
    for base, dirs, files in os.walk(source, followlinks=False):
        dirs.sort()
        files.sort()
        rel_base = Path(base).relative_to(source)
        target_directory = destination / rel_base
        target_directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(target_directory, 0o700)
        for name in files:
            copy_regular(Path(base, name), destination / rel_base / name)
            count += 1
    return count


def copy_tree_by_descriptor(source: Path, destination: Path) -> int:
    """Copy a tree relative to stable directory descriptors without following links."""

    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(source, root_flags)
    except OSError as exc:
        raise PackageError(f"state directory is unavailable: {source}") from exc
    try:
        return copy_tree_from_descriptor(root_fd, destination)
    finally:
        os.close(root_fd)


def copy_tree_from_descriptor(root_fd: int, destination: Path) -> int:
    count = 0
    descriptor = os.dup(root_fd)
    try:
        try:
            walker = os.fwalk(".", topdown=True, follow_symlinks=False, dir_fd=descriptor)
            for base, dirs, files, directory_fd in walker:
                dirs.sort()
                files.sort()
                relative_base = Path() if base == "." else Path(base)
                target_directory = destination / relative_base
                target_directory.mkdir(parents=True, mode=0o700, exist_ok=True)
                os.chmod(target_directory, 0o700)
                for name in dirs:
                    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(metadata.st_mode):
                        raise PackageError(f"non-directory entry refused in state tree: {relative_base / name}")
                for name in files:
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    try:
                        source_fd = os.open(name, flags, dir_fd=directory_fd)
                    except OSError as exc:
                        raise PackageError(
                            f"non-regular file refused in state tree: {relative_base / name}"
                        ) from exc
                    try:
                        _copy_open_regular(
                            source_fd, target_directory / name, relative_base / name,
                        )
                    finally:
                        os.close(source_fd)
                    count += 1
        except OSError as exc:
            raise PackageError("state tree changed or became unsafe while copied") from exc
    finally:
        os.close(descriptor)
    return count


def write_tree_checksums(root: Path) -> None:
    lines = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name != "STATE_CHECKSUMS.sha256":
            lines.append(f"{sha256(path)}  {path.relative_to(root).as_posix()}")
    private_write(root / "STATE_CHECKSUMS.sha256", "\n".join(lines) + ("\n" if lines else ""))


def stage_persistent(
    state_root: Path, destination: Path, *, root_fd: int | None = None,
) -> tuple[list[dict[str, object]], int]:
    inventory: list[dict[str, object]] = []
    file_count = 0
    destination.mkdir(parents=True, mode=0o700)
    for name in sorted(PERSISTENT):
        source = state_entry_path(state_root, root_fd, name)
        if root_fd is not None:
            try:
                metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
        else:
            if not source.exists():
                continue
            metadata = source.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise PackageError(f"symlink refused in persistent state: {name}")
        target = destination / name
        if name == "harness.db":
            if not stat.S_ISREG(metadata.st_mode):
                raise PackageError("harness.db is not a regular file")
            snapshot_database(source, target)
            count = 1
        elif stat.S_ISDIR(metadata.st_mode):
            if root_fd is not None and os.name == "posix" and hasattr(os, "fwalk"):
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                child_fd = os.open(name, flags, dir_fd=root_fd)
                try:
                    count = copy_tree_from_descriptor(child_fd, target)
                finally:
                    os.close(child_fd)
            else:
                count = copy_tree(source, target)
        elif stat.S_ISREG(metadata.st_mode):
            if root_fd is not None:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                source_fd = os.open(name, flags, dir_fd=root_fd)
                try:
                    _copy_open_regular(source_fd, target, name)
                finally:
                    os.close(source_fd)
            else:
                copy_regular(source, target)
            count = 1
        else:
            raise PackageError(f"unsupported persistent state entry: {name}")
        file_count += count
        inventory.append({
            "name": name,
            "kind": "directory" if stat.S_ISDIR(metadata.st_mode) else "file",
            "files": count,
        })
    write_tree_checksums(destination)
    return inventory, file_count


def validate_staged_state(
    staged: Path, staged_secrets: Path, openssl: str,
) -> None:
    """Validate copied cross-resource state without mutating the live source."""

    restore_path = Path(__file__).resolve().parent / "restore_state.py"
    spec = importlib.util.spec_from_file_location("harness2_migration_restore_validation", restore_path)
    if spec is None or spec.loader is None:
        raise PackageError("migration restore validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.validate_database(staged)
        module.validate_and_relocate_context_state(staged, staged, relocate=False)
    except Exception as exc:
        raise PackageError(f"staged state validation failed: {exc}") from exc

    source_root = str(Path(__file__).resolve().parents[2])
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from harness2.context.package import ContextPackage as RuntimeContextPackage
    try:
        contexts = staged / "contexts"
        if contexts.is_dir():
            for entry in sorted(contexts.iterdir()):
                package = RuntimeContextPackage.load(str(entry))
                if package.root != str(entry.resolve()) or package.ir.context_id != entry.name:
                    raise PackageError("runtime context package identity is inconsistent")
    except Exception as exc:
        raise PackageError(f"runtime context package validation failed: {exc}") from exc

    context_jobs = staged / "context-jobs"
    version_two: list[dict[str, object]] = []
    if context_jobs.is_dir():
        for path in sorted(context_jobs.glob("*.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("schema") == "harness.context-job/v2":
                version_two.append(value)
    if not version_two:
        return
    key_path = staged_secrets / "object-store.key"
    if key_path.is_symlink() or not key_path.is_file():
        raise PackageError("object-store key is required to authenticate context snapshots")
    key = key_path.read_bytes()
    if len(key) != 32:
        raise PackageError("object-store key has an invalid size")

    from harness2.kernel.payloads import PayloadReference
    from harness2.storage.local import LocalAuthenticatedStorage

    storage = object.__new__(LocalAuthenticatedStorage)
    storage.root = str(staged / "objects")
    storage.key_path = str(key_path)
    storage.master = key
    storage.openssl_bin = openssl
    storage.key_id = hashlib.sha256(b"harness-object-key/v1\0" + key).hexdigest()[:32]
    database = staged / "harness.db"
    try:
        with closing(sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            for job in version_two:
                row = connection.execute(
                    "SELECT s.*,r.backend_id,r.object_key,r.reference_id,"
                    "r.content_sha256 AS ref_content_sha256,r.size_bytes AS ref_size_bytes,"
                    "r.media_type AS ref_media_type,r.schema_id,r.purpose,r.envelope_version,"
                    "r.key_id,r.reference_mac FROM kernel_source_snapshots s "
                    "JOIN kernel_payload_references r ON r.reference_id=s.reference_id "
                    "WHERE s.snapshot_id=?",
                    (job["snapshot_id"],),
                ).fetchone()
                if row is None:
                    raise PackageError("context snapshot metadata is missing")
                reference = PayloadReference(
                    str(row["reference_id"]), str(row["backend_id"]), str(row["object_key"]),
                    str(row["ref_content_sha256"]), int(row["ref_size_bytes"]),
                    str(row["ref_media_type"]), str(row["schema_id"]), str(row["purpose"]),
                    int(row["envelope_version"]), str(row["key_id"]), str(row["reference_mac"]),
                )
                object_path = staged / "objects" / reference.object_key[:2] / f"{reference.object_key[2:]}.blob"
                if object_path.is_symlink() or not object_path.is_file():
                    raise PackageError("context snapshot object is missing")
                content = storage.get(reference, binding={
                    "source_type": str(row["source_type"]),
                    "source_identifier_hash": str(row["source_identifier_hash"]),
                    "source_revision": str(row["source_revision"] or ""),
                    "purpose": "context.source",
                })
                expected_snapshot_id = "snapshot-" + hashlib.sha256(
                    b"harness.source-snapshot/v1\0" + reference.reference_id.encode()
                    + b"\0" + str(row["source_identifier_hash"]).encode()
                ).hexdigest()
                try:
                    snapshot_metadata = json.loads(str(row["metadata_json"]))
                except json.JSONDecodeError as exc:
                    raise PackageError("context snapshot metadata is invalid") from exc
                if (
                    expected_snapshot_id != str(row["snapshot_id"])
                    or reference.schema_id != "harness.source-snapshot/1"
                    or reference.purpose != "context.source"
                    or reference.size_bytes != int(row["size_bytes"])
                    or reference.media_type != str(row["media_type"])
                    or not isinstance(snapshot_metadata, dict)
                    or json.dumps(snapshot_metadata, sort_keys=True, separators=(",", ":")) != str(row["metadata_json"])
                    or hashlib.sha256(content).hexdigest() != str(row["content_sha256"])
                    or len(content) != int(row["size_bytes"])
                    or str(row["source_revision"] or "") != str(job["id"])
                ):
                    raise PackageError("context snapshot content or binding is inconsistent")
    except sqlite3.Error as exc:
        raise PackageError(f"context snapshot validation failed: {exc}") from exc


def file_inventory(root: Path) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "STATE_CHECKSUMS.sha256":
            continue
        relative = path.relative_to(root).as_posix()
        values.append({
            "path": relative,
            "size": path.stat().st_size,
            "mode": oct(path.stat().st_mode & 0o777),
            "sha256": sha256(path),
        })
    return values


def tar_add_tree(archive: tarfile.TarFile, source: Path, prefix: str) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise PackageError(f"symlink refused while archiving: {path}")
        arcname = str(PurePosixPath(prefix) / path.relative_to(source).as_posix())
        if path.is_dir():
            info = tarfile.TarInfo(arcname + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o700
            info.mtime = 0
            archive.addfile(info)
        elif path.is_file():
            info = archive.gettarinfo(str(path), arcname=arcname)
            info.mode = 0o700 if path.stat().st_mode & 0o111 else 0o600
            info.mtime = 0
            with path.open("rb") as handle:
                archive.addfile(info, handle)
        else:
            raise PackageError(f"unsupported archive entry: {path}")


def make_tar(path: Path, trees: list[tuple[Path, str]], files: list[tuple[Path, str]] = ()) -> None:
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        for tree, prefix in trees:
            tar_add_tree(archive, tree, prefix)
        for source, arcname in files:
            if source.is_symlink() or not source.is_file():
                raise PackageError(f"non-regular archive input: {source}")
            info = archive.gettarinfo(str(source), arcname=arcname)
            info.mode = 0o600
            info.mtime = 0
            with source.open("rb") as handle:
                archive.addfile(info, handle)


def validate_tar(path: Path) -> None:
    with tarfile.open(path, "r:*") as archive:
        for member in archive.getmembers():
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts or member.issym() or member.islnk():
                raise PackageError("unsafe archive member generated")
            if not (member.isdir() or member.isfile()):
                raise PackageError("unsupported archive member generated")
            if member.isfile():
                handle = archive.extractfile(member)
                if handle is None:
                    raise PackageError("generated archive member is unreadable")
                while handle.read(1024 * 1024):
                    pass


def encrypt(openssl: str, key: bytes, source: Path, destination: Path) -> None:
    with tempfile.NamedTemporaryFile(prefix="migration-cipher-", delete=False) as handle:
        ciphertext = Path(handle.name)
    with tempfile.NamedTemporaryFile(prefix="migration-decrypt-", suffix=".tar", delete=False) as handle:
        decrypted = Path(handle.name)
    try:
        with private_key_file(key) as password_file:
            run([openssl, *OPENSSL_CIPHER, "-pass", f"file:{password_file}", "-in", str(source), "-out", str(ciphertext)])
        authentication = hmac.new(mac_key(key), digestmod=hashlib.sha256)
        authentication.update(ENVELOPE_MAGIC)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as output, ciphertext.open("rb") as encrypted:
                output.write(ENVELOPE_MAGIC)
                for block in iter(lambda: encrypted.read(1024 * 1024), b""):
                    authentication.update(block)
                    output.write(block)
                output.write(authentication.digest())
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        decrypt_envelope(openssl, key, destination, decrypted)
        validate_tar(decrypted)
    finally:
        ciphertext.unlink(missing_ok=True)
        decrypted.unlink(missing_ok=True)


def decrypt_envelope(openssl: str, key: bytes, source: Path, destination: Path) -> None:
    size = source.stat().st_size
    if size <= len(ENVELOPE_MAGIC) + ENVELOPE_MAC_BYTES:
        raise PackageError("encrypted migration envelope is truncated")
    with tempfile.NamedTemporaryFile(prefix="migration-cipher-", delete=False) as handle:
        ciphertext = Path(handle.name)
    try:
        authentication = hmac.new(mac_key(key), digestmod=hashlib.sha256)
        remaining = size - ENVELOPE_MAC_BYTES
        with source.open("rb") as encrypted, ciphertext.open("wb") as output:
            magic = encrypted.read(len(ENVELOPE_MAGIC))
            if magic != ENVELOPE_MAGIC:
                raise PackageError("encrypted migration envelope has an invalid header")
            authentication.update(magic)
            remaining -= len(magic)
            while remaining:
                block = encrypted.read(min(1024 * 1024, remaining))
                if not block:
                    raise PackageError("encrypted migration envelope is truncated")
                authentication.update(block)
                output.write(block)
                remaining -= len(block)
            supplied = encrypted.read(ENVELOPE_MAC_BYTES)
        if not hmac.compare_digest(supplied, authentication.digest()):
            raise PackageError("encrypted migration envelope authentication failed")
        with private_key_file(key) as password_file:
            run([openssl, *OPENSSL_CIPHER, "-d", "-pass", f"file:{password_file}", "-in", str(ciphertext), "-out", str(destination)])
    finally:
        ciphertext.unlink(missing_ok=True)


def tracked_files(repo: Path) -> list[str]:
    return sorted(filter(None, run(["git", "ls-files", "-z"], cwd=repo).stdout.split("\0")))


def copy_tracked(repo: Path, destination: Path) -> int:
    count = 0
    for rel in tracked_files(repo):
        source = repo / rel
        target = destination / rel
        copy_regular(source, target)
        if source.stat().st_mode & 0o111:
            os.chmod(target, 0o700)
        count += 1
    return count


def verify_source_stage(repo: Path, destination: Path, head: str) -> None:
    records = run(["git", "ls-tree", "-r", "-z", "--full-tree", head], cwd=repo).stdout.split("\0")
    expected: dict[str, str] = {}
    for record in filter(None, records):
        metadata, relative = record.split("\t", 1)
        mode, kind, object_id = metadata.split(" ", 2)
        if kind != "blob" or mode == "120000":
            raise PackageError(f"unsupported sealed source object: {relative}")
        expected[relative] = object_id
    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*") if path.is_file()
    }
    if actual != set(expected):
        raise PackageError("staged source inventory does not match the sealed commit")
    for relative, object_id in expected.items():
        observed = run(
            ["git", "hash-object", str(destination / relative)], cwd=repo, timeout=30,
        ).stdout.strip()
        if observed != object_id:
            raise PackageError(f"staged source differs from sealed commit: {relative}")


def secret_names(state_root: Path) -> list[str]:
    path = state_root / "secrets.json"
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError("secrets.json is unreadable") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PackageError("secrets.json must contain a JSON object")
    return sorted(key for key in value if key and "\n" not in key and "=" not in key)


def secret_material(state_root: Path) -> tuple[bytes, ...]:
    """Collect opaque current secret values for exact history/plaintext scans."""

    values: set[bytes] = set()
    for name in SECRET_FILES:
        path = state_root / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise PackageError(f"secret file is unsafe or too large: {name}")
        raw = path.read_bytes()
        if 8 <= len(raw) <= 1024 * 1024:
            values.add(raw)
        if name == "secrets.json":
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise PackageError("secrets.json is unreadable") from exc

            def collect(value: object) -> None:
                if isinstance(value, str):
                    encoded = value.encode("utf-8")
                    if len(encoded) >= 8:
                        values.add(encoded)
                elif isinstance(value, dict):
                    for item in value.values():
                        collect(item)
                elif isinstance(value, list):
                    for item in value:
                        collect(item)

            collect(parsed)
    return tuple(sorted(values, key=lambda item: (len(item), item)))


def validate_git_history(repo: Path, head: str, secrets_to_find: tuple[bytes, ...]) -> dict[str, object]:
    lines = run(["git", "rev-list", "--objects", head], cwd=repo, timeout=120).stdout.splitlines()
    object_ids = list(dict.fromkeys(line.split(" ", 1)[0] for line in lines if line))
    if len(object_ids) > MAX_GIT_OBJECTS:
        raise PackageError("Git history exceeds the configured object scan limit")
    blobs = 0
    scanned = 0
    for object_id in object_ids:
        if run(["git", "cat-file", "-t", object_id], cwd=repo, timeout=30).stdout.strip() != "blob":
            continue
        try:
            size = int(run(["git", "cat-file", "-s", object_id], cwd=repo, timeout=30).stdout.strip())
        except ValueError as exc:
            raise PackageError("Git object size is invalid") from exc
        if size < 0 or size > MAX_GIT_BLOB_BYTES or scanned + size > MAX_GIT_SCAN_BYTES:
            raise PackageError("Git history exceeds the configured plaintext scan byte limit")
        content = run_bytes(["git", "cat-file", "blob", object_id], cwd=repo, timeout=60)
        if len(content) != size:
            raise PackageError("Git blob changed or was read incompletely")
        if any(secret and secret in content for secret in secrets_to_find):
            raise PackageError("known secret material exists in sealed Git history")
        if any(pattern.search(content) for pattern in STRONG_SECRET_PATTERNS):
            raise PackageError("probable credential exists in sealed Git history")
        blobs += 1
        scanned += size
    return {"scope": head, "objects": len(object_ids), "blobs": blobs, "bytes": scanned, "result": "no-match"}


def verify_sealed_bundle(bundle: Path, sealed_sha: str) -> list[str]:
    heads = run(["git", "bundle", "list-heads", str(bundle)], cwd=bundle.parent, timeout=60).stdout.splitlines()
    if heads != [f"{sealed_sha} HEAD"]:
        raise PackageError("normal Git bundle does not contain exactly the sealed HEAD")
    with tempfile.TemporaryDirectory(prefix="harness-bundle-verify-") as temp_name:
        bare = Path(temp_name) / "repository.git"
        run(["git", "init", "--bare", str(bare)], cwd=Path(temp_name), timeout=30)
        run(["git", "bundle", "verify", str(bundle)], cwd=bare, timeout=60)
    return heads


def scan_plaintext_tree(root: Path, secrets_to_find: tuple[bytes, ...]) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix == ".enc" or path.name == "repository.bundle":
            continue
        if path.stat().st_size > MAX_GIT_BLOB_BYTES:
            raise PackageError(f"package plaintext file exceeds scan limit: {path.name}")
        content = path.read_bytes()
        if any(secret and secret in content for secret in secrets_to_find):
            raise PackageError("known secret material leaked into normal package plaintext")


def build(args: argparse.Namespace) -> dict[str, Path]:
    repo, state_root, output, key_file, head = validate_inputs(args)
    branch = optional_git(repo, "branch", "--show-current") or "detached"
    repository_url = sanitize_repository_url(optional_git(repo, "remote", "get-url", "origin"))
    baseline_sha = getattr(args, "baseline_sha", "") or ""
    if baseline_sha and (
        len(baseline_sha) != 40
        or any(character not in "0123456789abcdef" for character in baseline_sha.lower())
    ):
        raise PackageError("baseline SHA must be a full commit SHA")
    source_platform = getattr(args, "source_platform", "") or platform.platform()
    local_test_summary = getattr(args, "local_test_summary", "") or "not supplied"
    if (
        not isinstance(source_platform, str) or not isinstance(local_test_summary, str)
        or len(source_platform) > 500 or len(local_test_summary) > 2000
        or any(character in local_test_summary for character in "\r\n")
    ):
        raise PackageError("source platform or local test attestation is invalid")
    readiness_verified = bool(getattr(args, "readiness_verified", False))
    if not readiness_verified:
        raise PackageError("final readiness verification must be explicitly confirmed")
    openssl = shutil.which(args.openssl) if os.sep not in args.openssl else args.openssl
    if not openssl or not Path(openssl).is_file():
        raise PackageError("OpenSSL executable is unavailable")

    normal_tar = output.parent / f"{output.name}.tar.gz"
    secret_enc = output.parent / f"{output.name}.secret-transfer.tar.enc"
    emergency_enc = output.parent / f"{output.name}.emergency-source-state.tar.enc"
    attestation = output.parent / f"{output.name}.build-attestation.json"
    artifact_paths = [normal_tar, secret_enc, emergency_enc, attestation]
    artifact_paths += [Path(str(path) + ".sha256") for path in artifact_paths]
    if any(path.exists() for path in artifact_paths):
        raise PackageError("one or more output artifacts already exist")
    key_existed = key_file.exists() or key_file.is_symlink()
    ensure_key(key_file)
    key_material = read_key(key_file)

    created: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix=f".{output.name}.build-", dir=output.parent) as temp_name:
            work = Path(temp_name)
            package = work / output.name
            package.mkdir(mode=0o700)
            (package / "config").mkdir(mode=0o700)
            (package / "reports").mkdir(mode=0o700)
            (package / "verification").mkdir(mode=0o700)
            (package / "docs").mkdir(mode=0o700)

            persistent = work / "persistent"
            secret_stage = work / "secret-stage"
            secret_stage.mkdir(mode=0o700)
            secret_inventory: list[str] = []
            with state_snapshot_guard(state_root) as state_fd:
                validate_quiescent_records(state_root)
                before_state = state_fingerprint(state_root)
                inventory, state_file_count = stage_persistent(
                    state_root, persistent, root_fd=state_fd,
                )
                for name in sorted(SECRET_FILES):
                    if state_fd is not None:
                        try:
                            metadata = os.stat(name, dir_fd=state_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if not stat.S_ISREG(metadata.st_mode):
                            raise PackageError(f"secret state entry is not regular: {name}")
                        source_fd = os.open(
                            name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=state_fd,
                        )
                        try:
                            _copy_open_regular(source_fd, secret_stage / name, name)
                        finally:
                            os.close(source_fd)
                    else:
                        source = state_root / name
                        if not source.exists():
                            continue
                        copy_regular(source, secret_stage / name)
                    secret_inventory.append(name)
                names = secret_names(secret_stage)
                known_secrets = secret_material(secret_stage)
                supplied_evidence = (source_platform + "\n" + local_test_summary + "\n" + args.ci_url).encode("utf-8")
                if any(secret and secret in supplied_evidence for secret in known_secrets):
                    raise PackageError("operator-supplied migration evidence contains secret material")
                validate_staged_state(persistent, secret_stage, str(openssl))
                if (persistent / "harness.db").is_file():
                    stability_database = work / "stability-check.db"
                    snapshot_database(
                        state_entry_path(state_root, state_fd, "harness.db"), stability_database,
                    )
                    if sha256(stability_database) != sha256(persistent / "harness.db"):
                        raise PackageError("SQLite state changed during migration snapshot")
                    stability_database.unlink()
                after_state = state_fingerprint(state_root)
                if before_state != after_state:
                    raise PackageError("state changed during migration snapshot")

            persistent_files = file_inventory(persistent)
            private_plain = work / "private-state.tar"
            make_tar(private_plain, [(persistent, "state")])
            encrypt(openssl, key_material, private_plain, package / "private-state.tar.enc")

            secret_plain = work / "secret-transfer.tar"
            make_tar(secret_plain, [(secret_stage, "secrets")])
            encrypt(openssl, key_material, secret_plain, work / secret_enc.name)

            history_scan = validate_git_history(repo, head, known_secrets)
            bundle = package / "repository.bundle"
            run(["git", "bundle", "create", str(bundle), "HEAD"], cwd=repo)
            bundle_heads = verify_sealed_bundle(bundle, head)
            os.chmod(bundle, 0o600)

            emergency_bundle = work / "full-history.bundle"
            run(["git", "bundle", "create", str(emergency_bundle), "--all"], cwd=repo)
            with tempfile.TemporaryDirectory(prefix="harness-full-bundle-") as bare_name:
                bare = Path(bare_name) / "repository.git"
                run(["git", "init", "--bare", str(bare)], cwd=Path(bare_name), timeout=30)
                run(["git", "bundle", "verify", str(emergency_bundle)], cwd=bare, timeout=60)
            os.chmod(emergency_bundle, 0o600)

            source_stage = work / "source"
            source_stage.mkdir(mode=0o700)
            source_count = copy_tracked(repo, source_stage)
            verify_source_stage(repo, source_stage, head)
            emergency_plain = work / "emergency-source-state.tar"
            make_tar(
                emergency_plain,
                [(source_stage, "source"), (persistent, "state"), (secret_stage, "secrets")],
                [(emergency_bundle, "repository.bundle")],
            )
            encrypt(openssl, key_material, emergency_plain, work / emergency_enc.name)

            private_write(package / "config" / "env.example", "".join(f"{name}=\n" for name in names))

            script_root = Path(__file__).resolve().parent
            for name in ("restore_state.py", "verify-package.sh", "verify-after-clone.sh"):
                copy_regular(script_root / name, package / "verification" / name)
            os.chmod(package / "verification" / "restore_state.py", 0o700)
            os.chmod(package / "verification" / "verify-package.sh", 0o700)
            os.chmod(package / "verification" / "verify-after-clone.sh", 0o700)

            timestamp = datetime.now(timezone.utc).isoformat()
            private_state_hash = sha256(package / "private-state.tar.enc")
            secret_transfer_hash = sha256(work / secret_enc.name)
            emergency_hash = sha256(work / emergency_enc.name)
            required_versions = {
                "python": ">=3.11",
                "observed_python": platform.python_version(),
                "git": optional_git(repo, "--version") or "required",
                "openssl": run([str(openssl), "version"], timeout=30).stdout.strip()[:200],
            }
            manifest = {
                "schema": "harness2.migration-package/v1",
                "created_at": timestamp,
                "checkpoint": "3C",
                "source": {
                    "repository": repository_url,
                    "branch": branch,
                    "sealed_commit_sha": head,
                    "baseline_commit_sha": baseline_sha or None,
                    "tracked_source_status": "clean",
                    "tracked_file_count": source_count,
                    "normal_bundle_scope": "sealed HEAD and required ancestry only",
                    "normal_bundle_heads": bundle_heads,
                    "history_secret_scan": history_scan,
                },
                "source_platform": source_platform,
                "source_architecture": platform.machine() or "unknown",
                "required_runtime_versions": required_versions,
                "state": {
                    "persistent": inventory,
                    "files": persistent_files,
                    "file_count": state_file_count,
                    "encrypted_archive": "private-state.tar.enc",
                    "encrypted_archive_sha256": private_state_hash,
                    "sqlite_backup": "online-backup-api",
                    "sqlite_integrity_check": "ok" if (state_root / "harness.db").exists() else "not_present",
                    "stable_pre_post_fingerprint": "matched",
                    "nonterminal_work": "none",
                },
                "secrets": {
                    "excluded_from_package": secret_inventory,
                    "required_variable_names": names,
                    "transferred_separately": bool(secret_inventory),
                    "encrypted_artifact": secret_enc.name,
                    "encrypted_artifact_sha256": secret_transfer_hash,
                    "key_material": "excluded; transfer separately",
                },
                "rebuildable_files_excluded": sorted(EXCLUDED | DATABASE_TRANSIENT),
                "platform_specific_files": [
                    "bin/harness", "bin/prime-cli", "bin/harness.cmd", "bin/harness.ps1",
                    "deploy/linux/", "deploy/macos/", "deploy/windows/",
                ],
                "test_results": {
                    "local_operator_attestation": local_test_summary,
                    "ci_operator_attestation": "reported green; not independently queried by offline builder",
                    "ci_run_url": args.ci_url,
                    "attested_for_commit": head,
                },
                "encryption": {
                    "cipher": "AES-256-CTR", "kdf": "PBKDF2", "iterations": 200000,
                    "authentication": "HMAC-SHA256 encrypt-then-MAC",
                },
                "emergency_backup": {
                    "artifact": emergency_enc.name,
                    "sha256": emergency_hash,
                    "contains": ["tracked source", "verified full-history Git bundle", "persistent state", "secrets"],
                    "restore_status": "manual recovery path; normal state restore is mechanically tested",
                },
                "artifacts": [
                    "private-state.tar.enc", "repository.bundle", "config/env.example",
                    "reports/build-report.json", "reports/build-report.md",
                    "reports/state-inventory.json", "reports/state-inventory.md",
                    "reports/inventory-policy.json",
                    "reports/test-report.md", "reports/dependency-report.md",
                    "reports/platform-audit.md", "reports/migration-readiness.md",
                    "verification/restore_state.py", "verification/verify-package.sh",
                    "verification/verify-after-clone.sh", "docs/migration/", "docs/architecture/CHECKPOINT_3C.md",
                ],
                "checksum_policy": "CHECKSUMS.sha256 excludes itself; the package tar is covered by an external sidecar",
                "migration_package_checksum": {
                    "value": None,
                    "reason": "archive cannot contain its own digest; verify the trusted external sidecar before extraction",
                    "external_sidecar": f"{normal_tar.name}.sha256",
                    "external_attestation": attestation.name,
                },
                "restore_instructions": "README.md and verification/restore_state.py",
                "verification_commands": [
                    "verification/verify-package.sh . /separate/path/to/key",
                    "verification/verify-after-clone.sh . /path/to/clone /separate/path/to/key",
                ],
                "rollback_instructions": "use the tested normal state restore plus sealed bundle; emergency full-history extraction remains a documented manual fallback",
            }
            report = {
                "schema": "harness2.migration-report/v1",
                "result": "built_and_internally_verified",
                "commit": head,
                "branch": branch,
                "source_platform": source_platform,
                "ci_attestation_url": args.ci_url,
                "ci_attestation_scope": head,
                "ci_independently_queried": False,
                "repository_files": source_count,
                "persistent_state_files": state_file_count,
                "secret_files_transferred": len(secret_inventory),
                "sqlite_online_backup": (state_root / "harness.db").exists(),
                "sqlite_integrity_check": "ok" if (state_root / "harness.db").exists() else "not-present",
                "git_bundle_verify": "ok",
                "git_bundle_heads": bundle_heads,
                "git_history_secret_scan": history_scan,
                "encrypted_archive_readback": "ok",
                "service_pause_required_and_checked": True,
                "operator_readiness_attested": readiness_verified,
                "outer_artifact_checksum": "pending external sidecar and build attestation",
            }
            json_write(package / "MANIFEST.json", manifest)
            json_write(package / "reports" / "build-report.json", report)
            json_write(package / "reports" / "state-inventory.json", {
                "schema": "harness2.state-inventory/v1",
                "persistent": persistent_files,
                "secret_files_excluded": secret_inventory,
                "secret_variable_names": names,
                "rebuildable_excluded": sorted(EXCLUDED | DATABASE_TRANSIENT),
            })
            json_write(package / "reports" / "inventory-policy.json", {
                "schema": "harness2.migration-inventory-policy/v1",
                "persistent": sorted(PERSISTENT),
                "secrets": sorted(SECRET_FILES),
                "rebuildable_excluded": sorted(EXCLUDED | DATABASE_TRANSIENT),
                "unknown_entry_policy": "refuse",
                "symlink_policy": "refuse",
            })
            private_write(
                package / "reports" / "build-report.md",
                "# Migration build report\n\n"
                f"- Result: built and internally verified\n- Commit: `{head}`\n"
                f"- CI operator attestation (not queried by this offline builder): {args.ci_url}\n"
                f"- Tracked source files: {source_count}\n- Persistent state files: {state_file_count}\n"
                f"- Secret files transferred separately: {len(secret_inventory)}\n"
                "- SQLite stability/integrity, scoped Git bundle, history secret scan, and encrypted archive readback: verified\n"
                "- Outer archive checksum: recorded after package creation in the external build attestation\n",
            )
            private_write(
                package / "reports" / "state-inventory.md",
                "# State inventory\n\n"
                f"- Persistent files: {state_file_count}\n"
                f"- Secret files excluded and encrypted separately: {len(secret_inventory)}\n"
                f"- Rebuildable/transient categories excluded: {', '.join(sorted(EXCLUDED | DATABASE_TRANSIENT))}\n"
                "- File-level sizes, modes, and SHA-256 values: `state-inventory.json`\n",
            )
            private_write(
                package / "reports" / "test-report.md",
                "# Verification evidence\n\n"
                f"- Local operator attestation: {local_test_summary}\n"
                f"- CI operator attestation (not independently queried here): {args.ci_url}\n"
                f"- Sealed commit: `{head}`\n",
            )
            private_write(
                package / "reports" / "dependency-report.md",
                "# Dependency report\n\n"
                "- Python: >=3.11\n- Git: required for preferred/bundle restore\n"
                "- OpenSSL: required for encrypted state and object storage\n"
                "- Linux/Termux installer: POSIX shell, curl, coreutils\n"
                "- OpenCode, Prime Agent, Hermes, Node: optional capability providers\n"
                "- Android/Termux API integrations: optional and not implemented by Checkpoint 3C\n",
            )
            private_write(
                package / "reports" / "platform-audit.md",
                "# Platform audit\n\n"
                f"- Source platform: {source_platform}\n- Architecture: {platform.machine() or 'unknown'}\n"
                "- Android/Termux and Linux: supported runtime targets\n"
                "- macOS and Windows: import/CLI smoke-tested only\n"
                "- Portable core has no required provider CLI; unavailable providers fail closed\n",
            )
            readiness = (
                ("SOURCE REPRODUCIBLE", "PASS — clean sealed SHA and scoped bundle verified"),
                ("STATE INVENTORIED", "PASS — exact file inventory generated"),
                ("STATE BACKED UP", "PASS — authenticated archive readback passed"),
                ("DATABASES VERIFIED", "PASS — stable online backups and integrity checks passed"),
                ("SECRETS EXCLUDED FROM GIT", "PASS — current secret exact-match and strong-pattern history scan passed"),
                ("TERMUX PATHS AUDITED", "DOCUMENTED — see packaged reference audit"),
                ("PORTABILITY ISSUES RESOLVED", "OPERATOR ATTESTED — see local evidence"),
                ("PHONE STILL SUPPORTED", "TRACKED CLAIM — confirm with sealed CI/local suite"),
                ("CLEAN INSTALL VERIFIED", "OPERATOR ATTESTED — not rerun by package builder"),
                ("CI GREEN", "OPERATOR ATTESTED — immutable URL supplied, not queried by builder"),
                ("MIGRATION PACKAGE BUILT", "PASS"),
                ("PACKAGE CHECKSUM VERIFIED", "PENDING EXTERNAL — use sidecar/build attestation before extraction"),
                ("ROLLBACK AVAILABLE", "NORMAL PATH TESTED; EMERGENCY EXTRACTION MANUAL"),
            )
            readiness_lines = "\n".join(f"- {label}: {status}" for label, status in readiness)
            private_write(
                package / "reports" / "migration-readiness.md",
                "# Migration readiness\n\n" + readiness_lines + "\n",
            )
            migration_docs = repo / "docs" / "migration"
            if migration_docs.is_dir():
                copy_tree(migration_docs, package / "docs" / "migration")
            checkpoint_doc = repo / "docs" / "architecture" / "CHECKPOINT_3C.md"
            if checkpoint_doc.is_file() and not checkpoint_doc.is_symlink():
                copy_regular(checkpoint_doc, package / "docs" / "architecture" / checkpoint_doc.name)
            private_write(
                package / "SOURCE.txt",
                f"repository={repository_url}\nbranch={branch}\ncommit={head}\nci_url={args.ci_url}\n",
            )
            private_write(
                package / "README.md",
                "# Harness migration package\n\n"
                "This offline package contains a scoped, verified Git bundle and encrypted persistent state. "
                "Classified current secret values are only in the separately encrypted secret-transfer and emergency archives; "
                "the encryption key is excluded and must travel separately.\n\n"
                f"Before extraction, verify `{normal_tar.name}.sha256` against an independently transferred digest. "
                "Then run `verification/verify-package.sh . /path/to/key` before use. Restore only into a "
                "fresh state directory with `verification/restore_state.py`. `CHECKSUMS.sha256` "
                "intentionally excludes itself; internal checksums prove consistency, not publisher authenticity.\n",
            )

            if run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip().lower() != head:
                raise PackageError("repository HEAD changed during package build")
            if run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo).stdout.strip():
                raise PackageError("repository changed during package build")
            scan_plaintext_tree(package, known_secrets)
            checksum_lines = []
            for path in sorted(item for item in package.rglob("*") if item.is_file()):
                if path.name != "CHECKSUMS.sha256":
                    checksum_lines.append(f"{sha256(path)}  {path.relative_to(package).as_posix()}")
            private_write(package / "CHECKSUMS.sha256", "\n".join(checksum_lines) + "\n")
            run(
                [str(package / "verification" / "verify-package.sh"), str(package), str(key_file)],
                cwd=package, timeout=600,
            )

            package.rename(output)
            created.append(output)
            shutil.move(str(work / secret_enc.name), secret_enc)
            created.append(secret_enc)
            shutil.move(str(work / emergency_enc.name), emergency_enc)
            created.append(emergency_enc)
            with tarfile.open(normal_tar, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                archive.add(output, arcname=output.name, recursive=True)
            validate_tar(normal_tar)
            os.chmod(normal_tar, 0o600)
            created.append(normal_tar)

        for artifact in (normal_tar, secret_enc, emergency_enc):
            sidecar = Path(str(artifact) + ".sha256")
            private_write(sidecar, f"{sha256(artifact)}  {artifact.name}\n")
            created.append(sidecar)
        json_write(attestation, {
            "schema": "harness2.external-build-attestation/v1",
            "sealed_commit_sha": head,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ci": {
                "status": "operator_attested_green_not_independently_queried",
                "run_url": args.ci_url,
                "commit": head,
            },
            "local_test_attestation": local_test_summary,
            "checks": {
                "package_internal_verifier": "passed",
                "encrypted_state_readback": "passed",
                "git_bundle_scope_and_integrity": "passed",
                "state_snapshot_stability": "passed",
            },
            "artifacts": {
                artifact.name: {"sha256": sha256(artifact), "sidecar": f"{artifact.name}.sha256"}
                for artifact in (normal_tar, secret_enc, emergency_enc)
            },
        })
        created.append(attestation)
        attestation_sidecar = Path(str(attestation) + ".sha256")
        private_write(attestation_sidecar, f"{sha256(attestation)}  {attestation.name}\n")
        created.append(attestation_sidecar)
        return {
            "package": output, "package_tar": normal_tar,
            "secret_transfer": secret_enc, "emergency": emergency_enc,
            "attestation": attestation,
        }
    except Exception:
        for path in reversed(created):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        if not key_existed:
            key_file.unlink(missing_ok=True)
        raise


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--repo", required=True)
    value.add_argument("--state", required=True)
    value.add_argument("--output", required=True)
    value.add_argument("--key-file", required=True)
    value.add_argument("--sealed-sha", required=True)
    value.add_argument("--baseline-sha", default="")
    value.add_argument("--ci-url", required=True)
    value.add_argument("--source-platform", default="")
    value.add_argument("--local-test-summary", default="not supplied")
    value.add_argument("--readiness-verified", action="store_true")
    value.add_argument("--openssl", default="openssl")
    value.add_argument("--require-service-paused", action="store_true")
    return value


def main(argv: list[str] | None = None) -> int:
    try:
        artifacts = build(parser().parse_args(argv))
    except PackageError as exc:
        print(f"migration package refused: {exc}", file=sys.stderr)
        return 2
    for name, path in artifacts.items():
        print(f"{name}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
