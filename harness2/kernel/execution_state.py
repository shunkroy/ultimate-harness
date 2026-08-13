"""Immutable payload binding, fenced checkpoints and source snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .event_bus import EventBus, TypedEvent, canonical_json
from .payloads import (
    CheckpointReference,
    PayloadError,
    PayloadIntegrityError,
    PayloadReference,
    TaskPayload,
    canonical_bytes,
)
from .task_types import TaskTypeDescriptor, TaskTypeRegistry
from .tasks import AttemptLease, StaleLeaseError, TaskRepository


MAX_SOURCE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class SourceSnapshot:
    snapshot_id: str
    payload: PayloadReference
    source_type: str
    source_identifier_hash: str
    source_revision: Optional[str]
    content_sha256: str
    size_bytes: int
    media_type: str
    metadata: Mapping[str, Any]
    captured_at: float

    def as_dict(self) -> dict[str, Any]:
        value = dict(self.__dict__)
        value["payload"] = self.payload.as_dict()
        value["metadata"] = dict(self.metadata)
        return value


class ExecutionStateRepository:
    def __init__(self, store, events: EventBus, tasks: TaskRepository, objects, task_types: TaskTypeRegistry):
        self.store = store
        self.events = events
        self.tasks = tasks
        self.objects = objects
        self.task_types = task_types

    @staticmethod
    def _reference_from_row(row, prefix: str = "") -> PayloadReference:
        key = lambda name: row[prefix + name]
        return PayloadReference(
            str(key("reference_id")), str(key("backend_id")), str(key("object_key")),
            str(key("content_sha256")), int(key("size_bytes")), str(key("media_type")),
            str(key("schema_id")), str(key("purpose")), int(key("envelope_version")),
            str(key("key_id")), str(key("reference_mac")),
        )

    @staticmethod
    def _insert_reference(con, reference: PayloadReference, now: float) -> None:
        con.execute(
            "INSERT OR IGNORE INTO kernel_payload_references("
            "reference_id,backend_id,object_key,content_sha256,size_bytes,media_type,"
            "schema_id,purpose,envelope_version,key_id,reference_mac,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                reference.reference_id, reference.backend_id, reference.object_key,
                reference.content_sha256, reference.size_bytes, reference.media_type,
                reference.schema_id, reference.purpose, reference.envelope_version,
                reference.key_id, reference.reference_mac, now,
            ),
        )
        row = con.execute(
            "SELECT * FROM kernel_payload_references WHERE reference_id=?",
            (reference.reference_id,),
        ).fetchone()
        if not row or ExecutionStateRepository._reference_from_row(row) != reference:
            raise PayloadIntegrityError("payload reference identity conflict")

    def create_task(
        self, payload: TaskPayload, *, task_id: str | None = None,
        idempotency_key: str | None = None, source: str = "application",
        reason: str = "typed submission", authority: str = "authenticated_user",
        priority: int = 0, max_attempts: int | None = None,
    ):
        descriptor = self.task_types.require(payload.task_type)
        descriptor.validate(payload)
        actual_task_id = task_id or uuid.uuid4().hex
        binding = {"task_id": actual_task_id, "role": "input", "task_type": payload.task_type}
        reference = self.objects.put(
            payload.canonical, schema_id=payload.schema_id, purpose="task.input",
            binding=binding,
        )
        from .contracts import ExecutionRequest
        plain = payload.as_dict()
        request = ExecutionRequest(
            actual_task_id, payload.objective, descriptor.required_capabilities,
            payload.constraints.get("preferred_runtime"), plain["inputs"],
            plain["constraints"], plain["budget"],
        )
        now = time.time()
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            task = self._create_task(
                con, payload, descriptor, request, reference,
                idempotency_key=idempotency_key, source=source, reason=reason,
                authority=authority, priority=priority,
                max_attempts=max_attempts or descriptor.max_attempts, now=now,
            )
        return task, reference

    def _create_task(
        self, con: sqlite3.Connection, payload: TaskPayload,
        descriptor: TaskTypeDescriptor, request, reference: PayloadReference, *,
        idempotency_key: str | None, source: str, reason: str, authority: str,
        priority: int, max_attempts: int, now: float,
    ):
        task = self.tasks._submit(
            con, request, task_type=payload.task_type, source=source, reason=reason,
            authority=authority, priority=priority, idempotency_key=idempotency_key,
            max_attempts=max_attempts, now=now,
        )
        self._insert_reference(con, reference, now)
        existing = con.execute(
            "SELECT reference_id FROM kernel_task_payload_bindings WHERE task_id=? AND role='input'",
            (task.task_id,),
        ).fetchone()
        if existing and str(existing[0]) != reference.reference_id:
            raise PayloadIntegrityError("task already binds a different immutable payload")
        con.execute(
            "INSERT OR IGNORE INTO kernel_task_payload_bindings(task_id,role,reference_id,bound_at) "
            "VALUES(?,?,?,?)", (task.task_id, "input", reference.reference_id, now),
        )
        resources = canonical_json(descriptor.resources.as_dict())
        con.execute(
            "INSERT OR IGNORE INTO kernel_task_type_snapshots("
            "task_id,task_type,descriptor_version,descriptor_sha256,resource_requirements_json,"
            "side_effect_class,resumable) VALUES(?,?,?,?,?,?,?)",
            (
                task.task_id, descriptor.task_type, descriptor.version,
                descriptor.descriptor_hash, resources,
                descriptor.side_effect_class.value, int(descriptor.resumable),
            ),
        )
        self.events.append(TypedEvent(
            event_type="task.payload.bound", source="kernel.execution-state",
            task_id=task.task_id, correlation_id=task.task_id,
            dedup_key=f"{task.task_id}:payload:input",
            payload={
                "payload_hash": reference.content_sha256,
                "reference_id": reference.reference_id,
                "task_type": descriptor.task_type,
                "descriptor_hash": descriptor.descriptor_hash,
            },
        ), connection=con)
        return task

    def payload_reference(self, task_id: str, *, role: str = "input") -> PayloadReference:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT r.* FROM kernel_task_payload_bindings b "
                "JOIN kernel_payload_references r ON r.reference_id=b.reference_id "
                "WHERE b.task_id=? AND b.role=?", (task_id, role),
            ).fetchone()
        if not row:
            raise PayloadError(f"task has no payload role {role}")
        return self._reference_from_row(row)

    def load_task_payload(self, task_id: str) -> TaskPayload:
        reference = self.payload_reference(task_id)
        with self.store.connect() as con:
            row = con.execute(
                "SELECT task_type,descriptor_sha256 FROM kernel_task_type_snapshots WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if not row:
            raise PayloadIntegrityError("task descriptor snapshot is missing")
        descriptor = self.task_types.require(str(row["task_type"]))
        if descriptor.descriptor_hash != str(row["descriptor_sha256"]):
            raise PayloadIntegrityError("task descriptor changed after submission")
        binding = {"task_id": task_id, "role": "input", "task_type": str(row[0])}
        data = self.objects.get(reference, binding=binding)
        payload = TaskPayload.from_bytes(data)
        descriptor.validate(payload)
        return payload

    def checkpoint(
        self, lease: AttemptLease, payload_data: Mapping[str, Any], *,
        workflow_step: str, resource_versions: Mapping[str, str] = {},
        resumable: bool = True, now: float | None = None,
    ) -> CheckpointReference:
        timestamp = time.time() if now is None else float(now)
        if not workflow_step or len(workflow_step) > 256:
            raise PayloadError("invalid checkpoint workflow step")
        data = canonical_bytes({
            "schema": "harness.checkpoint-payload/1",
            "workflow_step": workflow_step,
            "state": dict(payload_data),
        })
        binding = {
            "task_id": lease.task_id, "attempt_id": lease.attempt_id,
            "fence_token": lease.fence_token, "purpose": "task.checkpoint",
        }
        reference = self.objects.put(
            data, schema_id="harness.checkpoint-payload/1",
            purpose="task.checkpoint", binding=binding,
        )
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            task, attempt = self.tasks._verify_lease(con, lease, now=timestamp)
            row = con.execute(
                "SELECT checkpoint_seq,integrity_hash FROM kernel_task_checkpoints "
                "WHERE task_id=? ORDER BY checkpoint_seq DESC LIMIT 1", (lease.task_id,),
            ).fetchone()
            sequence = int(row[0]) + 1 if row else 1
            parent_hash = str(row[1]) if row else "0" * 64
            self._insert_reference(con, reference, timestamp)
            resource_json = canonical_json(dict(resource_versions), limit=16 * 1024)
            checkpoint_id = "checkpoint-" + hashlib.sha256(
                canonical_bytes({
                    "task_id": lease.task_id, "attempt_id": lease.attempt_id,
                    "fence_token": lease.fence_token, "sequence": sequence,
                    "workflow_step": workflow_step, "resource_versions": dict(resource_versions),
                    "reference_id": reference.reference_id,
                    "payload_hash": reference.content_sha256,
                    "parent_checkpoint_hash": parent_hash,
                    "resumable": resumable, "schema_version": 1,
                }, max_bytes=64 * 1024)
            ).hexdigest()
            integrity_hash = hashlib.sha256(
                b"harness.checkpoint/v1\0" + checkpoint_id.encode()
                + b"\0" + parent_hash.encode()
            ).hexdigest()
            event = self.events.append(TypedEvent(
                event_type="task.checkpoint.created", source="kernel.execution-state",
                task_id=lease.task_id, correlation_id=lease.task_id,
                dedup_key=f"{lease.attempt_id}:checkpoint:{sequence}",
                payload={
                    "attempt_id": lease.attempt_id, "fence_token": lease.fence_token,
                    "checkpoint_id": checkpoint_id, "checkpoint_seq": sequence,
                    "payload_hash": reference.content_sha256,
                    "workflow_step": workflow_step,
                },
            ), connection=con)
            con.execute(
                "INSERT INTO kernel_task_checkpoints("
                "checkpoint_id,task_id,attempt_id,fence_token,checkpoint_seq,workflow_step,"
                "resource_versions_json,reference_id,parent_checkpoint_hash,integrity_hash,"
                "resumable,schema_version,created_at,event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    checkpoint_id, lease.task_id, lease.attempt_id, lease.fence_token,
                    sequence, workflow_step, resource_json, reference.reference_id,
                    parent_hash, integrity_hash, int(resumable), 1, timestamp, event.event_id,
                ),
            )
        return CheckpointReference(
            checkpoint_id, lease.task_id, lease.attempt_id, lease.fence_token,
            sequence, reference, workflow_step, dict(resource_versions),
            parent_hash, integrity_hash, timestamp, resumable,
        )

    def latest_checkpoint(self, task_id: str, *, verify_payload: bool = True) -> CheckpointReference | None:
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT c.*,r.backend_id,r.object_key,r.content_sha256,r.size_bytes,r.media_type,"
                "r.schema_id,r.purpose,r.envelope_version,r.key_id,r.reference_mac "
                "FROM kernel_task_checkpoints c JOIN kernel_payload_references r "
                "ON r.reference_id=c.reference_id WHERE c.task_id=? "
                "ORDER BY c.checkpoint_seq", (task_id,),
            ).fetchall()
        if not rows:
            return None
        parent_hash = "0" * 64
        result = None
        for expected_sequence, row in enumerate(rows, 1):
            try:
                resource_versions = json.loads(row["resource_versions_json"])
            except (TypeError, ValueError) as exc:
                raise PayloadIntegrityError("checkpoint resource metadata is invalid") from exc
            if (
                not isinstance(resource_versions, dict)
                or canonical_json(resource_versions, limit=16 * 1024) != row["resource_versions_json"]
            ):
                raise PayloadIntegrityError("checkpoint resource metadata is not canonical")
            reference = self._reference_from_row(row)
            current = CheckpointReference(
                str(row["checkpoint_id"]), str(row["task_id"]), str(row["attempt_id"]),
                int(row["fence_token"]), int(row["checkpoint_seq"]), reference,
                str(row["workflow_step"]), resource_versions,
                str(row["parent_checkpoint_hash"]), str(row["integrity_hash"]),
                float(row["created_at"]), bool(row["resumable"]), int(row["schema_version"]),
            )
            if current.sequence != expected_sequence or current.parent_checkpoint_hash != parent_hash:
                raise PayloadIntegrityError("checkpoint chain is not contiguous")
            expected_id = "checkpoint-" + hashlib.sha256(canonical_bytes({
                "task_id": current.task_id, "attempt_id": current.attempt_id,
                "fence_token": current.fence_token, "sequence": current.sequence,
                "workflow_step": current.workflow_step,
                "resource_versions": dict(current.resource_versions),
                "reference_id": current.payload.reference_id,
                "payload_hash": current.payload.content_sha256,
                "parent_checkpoint_hash": current.parent_checkpoint_hash,
                "resumable": current.resumable, "schema_version": current.schema_version,
            }, max_bytes=64 * 1024)).hexdigest()
            expected_integrity = hashlib.sha256(
                b"harness.checkpoint/v1\0" + expected_id.encode()
                + b"\0" + current.parent_checkpoint_hash.encode()
            ).hexdigest()
            if expected_id != current.checkpoint_id or expected_integrity != current.integrity_hash:
                raise PayloadIntegrityError("checkpoint metadata integrity failed")
            if verify_payload:
                self.objects.get(current.payload, binding={
                    "task_id": current.task_id, "attempt_id": current.attempt_id,
                    "fence_token": current.fence_token, "purpose": "task.checkpoint",
                })
            parent_hash = current.integrity_hash
            result = current
        return result

    def capture_source(
        self, path: str, *, source_type: str = "file", source_revision: str | None = None,
        metadata: Mapping[str, Any] = {}, media_type: str = "text/plain",
    ) -> SourceSnapshot:
        absolute = os.path.abspath(os.path.expanduser(path))
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(absolute, flags)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise PayloadError("source must be a regular file")
            before_identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

            def read_once() -> bytes:
                os.lseek(fd, 0, os.SEEK_SET)
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(fd, min(1024 * 1024, MAX_SOURCE_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_SOURCE_BYTES:
                        raise PayloadError("source exceeds 10 MiB")
                return b"".join(chunks)

            content = read_once()
            if read_once() != content:
                raise PayloadError("source changed while it was captured")
            after = os.fstat(fd)
            if before_identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise PayloadError("source changed while it was captured")
        finally:
            os.close(fd)
        content_hash = hashlib.sha256(content).hexdigest()
        identifier_hash = hashlib.sha256(absolute.encode("utf-8", "surrogatepass")).hexdigest()
        binding = {
            "source_type": source_type, "source_identifier_hash": identifier_hash,
            "source_revision": source_revision or "", "purpose": "context.source",
        }
        reference = self.objects.put(
            content, schema_id="harness.source-snapshot/1",
            purpose="context.source", binding=binding, media_type=media_type,
        )
        snapshot_id = "snapshot-" + hashlib.sha256(
            b"harness.source-snapshot/v1\0" + reference.reference_id.encode()
            + b"\0" + identifier_hash.encode()
        ).hexdigest()
        captured = time.time()
        metadata_json = canonical_json(dict(metadata), limit=16 * 1024)
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._insert_reference(con, reference, captured)
            con.execute(
                "INSERT OR IGNORE INTO kernel_source_snapshots("
                "snapshot_id,reference_id,source_type,source_identifier_hash,source_revision,"
                "content_sha256,size_bytes,media_type,metadata_json,captured_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id, reference.reference_id, source_type, identifier_hash,
                    source_revision, content_hash, len(content), media_type,
                    metadata_json, captured,
                ),
            )
        return self.source_snapshot(snapshot_id)

    def load_source(self, snapshot: SourceSnapshot) -> bytes:
        binding = {
            "source_type": snapshot.source_type,
            "source_identifier_hash": snapshot.source_identifier_hash,
            "source_revision": snapshot.source_revision or "",
            "purpose": "context.source",
        }
        content = self.objects.get(snapshot.payload, binding=binding)
        if hashlib.sha256(content).hexdigest() != snapshot.content_sha256:
            raise PayloadIntegrityError("source snapshot content mismatch")
        if len(content) != snapshot.size_bytes:
            raise PayloadIntegrityError("source snapshot size mismatch")
        return content

    def source_snapshot(self, snapshot_id: str) -> SourceSnapshot:
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith("snapshot-"):
            raise PayloadError("invalid source snapshot id")
        with self.store.connect() as con:
            row = con.execute(
                "SELECT s.*,r.backend_id,r.object_key,r.reference_id,r.content_sha256 AS ref_content_sha256,"
                "r.size_bytes AS ref_size_bytes,r.media_type AS ref_media_type,r.schema_id,r.purpose,"
                "r.envelope_version,r.key_id,r.reference_mac FROM kernel_source_snapshots s "
                "JOIN kernel_payload_references r ON r.reference_id=s.reference_id "
                "WHERE s.snapshot_id=?", (snapshot_id,),
            ).fetchone()
        if not row:
            raise PayloadError("unknown source snapshot")
        reference = PayloadReference(
            str(row["reference_id"]), str(row["backend_id"]), str(row["object_key"]),
            str(row["ref_content_sha256"]), int(row["ref_size_bytes"]),
            str(row["ref_media_type"]), str(row["schema_id"]), str(row["purpose"]),
            int(row["envelope_version"]), str(row["key_id"]), str(row["reference_mac"]),
        )
        source_type = str(row["source_type"])
        identifier_hash = str(row["source_identifier_hash"])
        revision = row["source_revision"]
        expected_id = "snapshot-" + hashlib.sha256(
            b"harness.source-snapshot/v1\0" + reference.reference_id.encode()
            + b"\0" + identifier_hash.encode()
        ).hexdigest()
        if expected_id != snapshot_id:
            raise PayloadIntegrityError("source snapshot identity mismatch")
        if reference.schema_id != "harness.source-snapshot/1" or reference.purpose != "context.source":
            raise PayloadIntegrityError("source snapshot reference domain mismatch")
        content = self.objects.get(reference, binding={
            "source_type": source_type,
            "source_identifier_hash": identifier_hash,
            "source_revision": revision or "",
            "purpose": "context.source",
        })
        if (
            int(row["size_bytes"]) != reference.size_bytes
            or len(content) != int(row["size_bytes"])
            or str(row["media_type"]) != reference.media_type
            or str(row["content_sha256"]) != hashlib.sha256(content).hexdigest()
        ):
            raise PayloadIntegrityError("source snapshot metadata mismatch")
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except (TypeError, ValueError) as exc:
            raise PayloadIntegrityError("source snapshot metadata is invalid") from exc
        if not isinstance(metadata, dict) or canonical_json(metadata, limit=16 * 1024) != str(row["metadata_json"]):
            raise PayloadIntegrityError("source snapshot metadata is not canonical")
        return SourceSnapshot(
            snapshot_id, reference, source_type, identifier_hash,
            str(revision) if revision is not None else None,
            str(row["content_sha256"]), int(row["size_bytes"]),
            str(row["media_type"]), metadata, float(row["captured_at"]),
        )
