"""Persistent typed task state machine with fenced execution attempts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from .contracts import ExecutionPlan, ExecutionRequest
from .event_bus import EventBus, TypedEvent, canonical_json


class TaskError(RuntimeError):
    pass


class InvalidTransition(TaskError):
    pass


class StaleLeaseError(TaskError):
    pass


class TaskIdempotencyConflict(TaskError):
    pass


class TaskState(str, Enum):
    CREATED = "created"
    PLANNED = "planned"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    RETRYING = "retrying"
    DEGRADED = "degraded"
    RECOVERING = "recovering"


TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})
ALLOWED_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.CREATED: frozenset({TaskState.PLANNED, TaskState.CANCELLED, TaskState.BLOCKED}),
    TaskState.PLANNED: frozenset({TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED}),
    TaskState.READY: frozenset({TaskState.RUNNING, TaskState.CANCELLED, TaskState.BLOCKED}),
    TaskState.RUNNING: frozenset({
        TaskState.WAITING, TaskState.DEGRADED, TaskState.CANCELLED,
    }),
    TaskState.WAITING: frozenset({TaskState.READY, TaskState.RUNNING, TaskState.CANCELLED, TaskState.BLOCKED}),
    TaskState.VERIFYING: frozenset({TaskState.DEGRADED}),
    TaskState.RETRYING: frozenset({TaskState.READY, TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.DEGRADED: frozenset({TaskState.RECOVERING, TaskState.RETRYING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.RECOVERING: frozenset({TaskState.READY, TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.BLOCKED: frozenset({TaskState.READY, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset({TaskState.RETRYING}),
    TaskState.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    task_type: str
    state: TaskState
    source: str
    reason: str
    priority: int
    authority: str
    objective_hash: str
    request_hash: str
    idempotency_key: Optional[str]
    required_capabilities: tuple[str, ...]
    constraints: Mapping[str, Any]
    budget: Mapping[str, Any]
    preferred_runtime: Optional[str]
    max_attempts: int
    attempts_started: int
    current_fence: int
    active_attempt_id: Optional[str]
    next_run_at: float
    result_hash: Optional[str]
    error_code: Optional[str]
    created_at: float
    updated_at: float
    terminal_at: Optional[float]
    revision: int
    schema_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        value = dict(self.__dict__)
        value["state"] = self.state.value
        value["required_capabilities"] = list(self.required_capabilities)
        value["constraints"] = dict(self.constraints)
        value["budget"] = dict(self.budget)
        return value


@dataclass(frozen=True)
class AttemptLease:
    attempt_id: str
    task_id: str
    attempt_no: int
    fence_token: int
    owner_id: str
    lease_until: float
    runtime_id: str
    plan: ExecutionPlan

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "attempt_no": self.attempt_no,
            "fence_token": self.fence_token,
            "owner_id": self.owner_id,
            "lease_until": self.lease_until,
            "runtime_id": self.runtime_id,
            "plan_id": self.plan.plan_id,
        }


def execution_request_hash(request: ExecutionRequest) -> str:
    payload = canonical_json({
        "task_id": request.task_id,
        "objective_hash": hashlib.sha256(request.objective.encode("utf-8", "surrogatepass")).hexdigest(),
        "required_capabilities": list(request.required_capabilities),
        "preferred_runtime": request.preferred_runtime,
        "inputs": dict(request.inputs),
        "constraints": dict(request.constraints),
        "budget": dict(request.budget),
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _plan_dict(plan: ExecutionPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "task_id": plan.task_id,
        "runtime_id": plan.runtime_id,
        "capabilities": list(plan.capabilities),
        "steps": list(plan.steps),
        "verification": list(plan.verification),
        "reason": plan.reason,
    }


class TaskRepository:
    def __init__(self, store, events: EventBus):
        self.store = store
        self.events = events

    @staticmethod
    def _from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=str(row["task_id"]), schema_version=int(row["schema_version"]),
            task_type=str(row["task_type"]), state=TaskState(row["state"]),
            source=str(row["source"]), reason=str(row["reason"]),
            priority=int(row["priority"]), authority=str(row["authority"]),
            objective_hash=str(row["objective_hash"]), idempotency_key=row["idempotency_key"],
            request_hash=str(row["request_hash"]),
            required_capabilities=tuple(json.loads(row["required_capabilities_json"])),
            constraints=json.loads(row["constraints_json"]), budget=json.loads(row["budget_json"]),
            preferred_runtime=row["preferred_runtime"], max_attempts=int(row["max_attempts"]),
            attempts_started=int(row["attempts_started"]), current_fence=int(row["current_fence"]),
            active_attempt_id=row["active_attempt_id"], next_run_at=float(row["next_run_at"]),
            result_hash=row["result_hash"], error_code=row["error_code"],
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
            terminal_at=row["terminal_at"], revision=int(row["revision"]),
        )

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self.store.connect() as con:
            row = con.execute("SELECT * FROM kernel_tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._from_row(row) if row else None

    def list(self, *, state: TaskState | None = None, limit: int = 100) -> tuple[TaskRecord, ...]:
        query = "SELECT * FROM kernel_tasks"
        values: list[Any] = []
        if state is not None:
            query += " WHERE state=?"
            values.append(state.value)
        query += " ORDER BY priority DESC,created_at,task_id LIMIT ?"
        values.append(max(1, min(int(limit), 1000)))
        with self.store.connect() as con:
            rows = con.execute(query, values).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def submit(
        self, request: ExecutionRequest, *, task_type: str = "execution",
        source: str = "user", reason: str = "explicit request", priority: int = 0,
        authority: str = "user", idempotency_key: str | None = None,
        max_attempts: int = 1, now: float | None = None,
    ) -> TaskRecord:
        timestamp = time.time() if now is None else float(now)
        request_hash = execution_request_hash(request)
        submission_hash = hashlib.sha256(canonical_json({
            "request_hash": request_hash,
            "task_type": task_type,
            "source": source,
            "reason": reason,
            "priority": int(priority),
            "authority": authority,
            "max_attempts": max(1, int(max_attempts)),
        }).encode("utf-8")).hexdigest()
        objective_hash = hashlib.sha256(request.objective.encode("utf-8", "surrogatepass")).hexdigest()
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing_id = con.execute(
                "SELECT * FROM kernel_tasks WHERE task_id=?", (request.task_id,),
            ).fetchone()
            if existing_id:
                value = self._from_row(existing_id)
                if value.request_hash == submission_hash and value.idempotency_key == idempotency_key:
                    return value
                raise TaskIdempotencyConflict("task identity already has a different request")
            if idempotency_key:
                existing = con.execute(
                    "SELECT * FROM kernel_tasks WHERE idempotency_key=?", (idempotency_key,),
                ).fetchone()
                if existing:
                    value = self._from_row(existing)
                    if value.request_hash != submission_hash or value.task_id != request.task_id:
                        raise TaskIdempotencyConflict("idempotency key has a different request")
                    return value
            con.execute(
                "INSERT INTO kernel_tasks("
                "task_id,schema_version,task_type,state,source,reason,priority,authority,"
                "objective_hash,request_hash,idempotency_key,required_capabilities_json,constraints_json,"
                "budget_json,preferred_runtime,max_attempts,attempts_started,current_fence,"
                "active_attempt_id,next_run_at,result_hash,error_code,created_at,updated_at,"
                "terminal_at,revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request.task_id, 1, task_type, TaskState.CREATED.value, source, reason,
                    int(priority), authority, objective_hash, submission_hash, idempotency_key,
                    canonical_json({"values": list(request.required_capabilities)}),
                    canonical_json(request.constraints), canonical_json(request.budget),
                    request.preferred_runtime, max(1, int(max_attempts)), 0, 0, None,
                    timestamp, None, None, timestamp, timestamp, None, 0,
                ),
            )
            # Store capability arrays as arrays, while using canonical validation above.
            con.execute(
                "UPDATE kernel_tasks SET required_capabilities_json=? WHERE task_id=?",
                (json.dumps(list(request.required_capabilities), separators=(",", ":")), request.task_id),
            )
            event = self.events.append(TypedEvent(
                event_type="task.created", source="kernel.tasks", task_id=request.task_id,
                correlation_id=request.task_id, dedup_key=f"{request.task_id}:created",
                payload={
                    "task_type": task_type, "state": TaskState.CREATED.value,
                    "objective_hash": objective_hash, "request_hash": submission_hash,
                    "priority": int(priority), "authority": authority,
                },
            ), connection=con)
            self._record_transition(
                con, request.task_id, None, None, TaskState.CREATED,
                "submitted", event.event_id, timestamp, 0,
            )
            row = con.execute("SELECT * FROM kernel_tasks WHERE task_id=?", (request.task_id,)).fetchone()
            return self._from_row(row)

    @staticmethod
    def _record_transition(
        con: sqlite3.Connection, task_id: str, attempt_id: str | None,
        from_state: TaskState | None, to_state: TaskState, reason_code: str,
        event_id: str, occurred_at: float, revision: int,
    ) -> None:
        con.execute(
            "INSERT INTO kernel_task_transitions(transition_id,task_id,attempt_id,from_state,"
            "to_state,reason_code,event_id,occurred_at,revision) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex, task_id, attempt_id,
                from_state.value if from_state else None, to_state.value,
                reason_code, event_id, occurred_at, revision,
            ),
        )

    def transition(
        self, task_id: str, to_state: TaskState, *, reason_code: str,
        causation_id: str | None = None, now: float | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> TaskRecord:
        timestamp = time.time() if now is None else float(now)

        def apply(con: sqlite3.Connection) -> TaskRecord:
            row = con.execute("SELECT * FROM kernel_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise TaskError(f"unknown task: {task_id}")
            current = self._from_row(row)
            if current.active_attempt_id:
                raise InvalidTransition(
                    "active attempts require fenced completion, cancellation, or recovery"
                )
            if to_state not in ALLOWED_TRANSITIONS[current.state]:
                raise InvalidTransition(f"{current.state.value} -> {to_state.value} is not allowed")
            revision = current.revision + 1
            terminal = timestamp if to_state in TERMINAL_STATES else None
            active_attempt = current.active_attempt_id if to_state == TaskState.RUNNING else None
            con.execute(
                "UPDATE kernel_tasks SET state=?,active_attempt_id=?,updated_at=?,terminal_at=?,"
                "revision=? WHERE task_id=? AND revision=?",
                (to_state.value, active_attempt, timestamp, terminal, revision, task_id, current.revision),
            )
            event = self.events.append(TypedEvent(
                event_type=f"task.{to_state.value}", source="kernel.tasks", task_id=task_id,
                correlation_id=task_id, causation_id=causation_id,
                dedup_key=f"{task_id}:revision:{revision}",
                payload={"from": current.state.value, "to": to_state.value, "reason_code": reason_code},
            ), connection=con)
            self._record_transition(
                con, task_id, current.active_attempt_id, current.state, to_state,
                reason_code, event.event_id, timestamp, revision,
            )
            row = con.execute("SELECT * FROM kernel_tasks WHERE task_id=?", (task_id,)).fetchone()
            return self._from_row(row)

        if connection is not None:
            return apply(connection)
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            return apply(con)

    def prepare(self, task_id: str, *, now: float | None = None) -> TaskRecord:
        task = self.get(task_id)
        if not task:
            raise TaskError(f"unknown task: {task_id}")
        if task.state == TaskState.CREATED:
            task = self.transition(task_id, TaskState.PLANNED, reason_code="plan.created", now=now)
        if task.state == TaskState.PLANNED:
            task = self.transition(task_id, TaskState.READY, reason_code="plan.ready", now=now)
        if task.state == TaskState.BLOCKED:
            task = self.transition(task_id, TaskState.READY, reason_code="plan.recheck", now=now)
        return task

    def cancel(self, task_id: str, *, now: float | None = None) -> TaskRecord:
        timestamp = time.time() if now is None else float(now)
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM kernel_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise TaskError(f"unknown task: {task_id}")
            task = self._from_row(row)
            if task.state in TERMINAL_STATES:
                raise InvalidTransition(f"cannot cancel terminal task: {task.state.value}")
            if TaskState.CANCELLED not in ALLOWED_TRANSITIONS[task.state]:
                raise InvalidTransition(f"{task.state.value} -> cancelled is not allowed")
            if task.active_attempt_id:
                con.execute(
                    "UPDATE kernel_task_attempts SET state='cancelled',finished_at=?,error_code=? "
                    "WHERE attempt_id=? AND state='running'",
                    (timestamp, "task_cancelled", task.active_attempt_id),
                )
            revision = task.revision + 1
            con.execute(
                "UPDATE kernel_tasks SET state='cancelled',active_attempt_id=NULL,error_code=?,"
                "updated_at=?,terminal_at=?,revision=? WHERE task_id=? AND revision=?",
                ("task_cancelled", timestamp, timestamp, revision, task_id, task.revision),
            )
            event = self.events.append(TypedEvent(
                event_type="task.cancelled", source="kernel.tasks", task_id=task_id,
                correlation_id=task_id, dedup_key=f"{task_id}:revision:{revision}",
                payload={"from": task.state.value, "to": "cancelled", "reason_code": "user.cancelled"},
            ), connection=con)
            self._record_transition(
                con, task_id, task.active_attempt_id, task.state, TaskState.CANCELLED,
                "user.cancelled", event.event_id, timestamp, revision,
            )
            row = con.execute("SELECT * FROM kernel_tasks WHERE task_id=?", (task_id,)).fetchone()
            return self._from_row(row)

    def retry(self, task_id: str, *, now: float | None = None) -> TaskRecord:
        timestamp = time.time() if now is None else float(now)
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM kernel_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise TaskError(f"unknown task: {task_id}")
            task = self._from_row(row)
            if task.state != TaskState.FAILED:
                raise InvalidTransition("only failed tasks can be retried explicitly")
            if task.attempts_started >= task.max_attempts:
                raise InvalidTransition("task attempt budget is exhausted")
            revision = task.revision + 1
            con.execute(
                "UPDATE kernel_tasks SET state='retrying',next_run_at=?,error_code=NULL,"
                "updated_at=?,terminal_at=NULL,revision=? WHERE task_id=? AND revision=?",
                (timestamp, timestamp, revision, task_id, task.revision),
            )
            event = self.events.append(TypedEvent(
                event_type="task.retrying", source="kernel.tasks", task_id=task_id,
                correlation_id=task_id, dedup_key=f"{task_id}:revision:{revision}",
                payload={"from": "failed", "to": "retrying", "reason_code": "user.retry"},
            ), connection=con)
            self._record_transition(
                con, task_id, None, task.state, TaskState.RETRYING,
                "user.retry", event.event_id, timestamp, revision,
            )
            row = con.execute("SELECT * FROM kernel_tasks WHERE task_id=?", (task_id,)).fetchone()
            return self._from_row(row)

    def claim(
        self, task_id: str, plan: ExecutionPlan, *, owner_id: str,
        lease_seconds: int = 600, now: float | None = None,
    ) -> AttemptLease:
        timestamp = time.time() if now is None else float(now)
        plan_json = canonical_json(_plan_dict(plan))
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM kernel_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise TaskError(f"unknown task: {task_id}")
            task = self._from_row(row)
            if task.state not in {TaskState.READY, TaskState.RETRYING, TaskState.RECOVERING}:
                raise InvalidTransition(f"task is not claimable from {task.state.value}")
            if task.attempts_started >= task.max_attempts:
                raise InvalidTransition("task attempt budget is exhausted")
            attempt_no = task.attempts_started + 1
            fence = task.current_fence + 1
            attempt_id = uuid.uuid4().hex
            lease_until = timestamp + max(1, int(lease_seconds))
            con.execute(
                "INSERT INTO kernel_task_attempts("
                "attempt_id,task_id,attempt_no,fence_token,state,owner_id,lease_until,runtime_id,"
                "plan_id,plan_json,plan_sha256,started_at,renewed_at,finished_at,outcome_hash,error_code)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id, task_id, attempt_no, fence, "running", owner_id,
                    lease_until, plan.runtime_id, plan.plan_id, plan_json,
                    hashlib.sha256(plan_json.encode()).hexdigest(), timestamp, timestamp,
                    None, None, None,
                ),
            )
            revision = task.revision + 1
            cur = con.execute(
                "UPDATE kernel_tasks SET state=?,attempts_started=?,current_fence=?,"
                "active_attempt_id=?,updated_at=?,revision=? WHERE task_id=? AND revision=?",
                (
                    TaskState.RUNNING.value, attempt_no, fence, attempt_id,
                    timestamp, revision, task_id, task.revision,
                ),
            )
            if cur.rowcount != 1:
                raise StaleLeaseError("task changed while claiming")
            event = self.events.append(TypedEvent(
                event_type="task.started", source="kernel.tasks", task_id=task_id,
                correlation_id=task_id, dedup_key=f"{task_id}:attempt:{attempt_no}:started",
                payload={
                    "attempt_id": attempt_id, "attempt_no": attempt_no,
                    "fence_token": fence, "runtime_id": plan.runtime_id,
                    "plan_id": plan.plan_id,
                },
            ), connection=con)
            self._record_transition(
                con, task_id, attempt_id, task.state, TaskState.RUNNING,
                "attempt.started", event.event_id, timestamp, revision,
            )
        return AttemptLease(
            attempt_id, task_id, attempt_no, fence, owner_id,
            lease_until, plan.runtime_id, plan,
        )

    def _verify_lease(
        self, con: sqlite3.Connection, lease: AttemptLease, *, now: float,
    ) -> tuple[TaskRecord, sqlite3.Row]:
        task_row = con.execute("SELECT * FROM kernel_tasks WHERE task_id=?", (lease.task_id,)).fetchone()
        attempt = con.execute(
            "SELECT * FROM kernel_task_attempts WHERE attempt_id=?", (lease.attempt_id,),
        ).fetchone()
        if not task_row or not attempt:
            raise StaleLeaseError("task attempt no longer exists")
        task = self._from_row(task_row)
        valid = (
            task.state == TaskState.RUNNING
            and task.active_attempt_id == lease.attempt_id
            and task.current_fence == lease.fence_token
            and attempt["state"] == "running"
            and attempt["owner_id"] == lease.owner_id
            and int(attempt["fence_token"]) == lease.fence_token
            and float(attempt["lease_until"]) >= now
        )
        if not valid:
            raise StaleLeaseError("lease is no longer authoritative")
        return task, attempt

    def renew(
        self, lease: AttemptLease, *, lease_seconds: int = 600,
        now: float | None = None,
    ) -> AttemptLease:
        timestamp = time.time() if now is None else float(now)
        lease_until = timestamp + max(1, int(lease_seconds))
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._verify_lease(con, lease, now=timestamp)
            row = con.execute(
                "SELECT lease_until FROM kernel_task_attempts WHERE attempt_id=?",
                (lease.attempt_id,),
            ).fetchone()
            lease_until = max(float(row[0]), lease_until)
            con.execute(
                "UPDATE kernel_task_attempts SET lease_until=?,renewed_at=? WHERE attempt_id=?",
                (lease_until, timestamp, lease.attempt_id),
            )
            self.events.append(TypedEvent(
                event_type="task.lease.renewed", source="kernel.tasks", task_id=lease.task_id,
                correlation_id=lease.task_id,
                dedup_key=f"{lease.attempt_id}:renew:{int(timestamp * 1000)}",
                payload={"attempt_id": lease.attempt_id, "fence_token": lease.fence_token},
            ), connection=con)
        return AttemptLease(
            lease.attempt_id, lease.task_id, lease.attempt_no, lease.fence_token,
            lease.owner_id, lease_until, lease.runtime_id, lease.plan,
        )

    def complete(
        self, lease: AttemptLease, *, success: bool, outcome_hash: str,
        error_code: str | None = None, retryable: bool = False,
        retry_at: float | None = None, now: float | None = None,
    ) -> TaskRecord:
        timestamp = time.time() if now is None else float(now)
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            task, _ = self._verify_lease(con, lease, now=timestamp)
            if success:
                target = TaskState.COMPLETED
                attempt_state = "succeeded"
            elif retryable and task.attempts_started < task.max_attempts:
                target = TaskState.RETRYING
                attempt_state = "failed"
            else:
                target = TaskState.FAILED
                attempt_state = "failed"
            con.execute(
                "UPDATE kernel_task_attempts SET state=?,finished_at=?,outcome_hash=?,error_code=? "
                "WHERE attempt_id=? AND state='running'",
                (attempt_state, timestamp, outcome_hash, error_code, lease.attempt_id),
            )
            revision = task.revision + 1
            terminal = timestamp if target in TERMINAL_STATES else None
            next_run = retry_at if retry_at is not None else timestamp
            con.execute(
                "UPDATE kernel_tasks SET state=?,active_attempt_id=NULL,next_run_at=?,result_hash=?,"
                "error_code=?,updated_at=?,terminal_at=?,revision=? WHERE task_id=? AND revision=?",
                (
                    target.value, next_run, outcome_hash if success else None,
                    error_code, timestamp, terminal, revision, task.task_id, task.revision,
                ),
            )
            event = self.events.append(TypedEvent(
                event_type=f"task.{target.value}", source="kernel.tasks", task_id=task.task_id,
                correlation_id=task.task_id, causation_id=None,
                dedup_key=f"{lease.attempt_id}:completed",
                payload={
                    "attempt_id": lease.attempt_id, "attempt_no": lease.attempt_no,
                    "fence_token": lease.fence_token, "success": success,
                    "outcome_hash": outcome_hash, "error_code": error_code or "",
                },
            ), connection=con)
            self._record_transition(
                con, task.task_id, lease.attempt_id, task.state, target,
                "attempt.completed", event.event_id, timestamp, revision,
            )
            row = con.execute("SELECT * FROM kernel_tasks WHERE task_id=?", (task.task_id,)).fetchone()
            return self._from_row(row)

    def recover_expired(self, *, now: float | None = None, limit: int = 100) -> int:
        timestamp = time.time() if now is None else float(now)
        recovered = 0
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT a.*,t.state AS task_state,t.max_attempts,t.attempts_started,t.revision "
                "FROM kernel_task_attempts a JOIN kernel_tasks t ON t.task_id=a.task_id "
                "WHERE a.state='running' AND a.lease_until<? AND t.state='running' "
                "AND t.active_attempt_id=a.attempt_id AND t.current_fence=a.fence_token "
                "ORDER BY a.lease_until LIMIT ?",
                (timestamp, max(1, min(int(limit), 1000))),
            ).fetchall()
            for row in rows:
                target = (
                    TaskState.RECOVERING
                    if int(row["attempts_started"]) < int(row["max_attempts"])
                    else TaskState.FAILED
                )
                con.execute(
                    "UPDATE kernel_task_attempts SET state='expired',finished_at=?,error_code=? "
                    "WHERE attempt_id=? AND state='running'",
                    (timestamp, "lease_expired", row["attempt_id"]),
                )
                revision = int(row["revision"]) + 1
                con.execute(
                    "UPDATE kernel_tasks SET state=?,active_attempt_id=NULL,error_code=?,"
                    "next_run_at=?,updated_at=?,terminal_at=?,revision=? WHERE task_id=? AND revision=?",
                    (
                        target.value, "lease_expired", timestamp, timestamp,
                        timestamp if target == TaskState.FAILED else None,
                        revision, row["task_id"], row["revision"],
                    ),
                )
                event = self.events.append(TypedEvent(
                    event_type="task.lease.expired", source="kernel.tasks", task_id=row["task_id"],
                    correlation_id=row["task_id"], dedup_key=f"{row['attempt_id']}:expired",
                    payload={
                        "attempt_id": row["attempt_id"],
                        "fence_token": int(row["fence_token"]),
                        "recovery_state": target.value,
                    },
                ), connection=con)
                self._record_transition(
                    con, row["task_id"], row["attempt_id"], TaskState.RUNNING,
                    target, "lease.expired", event.event_id, timestamp, revision,
                )
                recovered += 1
        return recovered
