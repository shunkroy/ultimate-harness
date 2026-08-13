"""Crash-recoverable durable job queue with encrypted payloads."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any, Dict, Optional

from .config import HarnessConfig
from .crypto import atomic_write_envelope, decrypt, encrypt, load_or_create_key
from .models import RunRequest
from .orchestrator import Orchestrator
from .security import ensure_private_dir, redact, task_hash
from .store import Store
from . import secrets as secret_store
from .kernel.contracts import ExecutionPlan
from .kernel.event_bus import EventBus, TypedEvent
from .kernel.execution_state import ExecutionStateRepository
from .kernel.payloads import TaskPayload
from .kernel.task_types import default_task_types
from .kernel.tasks import StaleLeaseError, TaskRepository, TaskState
from .storage import LocalAuthenticatedStorage


TRANSIENT = {"timeout", "process_error", "spawn_error", "daemon_unavailable", "unavailable", "local_error", "circuit_open"}


class JobManager:
    def __init__(self, config: HarnessConfig, store: Store, orchestrator: Orchestrator):
        self.config, self.store, self.orchestrator = config, store, orchestrator
        self.jobs_dir = ensure_private_dir(os.path.join(config.state_root, "jobs"))
        self.key = b"" if config.platform.is_windows else load_or_create_key(os.path.join(config.state_root, "job.key"))
        self.events = EventBus(store)
        self.tasks = TaskRepository(store, self.events)
        self.objects = LocalAuthenticatedStorage(
            config.object_store_root, config.object_store_key,
            openssl_bin=config.openssl_bin,
        )
        self.execution_state = ExecutionStateRepository(
            store, self.events, self.tasks, self.objects, default_task_types(),
        )

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
        typed = TaskPayload(
            "legacy.run/v1", request.prompt,
            inputs={},
            constraints={
                "engine": request.engine, "agent": request.agent,
                "model": request.model, "provider": request.provider,
                "timeout": request.timeout, "cwd": request.cwd,
                "sensitive": request.sensitive, "untrusted": request.untrusted,
                "no_fallback": request.no_fallback,
                "retries": request.retries,
                "required_capabilities": [],
            },
            budget={"max_attempts": max(1, max_attempts)},
        )
        descriptor = self.execution_state.task_types.require(typed.task_type)
        descriptor.validate(typed)
        task_id = f"legacy-job-{job_id}"
        binding = {"task_id": task_id, "role": "input", "task_type": typed.task_type}
        reference = self.objects.put(
            typed.canonical, schema_id=typed.schema_id, purpose="task.input", binding=binding,
        )
        from .kernel.contracts import ExecutionRequest
        plain = typed.as_dict()
        task_request = ExecutionRequest(
            task_id, typed.objective, descriptor.required_capabilities,
            typed.constraints.get("preferred_runtime"), plain["inputs"],
            plain["constraints"], plain["budget"],
        )
        try:
            with self.store.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job_id, now, now, "queued", task_hash(request.prompt), payload_path,
                        request.engine, request.agent, request.model, request.provider, request.timeout,
                        request.cwd, int(request.sensitive), int(request.untrusted), int(request.no_fallback),
                        request.retries, 0, max(1, max_attempts), now, None, None, None, None, None,
                    ),
                )
                task = self.execution_state._create_task(
                    con, typed, descriptor, task_request, reference,
                    idempotency_key=f"legacy-job:{job_id}", source="legacy.jobs",
                    reason="legacy job compatibility submission", authority="authenticated_user",
                    priority=0, max_attempts=max(1, max_attempts), now=now,
                )
                con.execute(
                    "INSERT INTO kernel_legacy_job_tasks("
                    "job_id,task_id,payload_reference_id,latest_attempt_id,latest_fence_token,"
                    "projection_revision,created_at) VALUES(?,?,?,?,?,?,?)",
                    (job_id, task.task_id, reference.reference_id, None, None, 0, now),
                )
        except Exception:
            try:
                os.unlink(payload_path)
            except FileNotFoundError:
                pass
            raise
        self.store.append_audit("job.queued", job_id, {"task_hash": task_hash(request.prompt), "engine": request.engine})
        return job_id

    def _recover_stale(self) -> int:
        now = time.time()
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT j.id,m.task_id FROM jobs j JOIN kernel_legacy_job_tasks m ON m.job_id=j.id "
                "WHERE j.status='running' AND j.lease_until IS NOT NULL AND j.lease_until<?",
                (now,),
            ).fetchall()
            recovered = 0
            for row in rows:
                task = self.tasks._get(con, str(row["task_id"]))
                status = "dead"
                if task and task.state == TaskState.RUNNING and task.active_attempt_id:
                    attempt = con.execute(
                        "SELECT * FROM kernel_task_attempts WHERE attempt_id=?",
                        (task.active_attempt_id,),
                    ).fetchone()
                    if attempt and float(attempt["lease_until"]) < now:
                        # Legacy provider runs may have produced external effects.
                        # Never auto-reclaim an executor that cannot be proven dead;
                        # require an explicit operator retry instead.
                        target = TaskState.FAILED
                        con.execute(
                            "UPDATE kernel_task_attempts SET state='expired',finished_at=?,error_code=? "
                            "WHERE attempt_id=? AND state='running'",
                            (now, "lease_expired", task.active_attempt_id),
                        )
                        revision = task.revision + 1
                        con.execute(
                            "UPDATE kernel_tasks SET state=?,active_attempt_id=NULL,error_code=?,"
                            "next_run_at=?,updated_at=?,terminal_at=?,revision=? WHERE task_id=? AND revision=?",
                            (
                                target.value, "lease_expired", now, now,
                                now if target == TaskState.FAILED else None,
                                revision, task.task_id, task.revision,
                            ),
                        )
                        event = self.events.append(TypedEvent(
                            event_type="task.lease.expired", source="kernel.tasks",
                            task_id=task.task_id, correlation_id=task.task_id,
                            dedup_key=f"{task.active_attempt_id}:expired",
                            payload={
                                "attempt_id": task.active_attempt_id,
                                "fence_token": task.current_fence,
                                "recovery_state": target.value,
                            },
                        ), connection=con)
                        self.tasks._record_transition(
                            con, task.task_id, task.active_attempt_id, TaskState.RUNNING,
                            target, "lease.expired", event.event_id, now, revision,
                        )
                    task = self.tasks._get(con, task.task_id)
                if task and task.state == TaskState.COMPLETED:
                    status = "succeeded"
                elif task and task.state == TaskState.CANCELLED:
                    status = "cancelled"
                con.execute(
                    "UPDATE jobs SET status=?,worker_pid=NULL,lease_until=NULL,updated_at=?,"
                    "error_code=? WHERE id=?",
                    (status, now, None if status == "queued" else "lease_expired", row["id"]),
                )
                recovered += 1
            cur = con.execute(
                "UPDATE jobs SET status='dead',worker_pid=NULL,lease_until=NULL,updated_at=?,"
                "error_code='legacy_payload_not_migrated',error_detail='resubmit with the current Harness version' "
                "WHERE status='running' AND lease_until IS NOT NULL AND lease_until<? "
                "AND NOT EXISTS(SELECT 1 FROM kernel_legacy_job_tasks m WHERE m.job_id=jobs.id)",
                (now, now),
            )
            return recovered + cur.rowcount

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
            mapping = con.execute(
                "SELECT task_id FROM kernel_legacy_job_tasks WHERE job_id=?", (row["id"],),
            ).fetchone()
            if not mapping:
                con.execute(
                    "UPDATE jobs SET status='dead',updated_at=?,error_code=?,error_detail=? "
                    "WHERE id=? AND status IN ('queued','retry')",
                    (
                        now, "legacy_payload_not_migrated",
                        "resubmit with the current Harness version", row["id"],
                    ),
                )
                self.events.append(TypedEvent(
                    event_type="legacy.job.quarantined", source="legacy.jobs",
                    correlation_id=str(row["id"]),
                    dedup_key=f"{row['id']}:unmigrated",
                    payload={
                        "job_id": str(row["id"]),
                        "reason_code": "legacy_payload_not_migrated",
                    },
                ), connection=con)
                return {
                    **dict(row), "status": "dead",
                    "error_code": "legacy_payload_not_migrated", "quarantined": True,
                }
            task = self.tasks._get(con, str(mapping["task_id"]))
            if task is None:
                raise RuntimeError("legacy job typed task mapping is missing")
            task = self.tasks.prepare(task.task_id, now=now, connection=con)
            runtime_id = str(row["engine"] if row["engine"] != "auto" else "opencode")
            plan = ExecutionPlan(
                uuid.uuid4().hex, task.task_id, runtime_id, (),
                ("legacy.run",), ("fenced.completion",),
                "legacy job compatibility execution",
            )
            typed_lease = self.tasks.claim(
                task.task_id, plan, owner_id=f"legacy-worker:{os.getpid()}",
                lease_seconds=max(lease_seconds, int(row["timeout"]) + 120),
                now=now, connection=con,
            )
            attempt = int(row["attempt"]) + 1
            cur = con.execute(
                "UPDATE jobs SET status='running',attempt=?,worker_pid=?,lease_until=?,updated_at=? "
                "WHERE id=? AND status IN ('queued','retry')",
                (attempt, os.getpid(), typed_lease.lease_until, now, row["id"]),
            )
            if cur.rowcount != 1:
                raise StaleLeaseError("legacy projection changed during typed claim")
            con.execute(
                "UPDATE kernel_legacy_job_tasks SET latest_attempt_id=?,latest_fence_token=?,"
                "projection_revision=projection_revision+1 WHERE job_id=?",
                (typed_lease.attempt_id, typed_lease.fence_token, row["id"]),
            )
            value = dict(row)
            value["attempt"] = attempt
            value["lease_until"] = typed_lease.lease_until
            value["typed_lease"] = typed_lease
            value["typed_task_id"] = task.task_id
            return value

    def _load_request(self, job: Dict[str, Any]) -> RunRequest:
        if not job.get("typed_task_id"):
            raise RuntimeError("unmapped legacy jobs are quarantined")
        payload = self.execution_state.load_task_payload(str(job["typed_task_id"]))
        values = payload.constraints
        return RunRequest(
            prompt=payload.objective, engine=str(values.get("engine", "auto")),
            agent=values.get("agent"), model=values.get("model"),
            provider=values.get("provider"), timeout=int(values.get("timeout", 240)),
            cwd=values.get("cwd"), sensitive=bool(values.get("sensitive", False)),
            untrusted=bool(values.get("untrusted", False)),
            no_fallback=bool(values.get("no_fallback", False)),
            retries=int(values.get("retries", 1)),
        )

    def work_once(self) -> Optional[Dict[str, Any]]:
        job = self.claim()
        if not job:
            return None
        if job.get("quarantined"):
            self.store.append_audit(
                "job.dead", str(job["id"]),
                {"error_code": str(job["error_code"]), "run_id": ""},
            )
            return {
                "id": str(job["id"]), "status": "dead", "run_id": None,
                "error_code": str(job["error_code"]),
            }
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
        typed_lease = job.get("typed_lease")
        if typed_lease:
            try:
                with self.store.connect() as con:
                    con.execute("BEGIN IMMEDIATE")
                    mapping = con.execute(
                        "SELECT latest_attempt_id,latest_fence_token FROM kernel_legacy_job_tasks WHERE job_id=?",
                        (job_id,),
                    ).fetchone()
                    if (
                        not mapping or mapping[0] != typed_lease.attempt_id
                        or int(mapping[1]) != typed_lease.fence_token
                    ):
                        raise StaleLeaseError("legacy projection fence is no longer authoritative")
                    task = self.tasks.complete(
                        typed_lease, success=success,
                        outcome_hash=hashlib.sha256(
                            f"legacy:{run_id or ''}:{error_code or ''}:{success}".encode()
                        ).hexdigest(),
                        error_code=error_code, retryable=retryable,
                        retry_at=next_run, now=now, connection=con,
                    )
                    status = {
                        TaskState.COMPLETED: "succeeded",
                        TaskState.RETRYING: "retry",
                        TaskState.FAILED: "dead",
                    }[task.state]
                    cur = con.execute(
                        "UPDATE jobs SET status=?,updated_at=?,lease_until=NULL,worker_pid=NULL,result_run_id=?,"
                        "error_code=?,error_detail=?,next_run_at=? WHERE id=? AND status='running'",
                        (status, now, run_id, error_code, redact(error or "", 200), next_run, job_id),
                    )
                    if cur.rowcount != 1:
                        raise StaleLeaseError("legacy job projection changed before completion")
            except StaleLeaseError:
                self.store.append_audit(
                    "legacy.job.stale_completion_rejected", job_id,
                    {"attempt_id": typed_lease.attempt_id, "fence_token": typed_lease.fence_token},
                )
                return {"id": job_id, "status": "stale_rejected", "run_id": run_id, "error_code": "stale_lease"}
        else:
            raise RuntimeError("unfenced legacy completion is disabled")
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
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload_path,status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] not in {"queued", "retry"}:
                return False
            mapping = con.execute(
                "SELECT task_id FROM kernel_legacy_job_tasks WHERE job_id=?", (job_id,),
            ).fetchone()
            if mapping:
                task = self.tasks._get(con, str(mapping[0]))
                if not task or task.state in {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}:
                    return False
                if task.active_attempt_id:
                    return False
                revision = task.revision + 1
                now = time.time()
                con.execute(
                    "UPDATE kernel_tasks SET state='cancelled',active_attempt_id=NULL,error_code=?,"
                    "updated_at=?,terminal_at=?,revision=? WHERE task_id=? AND revision=?",
                    ("task_cancelled", now, now, revision, task.task_id, task.revision),
                )
                event = self.events.append(TypedEvent(
                    event_type="task.cancelled", source="kernel.tasks", task_id=task.task_id,
                    correlation_id=task.task_id, dedup_key=f"{task.task_id}:revision:{revision}",
                    payload={
                        "from": task.state.value, "to": "cancelled",
                        "reason_code": "user.cancelled",
                    },
                ), connection=con)
                self.tasks._record_transition(
                    con, task.task_id, None, task.state, TaskState.CANCELLED,
                    "user.cancelled", event.event_id, now, revision,
                )
            con.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=?", (time.time(), job_id))
        try:
            os.unlink(row["payload_path"])
        except FileNotFoundError:
            pass
        self.store.append_audit("job.cancelled", job_id, {})
        return True

    def retry(self, job_id: str) -> bool:
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            mapping = con.execute(
                "SELECT task_id FROM kernel_legacy_job_tasks WHERE job_id=?", (job_id,),
            ).fetchone()
            if mapping:
                task = self.tasks._get(con, str(mapping["task_id"]))
                if not task or task.state != TaskState.FAILED or task.attempts_started >= task.max_attempts:
                    return False
                revision = task.revision + 1
                now = time.time()
                con.execute(
                    "UPDATE kernel_tasks SET state='retrying',next_run_at=?,error_code=NULL,"
                    "updated_at=?,terminal_at=NULL,revision=? WHERE task_id=? AND revision=?",
                    (now, now, revision, task.task_id, task.revision),
                )
                event = self.events.append(TypedEvent(
                    event_type="task.retrying", source="kernel.tasks", task_id=task.task_id,
                    correlation_id=task.task_id, dedup_key=f"{task.task_id}:revision:{revision}",
                    payload={"from": "failed", "to": "retrying", "reason_code": "user.retry"},
                ), connection=con)
                self.tasks._record_transition(
                    con, task.task_id, None, task.state, TaskState.RETRYING,
                    "user.retry", event.event_id, now, revision,
                )
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
            con.execute("DELETE FROM kernel_legacy_job_tasks WHERE job_id=?", (job_id,))
            con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        try:
            os.unlink(row["payload_path"])
        except FileNotFoundError:
            pass
        return True
