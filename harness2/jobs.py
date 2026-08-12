"""Crash-recoverable durable job queue with encrypted payloads."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict
from typing import Any, Dict, Optional

from .config import HarnessConfig
from .crypto import atomic_write_envelope, decrypt, encrypt, load_or_create_key
from .models import RunRequest
from .orchestrator import Orchestrator
from .security import ensure_private_dir, redact, task_hash
from .store import Store
from . import secrets as secret_store


TRANSIENT = {"timeout", "process_error", "spawn_error", "daemon_unavailable", "unavailable", "local_error", "circuit_open"}


class JobManager:
    def __init__(self, config: HarnessConfig, store: Store, orchestrator: Orchestrator):
        self.config, self.store, self.orchestrator = config, store, orchestrator
        self.jobs_dir = ensure_private_dir(os.path.join(config.state_root, "jobs"))
        self.key = b"" if config.platform.is_windows else load_or_create_key(os.path.join(config.state_root, "job.key"))

    def submit(self, request: RunRequest, max_attempts: int = 3) -> str:
        job_id = uuid.uuid4().hex
        payload_path = os.path.join(self.jobs_dir, job_id + ".bin")
        payload = json.dumps({"prompt": request.prompt}, ensure_ascii=False).encode("utf-8")
        if self.config.platform.is_windows:
            envelope = secret_store.protect_bytes(payload)
        elif not self.config.openssl_bin:
            raise RuntimeError("encrypted durable jobs require OpenSSL; configure HARNESS_OPENSSL_BIN")
        else:
            envelope = encrypt(self.key, payload, self.config.openssl_bin)
        atomic_write_envelope(payload_path, envelope)
        now = time.time()
        with self.store.connect() as con:
            con.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, now, now, "queued", task_hash(request.prompt), payload_path,
                    request.engine, request.agent, request.model, request.provider, request.timeout,
                    request.cwd, int(request.sensitive), int(request.untrusted), int(request.no_fallback),
                    request.retries, 0, max(1, max_attempts), now, None, None, None, None, None,
                ),
            )
        self.store.append_audit("job.queued", job_id, {"task_hash": task_hash(request.prompt), "engine": request.engine})
        return job_id

    def _recover_stale(self) -> int:
        now = time.time()
        with self.store.connect() as con:
            cur = con.execute(
                "UPDATE jobs SET status='queued',worker_pid=NULL,lease_until=NULL,updated_at=? "
                "WHERE status='running' AND lease_until IS NOT NULL AND lease_until<?",
                (now, now),
            )
            return cur.rowcount

    def claim(self, lease_seconds: int = 600) -> Optional[Dict[str, Any]]:
        self._recover_stale()
        now = time.time()
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','retry') AND next_run_at<=? "
                "ORDER BY created_at LIMIT 1", (now,),
            ).fetchone()
            if not row:
                return None
            attempt = int(row["attempt"]) + 1
            lease = now + max(lease_seconds, int(row["timeout"]) + 120)
            con.execute(
                "UPDATE jobs SET status='running',attempt=?,worker_pid=?,lease_until=?,updated_at=? WHERE id=?",
                (attempt, os.getpid(), lease, now, row["id"]),
            )
        value = dict(row)
        value["attempt"] = attempt
        value["lease_until"] = lease
        return value

    def _load_request(self, job: Dict[str, Any]) -> RunRequest:
        with open(job["payload_path"], "rb") as fh:
            envelope = fh.read()
        raw = (
            secret_store.unprotect_bytes(envelope)
            if self.config.platform.is_windows
            else decrypt(self.key, envelope, self.config.openssl_bin)
        )
        prompt = json.loads(raw.decode("utf-8"))["prompt"]
        return RunRequest(
            prompt=prompt, engine=job["engine"], agent=job["agent"], model=job["model"],
            provider=job["provider"], timeout=int(job["timeout"]), cwd=job["cwd"],
            sensitive=bool(job["sensitive"]), untrusted=bool(job["untrusted"]),
            no_fallback=bool(job["no_fallback"]), retries=int(job["retries"]),
        )

    def work_once(self) -> Optional[Dict[str, Any]]:
        job = self.claim()
        if not job:
            return None
        job_id = job["id"]
        try:
            request = self._load_request(job)
            _, result, run_id = self.orchestrator.run(request)
        except Exception as exc:
            result, run_id = None, None
            error_code, error = "worker_exception", redact(exc)
        else:
            error_code, error = result.error_code, result.error
        now = time.time()
        success = bool(result and result.success)
        retryable = not success and error_code in TRANSIENT and int(job["attempt"]) < int(job["max_attempts"])
        status = "succeeded" if success else ("retry" if retryable else "dead")
        next_run = now + min(300, 2 ** int(job["attempt"])) if retryable else now
        with self.store.connect() as con:
            con.execute(
                "UPDATE jobs SET status=?,updated_at=?,lease_until=NULL,worker_pid=NULL,result_run_id=?,"
                "error_code=?,error_detail=?,next_run_at=? WHERE id=?",
                (status, now, run_id, error_code, redact(error or "", 200), next_run, job_id),
            )
        if success:
            try:
                os.unlink(job["payload_path"])
            except FileNotFoundError:
                pass
        self.store.append_audit(f"job.{status}", job_id, {"error_code": error_code or "", "run_id": run_id or ""})
        return {"id": job_id, "status": status, "run_id": run_id, "error_code": error_code}

    def list(self, limit: int = 20) -> list[Dict[str, Any]]:
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT id,created_at,updated_at,status,task_hash,engine,attempt,max_attempts,next_run_at,"
                "result_run_id,error_code FROM jobs ORDER BY created_at DESC LIMIT ?", (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def show(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT id,created_at,updated_at,status,task_hash,engine,agent,model,provider,timeout,cwd,"
                "sensitive,untrusted,attempt,max_attempts,next_run_at,result_run_id,error_code,error_detail "
                "FROM jobs WHERE id=?", (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def cancel(self, job_id: str) -> bool:
        with self.store.connect() as con:
            row = con.execute("SELECT payload_path,status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] == "running":
                return False
            con.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=?", (time.time(), job_id))
        try:
            os.unlink(row["payload_path"])
        except FileNotFoundError:
            pass
        self.store.append_audit("job.cancelled", job_id, {})
        return True

    def retry(self, job_id: str) -> bool:
        with self.store.connect() as con:
            cur = con.execute(
                "UPDATE jobs SET status='queued',next_run_at=?,error_code=NULL,error_detail=NULL,updated_at=? "
                "WHERE id=? AND status='dead'",
                (time.time(), time.time(), job_id),
            )
            return cur.rowcount == 1

    def purge(self, job_id: str) -> bool:
        with self.store.connect() as con:
            row = con.execute("SELECT payload_path,status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] == "running":
                return False
            con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        try:
            os.unlink(row["payload_path"])
        except FileNotFoundError:
            pass
        return True
