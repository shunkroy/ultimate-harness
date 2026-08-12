"""Explicit bounded compilation queue for the always-active service."""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from typing import Any, Dict, Optional

from ..config import HarnessConfig
from ..security import atomic_write_json, ensure_private_dir, read_private_json
from ..store import Store
from .compiler import ContextCompiler
from .package import ContextPackage


class ContextJobManager:
    def __init__(self, config: HarnessConfig, store: Store):
        self.config = config
        self.store = store
        self.jobs_dir = ensure_private_dir(config.context_jobs_dir)
        self.packages_dir = ensure_private_dir(config.context_root)

    def submit(self, source: str, *, name: Optional[str] = None, version: str = "0.1.0") -> str:
        source_path = os.path.abspath(os.path.expanduser(source))
        st = os.lstat(source_path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise ValueError("context source must be a regular non-symlink file")
        job_id = uuid.uuid4().hex
        value = {
            "schema": "harness.context-job/v1",
            "id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "source": source_path,
            "name": name,
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
        for name in names:
            if name.endswith(".json"):
                value = read_private_json(os.path.join(self.jobs_dir, name))
                if value:
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
            compiled = ContextCompiler().compile_file(
                job["source"], name=job.get("name"), version=str(job.get("version", "0.1.0")),
            )
            destination = os.path.join(self.packages_dir, compiled.ir.context_id)
            package = ContextPackage.write(compiled, destination)
        except Exception as exc:
            job["status"] = "failed"
            job["error_code"] = type(exc).__name__
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
