#!/usr/bin/env python3
"""Safely restore an encrypted Harness persistent-state archive."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath


OPENSSL_CIPHER = ("enc", "-aes-256-ctr", "-salt", "-pbkdf2", "-iter", "200000")
ENVELOPE_MAGIC = b"H2M1"
ENVELOPE_MAC_BYTES = 32
TERMINAL = {"succeeded", "failed", "dead", "cancelled", "completed"}
SECRET_FILES = {"secrets.json", "secrets.dpapi", "job.key", "object-store.key"}
PERSISTENT = {"harness.db", "integrity.json", "jobs", "contexts", "context-jobs", "objects"}
MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_ENCRYPTED_ENVELOPE_BYTES = 5 * 1024 * 1024 * 1024
MAX_PATH_BYTES = 4096
MAX_CHECKSUM_BYTES = 16 * 1024 * 1024
MAX_CONTEXT_JOB_BYTES = 1024 * 1024
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CONTEXT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SNAPSHOT_ID = re.compile(r"^snapshot-[0-9a-f]{64}$")


class RestoreError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_key(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RestoreError("migration key is unavailable") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RestoreError("migration key is not a regular file")
        if os.name != "nt":
            if (metadata.st_mode & 0o777) != 0o600:
                raise RestoreError("migration key must have mode 0600")
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise RestoreError("migration key has the wrong owner")
        value = os.read(fd, 4097)
    finally:
        os.close(fd)
    if not re.fullmatch(rb"[0-9a-f]{64}\n?", value):
        raise RestoreError("migration key must be a 256-bit lowercase hex value")
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


def decrypt(openssl: str, key: Path | bytes, archive: Path, destination: Path) -> None:
    key_material = key if isinstance(key, bytes) else read_key(key)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        archive_fd = os.open(archive, flags)
    except OSError as exc:
        raise RestoreError("encrypted migration envelope is unavailable") from exc
    metadata = os.fstat(archive_fd)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(archive_fd)
        raise RestoreError("encrypted migration envelope is not a regular file")
    size = metadata.st_size
    if size > MAX_ENCRYPTED_ENVELOPE_BYTES:
        os.close(archive_fd)
        raise RestoreError("encrypted migration envelope exceeds its size limit")
    if size <= len(ENVELOPE_MAGIC) + ENVELOPE_MAC_BYTES:
        os.close(archive_fd)
        raise RestoreError("encrypted migration envelope is truncated")
    ciphertext = destination.with_suffix(destination.suffix + ".cipher")
    try:
        authentication = hmac.new(mac_key(key_material), digestmod=hashlib.sha256)
        remaining = size - ENVELOPE_MAC_BYTES
        encrypted = os.fdopen(archive_fd, "rb")
        archive_fd = -1
        with encrypted, ciphertext.open("xb") as output:
            magic = encrypted.read(len(ENVELOPE_MAGIC))
            if magic != ENVELOPE_MAGIC:
                raise RestoreError("encrypted migration envelope has an invalid header")
            authentication.update(magic)
            remaining -= len(magic)
            while remaining:
                block = encrypted.read(min(1024 * 1024, remaining))
                if not block:
                    raise RestoreError("encrypted migration envelope is truncated")
                authentication.update(block)
                output.write(block)
                remaining -= len(block)
            supplied = encrypted.read(ENVELOPE_MAC_BYTES)
            after = os.fstat(encrypted.fileno())
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
            ):
                raise RestoreError("encrypted migration envelope changed while reading")
        if not hmac.compare_digest(supplied, authentication.digest()):
            raise RestoreError("encrypted migration envelope authentication failed")
        with private_key_file(key_material) as password_file:
            subprocess.run(
                [openssl, *OPENSSL_CIPHER, "-d", "-pass", f"file:{password_file}", "-in", str(ciphertext), "-out", str(destination)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=600,
            )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RestoreError("archive decryption failed") from exc
    finally:
        if archive_fd >= 0:
            os.close(archive_fd)
        ciphertext.unlink(missing_ok=True)


def safe_extract(archive_path: Path, staging: Path) -> None:
    try:
        archive = tarfile.open(archive_path, "r:*")
    except (OSError, tarfile.TarError) as exc:
        raise RestoreError("decrypted archive is not a readable tar") from exc
    with archive:
        members = []
        for member in archive:
            members.append(member)
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise RestoreError("archive member limit exceeded")
        seen: set[str] = set()
        total_bytes = 0
        for member in members:
            pure = PurePosixPath(member.name)
            if (
                pure.is_absolute() or ".." in pure.parts or not pure.parts
                or pure.parts[0] != "state" or "\\" in member.name
                or len(member.name.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES
                or (
                    len(pure.parts) > 1
                    and pure.parts[1] not in PERSISTENT | {"STATE_CHECKSUMS.sha256"}
                )
            ):
                raise RestoreError(f"unsafe or unexpected archive path: {member.name}")
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise RestoreError(f"unsupported archive member: {member.name}")
            normalized = pure.as_posix().rstrip("/")
            if normalized in seen:
                raise RestoreError(f"duplicate archive member: {member.name}")
            seen.add(normalized)
            if len(pure.parts) == 1 and not member.isdir():
                raise RestoreError("state archive root must be a directory")
            if member.isfile():
                if member.size < 0 or member.size > MAX_ARCHIVE_FILE_BYTES:
                    raise RestoreError(f"archive member size limit exceeded: {member.name}")
                total_bytes += member.size
                if total_bytes > MAX_ARCHIVE_TOTAL_BYTES:
                    raise RestoreError("archive aggregate size limit exceeded")
        for member in members:
            relative = PurePosixPath(member.name).relative_to("state")
            if not relative.parts:
                continue
            destination = staging.joinpath(*relative.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = archive.extractfile(member)
            if source is None:
                raise RestoreError(f"unreadable archive member: {member.name}")
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                copied = 0
                while copied < member.size:
                    block = source.read(min(1024 * 1024, member.size - copied))
                    if not block:
                        raise RestoreError(f"truncated archive member: {member.name}")
                    output.write(block)
                    copied += len(block)
                if source.read(1):
                    raise RestoreError(f"archive member exceeded declared size: {member.name}")


def validate_state_inventory(root: Path) -> None:
    entries = {entry.name: entry for entry in root.iterdir()}
    unknown = set(entries) - PERSISTENT - {"STATE_CHECKSUMS.sha256"}
    if unknown:
        raise RestoreError("restored state contains unknown top-level entries")
    for name in ("harness.db", "integrity.json", "STATE_CHECKSUMS.sha256"):
        entry = entries.get(name)
        if entry is not None and (entry.is_symlink() or not entry.is_file()):
            raise RestoreError(f"restored state entry must be a regular file: {name}")
    for name in ("jobs", "contexts", "context-jobs", "objects"):
        entry = entries.get(name)
        if entry is not None and (entry.is_symlink() or not entry.is_dir()):
            raise RestoreError(f"restored state entry must be a directory: {name}")


def verify_state_checksums(root: Path) -> None:
    checksum_file = root / "STATE_CHECKSUMS.sha256"
    if not checksum_file.is_file():
        raise RestoreError("state checksum file is missing")
    if checksum_file.stat().st_size > MAX_CHECKSUM_BYTES:
        raise RestoreError("state checksum file exceeds its size limit")
    seen: set[str] = set()
    for line in checksum_file.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            expected, relative = line.split("  ", 1)
        except ValueError as exc:
            raise RestoreError("malformed state checksum record") from exc
        pure = PurePosixPath(relative)
        if (
            not _HEX64.fullmatch(expected) or not relative or "\\" in relative
            or not pure.parts or pure.is_absolute() or ".." in pure.parts or "" in pure.parts
            or relative in seen or pure.name == "STATE_CHECKSUMS.sha256"
        ):
            raise RestoreError("unsafe state checksum path")
        seen.add(relative)
        path = root.joinpath(*pure.parts)
        if not path.is_file() or sha256(path) != expected:
            raise RestoreError(f"state checksum mismatch: {relative}")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != checksum_file
    }
    if seen != actual:
        raise RestoreError("state checksum inventory does not exactly match extracted files")


def _read_context_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CONTEXT_JOB_BYTES:
        raise RestoreError(f"invalid context job metadata: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RestoreError(f"invalid context job metadata: {path.name}") from exc
    if not isinstance(value, dict):
        raise RestoreError(f"invalid context job metadata: {path.name}")
    return value


def _validate_context_package(path: Path, context_id: str, version: str) -> None:
    if (
        not _CONTEXT_ID.fullmatch(context_id) or not version or len(version) > 128
        or path.is_symlink() or not path.is_dir() or path.name != context_id
    ):
        raise RestoreError(f"context package is missing or unsafe: {context_id}")
    try:
        manifest_path = path / "manifest.json"
        ir_path = path / "ir.json"
        if manifest_path.is_symlink() or ir_path.is_symlink():
            raise RestoreError(f"context package contains a symlink: {context_id}")
        if manifest_path.stat().st_size > 1024 * 1024 or ir_path.stat().st_size > 16 * 1024 * 1024:
            raise RestoreError(f"context package metadata exceeds limits: {context_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ir_bytes = ir_path.read_bytes()
        ir = json.loads(ir_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RestoreError(f"context package is unreadable: {context_id}") from exc
    if not isinstance(manifest, dict) or not isinstance(ir, dict):
        raise RestoreError(f"context package metadata is invalid: {context_id}")
    ir_meta = manifest.get("ir")
    source_meta = manifest.get("source")
    ir_source = ir.get("source")
    if not isinstance(ir_meta, dict) or not isinstance(source_meta, dict) or not isinstance(ir_source, dict):
        raise RestoreError(f"context package manifest is incomplete: {context_id}")
    source_rel = source_meta.get("path")
    if not isinstance(source_rel, str) or not source_rel or "\\" in source_rel:
        raise RestoreError(f"context source path is invalid: {context_id}")
    source_pure = PurePosixPath(source_rel)
    if (
        source_pure.is_absolute() or ".." in source_pure.parts
        or len(source_pure.parts) != 2 or source_pure.parts[0] != "sources"
    ):
        raise RestoreError(f"context source path escapes package: {context_id}")
    source_path = path.joinpath(*source_pure.parts)
    if source_path.is_symlink() or not source_path.is_file() or source_path.stat().st_size > 16 * 1024 * 1024:
        raise RestoreError(f"context source is missing: {context_id}")
    actual_files = {
        item.relative_to(path).as_posix()
        for item in path.rglob("*") if item.is_file()
    }
    actual_dirs = {
        item.relative_to(path).as_posix()
        for item in path.rglob("*") if item.is_dir()
    }
    if (
        actual_files != {"manifest.json", "ir.json", source_pure.as_posix()}
        or actual_dirs != {"sources"}
    ):
        raise RestoreError(f"context package file inventory is invalid: {context_id}")
    source_bytes = source_path.read_bytes()
    if (
        manifest.get("schema") != "harness.context/v1"
        or ir.get("schema") != "harness.context-ir/v1"
        or manifest.get("context_id") != context_id or manifest.get("version") != version
        or ir.get("context_id") != context_id or ir.get("version") != version
        or ir_meta.get("path") != "ir.json" or not _HEX64.fullmatch(str(ir_meta.get("sha256", "")))
        or hashlib.sha256(ir_bytes).hexdigest() != ir_meta.get("sha256")
        or not _HEX64.fullmatch(str(source_meta.get("sha256", "")))
        or hashlib.sha256(source_bytes).hexdigest() != source_meta.get("sha256")
        or ir_source.get("path") != source_meta.get("path")
        or ir_source.get("sha256") != source_meta.get("sha256")
        or manifest.get("permissions") != []
        or ir.get("permissions") != []
        or manifest.get("operations") != ir.get("operations")
        or manifest.get("name") != ir.get("name")
        or source_meta != ir_source
    ):
        raise RestoreError(f"context package integrity failed: {context_id}")


def validate_and_relocate_context_state(root: Path, target: Path, *, relocate: bool) -> None:
    contexts = root / "contexts"
    jobs = root / "context-jobs"
    if contexts.exists():
        if contexts.is_symlink() or not contexts.is_dir():
            raise RestoreError("contexts entry is not a directory")
        for entry in contexts.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                raise RestoreError(f"unexpected context package entry: {entry.name}")
            # Unreferenced packages are still persistent state and must verify.
            try:
                manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RestoreError(f"context package is unreadable: {entry.name}") from exc
            _validate_context_package(entry, entry.name, str(manifest.get("version", "")))
    if not jobs.exists():
        return
    if jobs.is_symlink() or not jobs.is_dir():
        raise RestoreError("context-jobs entry is not a directory")
    database = root / "harness.db"
    snapshot_rows: dict[str, dict[str, object]] = {}
    if database.is_file():
        try:
            with closing(sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                if {"kernel_source_snapshots", "kernel_payload_references"}.issubset(tables):
                    snapshot_rows = {
                        str(row["snapshot_id"]): dict(row)
                        for row in connection.execute(
                            "SELECT s.snapshot_id,s.reference_id,s.source_type,s.source_identifier_hash,"
                            "s.source_revision,s.content_sha256,s.size_bytes,s.media_type,s.metadata_json,"
                            "r.object_key,r.content_sha256 AS ref_content_sha256,"
                            "r.size_bytes AS ref_size_bytes,r.media_type AS ref_media_type,"
                            "r.schema_id,r.purpose "
                            "FROM kernel_source_snapshots s JOIN kernel_payload_references r "
                            "ON r.reference_id=s.reference_id"
                        )
                    }
        except sqlite3.Error as exc:
            raise RestoreError(f"context snapshot metadata is unreadable: {exc}") from exc
    entries = sorted(jobs.iterdir())
    for path in entries:
        value = _read_context_json(path)
        job_id = value.get("id")
        schema = value.get("schema")
        status = str(value.get("status", "")).lower()
        if not isinstance(job_id, str) or not _HEX32.fullmatch(job_id) or path.name != f"{job_id}.json":
            raise RestoreError(f"context job identity mismatch: {path.name}")
        if status not in TERMINAL:
            raise RestoreError(f"nonterminal context job cannot be restored: {path.name}")
        required = {"schema", "id", "status", "created_at", "updated_at", "name", "version", "attempt", "result", "error_code"}
        if schema == "harness.context-job/v2":
            required.add("snapshot_id")
        elif schema == "harness.context-job/v1":
            required.add("source")
        else:
            raise RestoreError(f"unsupported context job schema: {path.name}")
        if set(value) != required:
            raise RestoreError(f"context job fields do not match schema: {path.name}")
        if (
            not isinstance(value.get("name"), str) or not str(value["name"]).strip()
            or len(str(value["name"])) > 256
            or not isinstance(value.get("version"), str) or not str(value["version"]).strip()
            or len(str(value["version"])) > 128
            or isinstance(value.get("attempt"), bool) or not isinstance(value.get("attempt"), int)
            or int(value["attempt"]) < 0
            or not all(
                isinstance(value.get(field), (int, float))
                and not isinstance(value.get(field), bool)
                and math.isfinite(float(value[field]))
                for field in ("created_at", "updated_at")
            )
            or (value.get("error_code") is not None and not isinstance(value.get("error_code"), str))
        ):
            raise RestoreError(f"context job values do not match schema: {path.name}")
        if schema == "harness.context-job/v2":
            snapshot_id = value.get("snapshot_id")
            if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
                raise RestoreError(f"context snapshot identity is invalid: {path.name}")
            snapshot = snapshot_rows.get(snapshot_id)
            if (
                snapshot is None or snapshot["source_type"] != "context-job.file"
                or snapshot["source_revision"] != job_id
            ):
                raise RestoreError(f"context snapshot binding is missing or invalid: {path.name}")
            expected_snapshot_id = "snapshot-" + hashlib.sha256(
                b"harness.source-snapshot/v1\0" + str(snapshot["reference_id"]).encode()
                + b"\0" + str(snapshot["source_identifier_hash"]).encode()
            ).hexdigest()
            try:
                metadata = json.loads(str(snapshot["metadata_json"]))
            except json.JSONDecodeError as exc:
                raise RestoreError(f"context snapshot metadata is invalid: {path.name}") from exc
            if (
                expected_snapshot_id != snapshot_id
                or snapshot["schema_id"] != "harness.source-snapshot/1"
                or snapshot["purpose"] != "context.source"
                or int(snapshot["size_bytes"]) != int(snapshot["ref_size_bytes"])
                or snapshot["media_type"] != snapshot["ref_media_type"]
                or not isinstance(metadata, dict)
                or json.dumps(metadata, sort_keys=True, separators=(",", ":")) != snapshot["metadata_json"]
            ):
                raise RestoreError(f"context snapshot metadata is inconsistent: {path.name}")
            object_key = snapshot["object_key"]
            if not isinstance(object_key, str) or not _HEX64.fullmatch(object_key):
                raise RestoreError(f"context snapshot object identity is invalid: {path.name}")
            object_path = root / "objects" / object_key[:2] / f"{object_key[2:]}.blob"
            if object_path.is_symlink() or not object_path.is_file():
                raise RestoreError(f"context snapshot object is missing: {path.name}")
        result = value.get("result")
        if status in {"succeeded", "completed"}:
            if not isinstance(result, dict) or set(result) != {"context_id", "version", "package"}:
                raise RestoreError(f"successful context job result is invalid: {path.name}")
            context_id = result.get("context_id")
            version = result.get("version")
            if not isinstance(context_id, str) or not context_id or not isinstance(version, str) or not version:
                raise RestoreError(f"successful context job identity is invalid: {path.name}")
            _validate_context_package(contexts / context_id, context_id, version)
            if relocate:
                result["package"] = str(target / "contexts" / context_id)
        elif result is not None:
            raise RestoreError(f"terminal failed context job must not contain a result: {path.name}")
        if relocate and schema == "harness.context-job/v1":
            source = value.get("source")
            source_hash = hashlib.sha256(
                str(source).encode("utf-8", "surrogatepass")
            ).hexdigest()
            value["source"] = f"redacted:sha256:{source_hash}"
        if relocate:
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)


def validate_and_relocate_database(root: Path, target: Path) -> None:
    database = root / "harness.db"
    if not database.exists():
        return
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database)
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        if rows != [("ok",)]:
            raise RestoreError("restored SQLite integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RestoreError("restored SQLite foreign_key_check failed")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "kernel_tasks" in tables:
            active = connection.execute(
                "SELECT task_id,state FROM kernel_tasks WHERE state NOT IN ('completed','failed','cancelled')"
            ).fetchall()
            if active:
                raise RestoreError("restored state contains nonterminal typed tasks")
        if "jobs" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            required = {"id", "status", "payload_path", "cwd"}
            if not required.issubset(columns):
                raise RestoreError("jobs table lacks required migration columns")
            jobs = connection.execute("SELECT id,status,payload_path,cwd FROM jobs").fetchall()
            updates: list[tuple[str, object]] = []
            for job_id, status, payload_path, _cwd in jobs:
                nonterminal = str(status).lower() not in TERMINAL
                if nonterminal:
                    raise RestoreError(f"restored state contains nonterminal job {job_id}")
                if payload_path is None:
                    continue
                raw = str(payload_path).replace("\\", "/")
                pure = PurePosixPath(raw)
                basename = pure.name
                if basename != f"{job_id}.bin" or len(pure.parts) < 2 or pure.parts[-2] != "jobs":
                    raise RestoreError(f"job {job_id} has unsafe payload path")
                restored = str(target / "jobs" / basename)
                updates.append((restored, job_id))
            with connection:
                connection.executemany("UPDATE jobs SET payload_path=? WHERE id=?", updates)
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        if rows != [("ok",)]:
            raise RestoreError("SQLite integrity_check failed after relocation")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RestoreError("SQLite foreign_key_check failed after relocation")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if not journal_mode or str(journal_mode[0]).lower() != "delete":
            raise RestoreError("restored SQLite database could not be normalized for transfer")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RestoreError("SQLite integrity_check failed after journal normalization")
    except sqlite3.Error as exc:
        raise RestoreError(f"SQLite restore validation failed: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(database) + suffix)
        if sidecar.is_symlink():
            raise RestoreError("unsafe SQLite sidecar after relocation")
        sidecar.unlink(missing_ok=True)


def validate_database(root: Path) -> None:
    database = root / "harness.db"
    if not database.exists():
        return
    try:
        with closing(sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)) as connection:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "jobs" in tables and connection.execute(
                "SELECT 1 FROM jobs WHERE status NOT IN ('succeeded','failed','dead','cancelled','completed') LIMIT 1"
            ).fetchone():
                raise RestoreError("state contains nonterminal legacy jobs")
            if "kernel_tasks" in tables and connection.execute(
                "SELECT 1 FROM kernel_tasks WHERE state NOT IN ('completed','failed','cancelled') LIMIT 1"
            ).fetchone():
                raise RestoreError("state contains nonterminal typed tasks")
    except sqlite3.Error as exc:
        raise RestoreError(f"SQLite restore validation failed: {exc}") from exc
    if rows != [("ok",)]:
        raise RestoreError("restored SQLite integrity_check failed")
    if foreign_key_errors:
        raise RestoreError("restored SQLite foreign_key_check failed")


def _validated_inputs(args: argparse.Namespace) -> tuple[Path, bytes, str]:
    raw_archive = Path(args.archive).expanduser()
    raw_key = Path(args.key_file).expanduser()
    if raw_archive.is_symlink() or not raw_archive.is_file():
        raise RestoreError("archive must be a regular file")
    if raw_key.is_symlink() or not raw_key.is_file():
        raise RestoreError("key must be a regular file")
    archive = raw_archive.resolve()
    key = raw_key.resolve()
    if os.name != "nt":
        if (key.stat().st_mode & 0o777) != 0o600:
            raise RestoreError("migration key must have mode 0600")
        if hasattr(os, "getuid") and key.stat().st_uid != os.getuid():
            raise RestoreError("migration key has the wrong owner")
    key_material = read_key(key)
    openssl = shutil.which(args.openssl) if os.sep not in args.openssl else args.openssl
    if not openssl or not Path(openssl).is_file():
        raise RestoreError("OpenSSL executable is unavailable")
    return archive, key_material, str(openssl)


def verify_archive(args: argparse.Namespace) -> None:
    archive, key, openssl = _validated_inputs(args)
    with tempfile.TemporaryDirectory(prefix="harness-verify-") as temp_name:
        temp = Path(temp_name)
        plain = temp / "state.tar"
        staging = temp / "state"
        staging.mkdir(mode=0o700)
        decrypt(openssl, key, archive, plain)
        safe_extract(plain, staging)
        validate_state_inventory(staging)
        verify_state_checksums(staging)
        validate_database(staging)
        validate_and_relocate_context_state(staging, staging, relocate=False)


def restore_secrets(archive: Path, key: bytes, target: Path, openssl: str) -> None:
    if not archive.is_file() or archive.is_symlink():
        raise RestoreError("secrets archive must be a regular file")
    with tempfile.TemporaryDirectory(prefix="harness-secrets-", dir=target.parent) as temp_name:
        temp = Path(temp_name)
        plain = temp / "secrets.tar"
        decrypt(openssl, key, archive, plain)
        try:
            bundle = tarfile.open(plain, "r:*")
        except (OSError, tarfile.TarError) as exc:
            raise RestoreError("decrypted secrets archive is not a readable tar") from exc
        with bundle:
            seen: set[str] = set()
            members = []
            total_bytes = 0
            for member in bundle:
                members.append(member)
                if len(members) > len(SECRET_FILES):
                    raise RestoreError("secrets archive member limit exceeded")
                if member.size < 0 or member.size > 16 * 1024 * 1024:
                    raise RestoreError("secrets archive member size limit exceeded")
                total_bytes += member.size
                if total_bytes > 64 * 1024 * 1024:
                    raise RestoreError("secrets archive aggregate size limit exceeded")
            for member in members:
                pure = PurePosixPath(member.name)
                if (
                    pure.is_absolute() or ".." in pure.parts or len(pure.parts) != 2
                    or pure.parts[0] != "secrets" or pure.parts[1] not in SECRET_FILES
                    or not member.isfile() or member.issym() or member.islnk()
                    or pure.parts[1] in seen
                ):
                    raise RestoreError(f"unsafe secrets archive member: {member.name}")
                seen.add(pure.parts[1])
                destination = target / pure.parts[1]
                if destination.exists() or destination.is_symlink():
                    raise RestoreError(f"secret destination already exists: {pure.parts[1]}")
                source = bundle.extractfile(member)
                if source is None:
                    raise RestoreError(f"unreadable secrets archive member: {member.name}")
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output:
                    copied = 0
                    while copied < member.size:
                        block = source.read(min(1024 * 1024, member.size - copied))
                        if not block:
                            raise RestoreError(f"truncated secrets archive member: {member.name}")
                        output.write(block)
                        copied += len(block)
                    if source.read(1):
                        raise RestoreError(f"secrets archive member exceeded declared size: {member.name}")


def restore(args: argparse.Namespace) -> Path:
    archive, key, openssl = _validated_inputs(args)
    if not args.target:
        raise RestoreError("target is required for restore")
    raw_target = Path(args.target).expanduser()
    if raw_target.exists() or raw_target.is_symlink():
        raise RestoreError("target must be fresh and nonexistent")
    if not raw_target.parent.is_dir():
        raise RestoreError("target parent does not exist")
    target = raw_target.parent.resolve(strict=True) / raw_target.name
    if target.exists() or target.is_symlink():
        raise RestoreError("resolved target must be fresh and nonexistent")
    with tempfile.TemporaryDirectory(prefix="harness-restore-", dir=target.parent) as temp_name:
        temp = Path(temp_name)
        plain = temp / "state.tar"
        staging = temp / "state"
        staging.mkdir(mode=0o700)
        decrypt(openssl, key, archive, plain)
        safe_extract(plain, staging)
        validate_state_inventory(staging)
        verify_state_checksums(staging)
        validate_and_relocate_database(staging, target)
        validate_and_relocate_context_state(staging, target, relocate=True)
        (staging / "STATE_CHECKSUMS.sha256").unlink()
        secrets_archive = getattr(args, "secrets_archive", None)
        if secrets_archive:
            raw_secrets = Path(secrets_archive).expanduser()
            if raw_secrets.is_symlink() or not raw_secrets.is_file():
                raise RestoreError("secrets archive must be a regular file")
            restore_secrets(raw_secrets.resolve(), key, staging, openssl)
        staging.rename(target)
    os.chmod(target, 0o700)
    return target


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--archive", required=True)
    value.add_argument("--key-file", required=True)
    value.add_argument("--target")
    value.add_argument("--secrets-archive")
    value.add_argument("--openssl", default="openssl")
    value.add_argument("--verify-only", action="store_true")
    return value


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if args.verify_only:
            verify_archive(args)
            print("archive_verified=1")
            return 0
        target = restore(args)
    except RestoreError as exc:
        print(f"restore refused: {exc}", file=sys.stderr)
        return 2
    print(f"restored={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
