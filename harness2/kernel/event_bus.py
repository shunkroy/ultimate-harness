"""Persistent, replayable, schema-versioned operational events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional


MAX_EVENT_BYTES = 64 * 1024


class EventValidationError(ValueError):
    pass


class EventConflict(EventValidationError):
    pass


def canonical_json(value: Mapping[str, Any], *, limit: int = MAX_EVENT_BYTES) -> str:
    if not isinstance(value, Mapping):
        raise EventValidationError("event payload must be an object")
    try:
        encoded = json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EventValidationError(f"event payload is not canonical JSON: {exc}") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise EventValidationError(f"event payload exceeds {limit} bytes")
    return encoded


@dataclass(frozen=True)
class TypedEvent:
    event_type: str
    source: str
    correlation_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    task_id: Optional[str] = None
    causation_id: Optional[str] = None
    schema_version: int = 1
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurred_at: float = field(default_factory=time.time)
    dedup_key: Optional[str] = None
    seq: Optional[int] = None
    recorded_at: Optional[float] = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.event_id, "event_id"), (self.event_type, "event_type"),
            (self.source, "source"), (self.correlation_id, "correlation_id"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise EventValidationError(f"{name} must be a non-empty string")
        if self.schema_version < 1:
            raise EventValidationError("schema_version must be positive")
        canonical_json(self.payload)
        canonical_json(self.metadata, limit=16 * 1024)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "source": self.source,
            "task_id": self.task_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "payload": dict(self.payload),
            "metadata": dict(self.metadata),
            "dedup_key": self.dedup_key,
        }


class EventBus:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _from_row(row: sqlite3.Row) -> TypedEvent:
        return TypedEvent(
            event_id=str(row["event_id"]),
            event_type=str(row["event_type"]),
            schema_version=int(row["schema_version"]),
            source=str(row["source"]),
            task_id=row["task_id"],
            correlation_id=str(row["correlation_id"]),
            causation_id=row["causation_id"],
            occurred_at=float(row["occurred_at"]),
            recorded_at=float(row["recorded_at"]),
            payload=json.loads(row["payload_json"]),
            metadata=json.loads(row["metadata_json"]),
            dedup_key=row["dedup_key"],
            seq=int(row["seq"]),
        )

    def append(self, event: TypedEvent, *, connection: sqlite3.Connection | None = None) -> TypedEvent:
        payload = canonical_json(event.payload)
        metadata = canonical_json(event.metadata, limit=16 * 1024)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        recorded = time.time()

        def equivalent(existing: TypedEvent) -> bool:
            return (
                existing.event_id == event.event_id
                and existing.event_type == event.event_type
                and existing.schema_version == event.schema_version
                and existing.source == event.source
                and existing.task_id == event.task_id
                and existing.correlation_id == event.correlation_id
                and existing.causation_id == event.causation_id
                and existing.occurred_at == event.occurred_at
                and existing.dedup_key == event.dedup_key
                and canonical_json(existing.payload) == payload
                and canonical_json(existing.metadata, limit=16 * 1024) == metadata
            )

        def write(con: sqlite3.Connection) -> TypedEvent:
            if event.dedup_key is not None:
                row = con.execute(
                    "SELECT * FROM kernel_events WHERE source=? AND dedup_key=?",
                    (event.source, event.dedup_key),
                ).fetchone()
                if row:
                    existing = self._from_row(row)
                    if not equivalent(existing):
                        raise EventConflict("event dedup key already has different content")
                    return existing
            try:
                cur = con.execute(
                    "INSERT INTO kernel_events("
                    "event_id,event_type,schema_version,source,task_id,correlation_id,"
                    "causation_id,occurred_at,recorded_at,payload_json,payload_sha256,"
                    "metadata_json,dedup_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id, event.event_type, event.schema_version,
                        event.source, event.task_id, event.correlation_id,
                        event.causation_id, event.occurred_at, recorded, payload,
                        digest, metadata, event.dedup_key,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                row = con.execute(
                    "SELECT * FROM kernel_events WHERE event_id=?", (event.event_id,),
                ).fetchone()
                if row:
                    existing = self._from_row(row)
                    if equivalent(existing):
                        return existing
                raise EventConflict("event identity already exists") from exc
            row = con.execute("SELECT * FROM kernel_events WHERE seq=?", (cur.lastrowid,)).fetchone()
            return self._from_row(row)

        if connection is not None:
            return write(connection)
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            return write(con)

    def replay(
        self, *, after_seq: int = 0, limit: int = 100,
        event_type: str | None = None, task_id: str | None = None,
    ) -> tuple[TypedEvent, ...]:
        clauses = ["seq>?"]
        values: list[Any] = [max(0, int(after_seq))]
        if event_type:
            clauses.append("event_type=?")
            values.append(event_type)
        if task_id:
            clauses.append("task_id=?")
            values.append(task_id)
        values.append(max(1, min(int(limit), 1000)))
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT * FROM kernel_events WHERE " + " AND ".join(clauses)
                + " ORDER BY seq LIMIT ?", values,
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def ack(self, consumer_id: str, through_seq: int) -> int:
        if not consumer_id.strip() or through_seq < 0:
            raise EventValidationError("invalid consumer acknowledgement")
        with self.store.connect() as con:
            high_water = int(con.execute("SELECT COALESCE(MAX(seq),0) FROM kernel_events").fetchone()[0])
            if int(through_seq) > high_water:
                raise EventValidationError(
                    f"consumer acknowledgement {through_seq} exceeds event high-water {high_water}"
                )
            con.execute(
                "INSERT INTO kernel_event_consumers(consumer_id,last_seq,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(consumer_id) DO UPDATE SET "
                "last_seq=MAX(last_seq,excluded.last_seq),updated_at=excluded.updated_at",
                (consumer_id, int(through_seq), time.time()),
            )
            row = con.execute(
                "SELECT last_seq FROM kernel_event_consumers WHERE consumer_id=?", (consumer_id,),
            ).fetchone()
        return int(row[0])

    def cursor(self, consumer_id: str) -> int:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT last_seq FROM kernel_event_consumers WHERE consumer_id=?", (consumer_id,),
            ).fetchone()
        return int(row[0]) if row else 0
