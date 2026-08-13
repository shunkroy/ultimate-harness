"""Explicit bounded compilation queue for the always-active service."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import HarnessConfig
from ..security import atomic_write_json, ensure_private_dir, read_private_json
from ..store import Store
from ..kernel.execution_state import ExecutionStateRepository
from .compiler import ContextCompiler
from .package import ContextPackage


class ContextJobManager:
    def __init__(
        self, config: HarnessConfig, store: Store,
        execution_state: ExecutionStateRepository | None = None,
    ):
        self.config = config
        self.store = store
        self.jobs_dir = ensure_private_dir(config.context_jobs_dir)
        self.packages_dir = ensure_private_dir(config.context_root)
        if execution_state is None:
            from ..kernel.event_bus import EventBus
            from ..kernel.task_types import default_task_types
            from ..kernel.tasks import TaskRepository
            from ..storage import LocalAuthenticatedStorage
            events = EventBus(store)
            execution_state = ExecutionStateRepository(
                store, events, TaskRepository(store, events),
                LocalAuthenticatedStorage(
                    config.object_store_root, config.object_store_key,
                    openssl_bin=config.openssl_bin,
                ),
                default_task_types(),
            )
        self.execution_state = execution_state

    def submit(self, source: str, *, name: Optional[str] = None, version: str = "0.1.0") -> str:
        source_path = os.path.abspath(os.path.expanduser(source))
        job_id = uuid.uuid4().hex
        resolved_name = name or Path(source_path).stem
        snapshot = self.execution_state.capture_source(
            source_path, source_type="context-job.file", source_revision=job_id,
            metadata={"source_name": os.path.basename(source_path)},
        )
        value = {
            "schema": "harness.context-job/v2",
            "id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "snapshot_id": snapshot.snapshot_id,
            "name": resolved_name,
            "version": version,
            "attempt": 0,
            "result": None,
            "error_code": None,
        }
        atomic_write_json(self._path(job_id), value)
        self.store.append_audit("context.job.queued", job_id, {"source_name": os.path.basename(source_path)})
        return job_id

    def show(self, job_id: str) -> Optional[Dict[str, Any]]:
        value = read_private_json(self._path(job_id))
        return value or None

    def list(self) -> list[Dict[str, Any]]:
        values: list[Dict[str, Any]] = []
        try:
            names = sorted(os.listdir(self.jobs_dir))
        except OSError:
            return values
        for filename in names:
            if filename.endswith(".json"):
                value = read_private_json(os.path.join(self.jobs_dir, filename))
                if value and filename == str(value.get("id", "")) + ".json":
                    values.append(value)
        values.sort(key=lambda item: (float(item.get("created_at", 0)), str(item.get("id", ""))))
        return values

    def work_once(self) -> Optional[Dict[str, Any]]:
        job = next((item for item in self.list() if item.get("status") == "queued"), None)
        if not job:
            return None
        job["status"] = "running"
        job["attempt"] = int(job.get("attempt", 0)) + 1
        job["updated_at"] = time.time()
        atomic_write_json(self._path(job["id"]), job)
        try:
            if job.get("schema") != "harness.context-job/v2":
                raise ValueError("legacy_source_snapshot_missing")
            snapshot = self.execution_state.source_snapshot(str(job.get("snapshot_id", "")))
            if snapshot.source_type != "context-job.file" or snapshot.source_revision != job["id"]:
                raise ValueError("context_snapshot_binding_mismatch")
            source_name = str(snapshot.metadata.get("source_name", "source.txt"))
            raw = self.execution_state.load_source(snapshot)
            compiled = ContextCompiler().compile_bytes(
                raw, name=str(job.get("name") or "context"),
                version=str(job.get("version", "0.1.0")), source_name=source_name,
            )
            if compiled.ir.source.sha256 != snapshot.content_sha256:
                raise ValueError("context_snapshot_hash_mismatch")
            destination = os.path.join(self.packages_dir, compiled.ir.context_id)
            package = ContextPackage.write(compiled, destination)
        except Exception as exc:
            job["status"] = "failed"
            job["error_code"] = (
                str(exc) if str(exc) in {
                    "legacy_source_snapshot_missing", "context_snapshot_binding_mismatch",
                    "context_snapshot_hash_mismatch",
                } else type(exc).__name__
            )
            job["result"] = None
        else:
            job["status"] = "succeeded"
            job["error_code"] = None
            job["result"] = {
                "context_id": package.ir.context_id,
                "version": package.ir.version,
                "package": package.root,
            }
        job["updated_at"] = time.time()
        atomic_write_json(self._path(job["id"]), job)
        self.store.append_audit(
            f"context.job.{job['status']}", job["id"],
            {"context_id": (job.get("result") or {}).get("context_id", ""), "error_code": job.get("error_code") or ""},
        )
        return job

    def _path(self, job_id: str) -> str:
        if not isinstance(job_id, str) or len(job_id) != 32 or any(ch not in "0123456789abcdef" for ch in job_id):
            raise ValueError("invalid context job id")
        return os.path.join(self.jobs_dir, job_id + ".json")
