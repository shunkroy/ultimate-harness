"""Additive migration, typed event and fenced task-state tests."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from harness2.kernel.contracts import ExecutionPlan, ExecutionRequest
from harness2.kernel.event_bus import EventBus, EventConflict, EventValidationError, TypedEvent
from harness2.kernel.migrations import (
    MIGRATIONS,
    Migration,
    MigrationDriftError,
    Migrator,
    SchemaTooNewError,
)
from harness2.kernel.tasks import (
    InvalidTransition,
    StaleLeaseError,
    TaskIdempotencyConflict,
    TaskRepository,
    TaskState,
)
from harness2.store import SCHEMA, Store


class KernelStateCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "state", "harness.db")
        self.store = Store(self.path)
        self.events = EventBus(self.store)
        self.tasks = TaskRepository(self.store, self.events)

    @staticmethod
    def request(task_id="task-1", objective="do bounded work"):
        return ExecutionRequest(
            task_id=task_id,
            objective=objective,
            required_capabilities=("code.execute",),
            preferred_runtime="provider-a",
            constraints={"privacy": "standard"},
            budget={"timeout_seconds": 30},
        )

    @staticmethod
    def plan(task_id="task-1"):
        return ExecutionPlan(
            plan_id="plan-1", task_id=task_id, runtime_id="provider-a",
            capabilities=("code.execute",), steps=("execute",),
            verification=("typed-outcome",), reason="explicit runtime",
        )


class MigrationTests(KernelStateCase):
    def test_fresh_store_applies_contiguous_additive_schema(self):
        self.assertEqual(self.store.schema_version(), len(MIGRATIONS))
        with self.store.connect() as con:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertTrue({"jobs", "audit", "kernel_events", "kernel_tasks"}.issubset(tables))

    def test_realistic_legacy_database_is_preserved(self):
        legacy = os.path.join(self.temp.name, "legacy.db")
        con = sqlite3.connect(legacy)
        con.executescript(SCHEMA)
        con.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES('legacy','preserve',1)"
        )
        con.commit()
        con.close()
        Migrator(legacy).migrate()
        con = sqlite3.connect(legacy)
        self.assertEqual(con.execute("SELECT value FROM settings WHERE key='legacy'").fetchone()[0], "preserve")
        self.assertEqual(con.execute("PRAGMA quick_check").fetchone()[0], "ok")
        con.close()

    def test_migration_is_idempotent_and_detects_drift_and_newer_schema(self):
        migrator = Migrator(self.path)
        self.assertEqual(migrator.migrate(), len(MIGRATIONS))
        altered = list(MIGRATIONS)
        altered[0] = Migration(1, altered[0].name, altered[0].statements + ("SELECT 1",))
        with self.assertRaises(MigrationDriftError):
            Migrator(self.path, altered).migrate()
        with self.store.connect() as con:
            con.execute(
                "INSERT INTO kernel_schema_migrations(version,name,checksum,applied_at) "
                "VALUES(99,'future','x',1)"
            )
        with self.assertRaises(SchemaTooNewError):
            migrator.migrate()

    def test_failed_migration_rolls_back_ddl_and_marker(self):
        path = os.path.join(self.temp.name, "failed.db")
        sqlite3.connect(path).close()
        bad = (Migration(1, "broken", (
            "CREATE TABLE should_rollback(id INTEGER)",
            "CREATE TABLE should_rollback(id INTEGER)",
        )),)
        with self.assertRaises(sqlite3.OperationalError):
            Migrator(path, bad).migrate()
        con = sqlite3.connect(path)
        self.assertIsNone(con.execute(
            "SELECT name FROM sqlite_master WHERE name='should_rollback'"
        ).fetchone())
        self.assertEqual(con.execute("SELECT COUNT(*) FROM kernel_schema_migrations").fetchone()[0], 0)
        con.close()

    def test_locked_database_fails_predictably_without_partial_task(self):
        lock = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        lock.execute("BEGIN EXCLUSIVE")
        original_connect = sqlite3.connect
        try:
            with patch("harness2.store.sqlite3.connect", wraps=sqlite3.connect) as connect:
                connect.side_effect = lambda *args, **kwargs: original_connect(
                    *args, **{**kwargs, "timeout": 0.01}
                )
                with self.assertRaises(sqlite3.OperationalError):
                    self.tasks.submit(self.request(task_id="locked"), idempotency_key="locked")
        finally:
            lock.rollback()
            lock.close()
        self.assertIsNone(self.tasks.get("locked"))


class EventBusTests(KernelStateCase):
    def test_replay_order_dedup_and_conflict(self):
        first = TypedEvent(
            event_id="event-1", event_type="task.created", source="test",
            correlation_id="task-1", task_id="task-1", dedup_key="created",
            payload={"state": "created"}, occurred_at=100,
        )
        stored = self.events.append(first)
        self.assertEqual(self.events.append(first).seq, stored.seq)
        second = self.events.append(TypedEvent(
            event_id="event-2", event_type="task.ready", source="test",
            correlation_id="task-1", task_id="task-1", dedup_key="ready",
            payload={"state": "ready"}, occurred_at=1,
        ))
        self.assertGreater(second.seq, stored.seq)
        self.assertEqual([event.event_id for event in self.events.replay(task_id="task-1")], ["event-1", "event-2"])
        with self.assertRaises(EventConflict):
            self.events.append(TypedEvent(
                event_id="different", event_type="task.created", source="test",
                correlation_id="task-1", task_id="task-1", dedup_key="created",
                payload={"state": "different"},
            ))

    def test_invalid_payload_and_monotonic_consumer_cursor(self):
        with self.assertRaises(EventValidationError):
            TypedEvent(
                event_type="bad", source="test", correlation_id="x",
                payload={"bad": float("nan")},
            )
        event = self.events.append(TypedEvent(
            event_type="task.created", source="test", correlation_id="x",
            payload={},
        ))
        self.assertEqual(self.events.ack("projection", event.seq), event.seq)
        self.assertEqual(self.events.ack("projection", 0), event.seq)
        self.assertEqual(self.events.cursor("projection"), event.seq)
        with self.assertRaises(EventValidationError):
            self.events.ack("projection", event.seq + 1)

    def test_same_event_id_with_different_semantics_is_conflict(self):
        original = TypedEvent(
            event_id="same", event_type="one", source="source-a",
            correlation_id="a", payload={"value": 1}, occurred_at=1,
        )
        self.events.append(original)
        with self.assertRaises(EventConflict):
            self.events.append(TypedEvent(
                event_id="same", event_type="two", source="source-b",
                correlation_id="b", payload={"value": 1}, occurred_at=1,
            ))


class TaskStateTests(KernelStateCase):
    def test_submit_is_idempotent_without_persisting_objective(self):
        request = self.request(objective="private objective text")
        one = self.tasks.submit(request, idempotency_key="request-1", max_attempts=2)
        two = self.tasks.submit(request, idempotency_key="request-1", max_attempts=2)
        self.assertEqual(one.task_id, two.task_id)
        with open(self.path, "rb") as stream:
            self.assertNotIn(b"private objective text", stream.read())
        with self.assertRaises(TaskIdempotencyConflict):
            self.tasks.submit(
                self.request(task_id="task-2", objective="different"),
                idempotency_key="request-1",
            )
        with self.assertRaises(TaskIdempotencyConflict):
            self.tasks.submit(
                self.request(task_id="task-1", objective="mutated"),
                idempotency_key="different-key",
            )
        with self.assertRaises(TaskIdempotencyConflict):
            self.tasks.submit(
                request, idempotency_key="request-1", authority="different",
            )

    def test_legal_transitions_and_atomic_events(self):
        task = self.tasks.submit(self.request(), idempotency_key="one")
        self.assertEqual(task.state, TaskState.CREATED)
        task = self.tasks.prepare(task.task_id)
        self.assertEqual(task.state, TaskState.READY)
        with self.assertRaises(InvalidTransition):
            self.tasks.transition(task.task_id, TaskState.COMPLETED, reason_code="illegal")
        self.assertEqual(self.tasks.get(task.task_id).state, TaskState.READY)
        self.assertEqual(
            [event.event_type for event in self.events.replay(task_id=task.task_id)],
            ["task.created", "task.planned", "task.ready"],
        )

    def test_fenced_expiry_rejects_stale_completion(self):
        self.tasks.submit(self.request(), idempotency_key="lease", max_attempts=2, now=10)
        self.tasks.prepare("task-1", now=10)
        first = self.tasks.claim("task-1", self.plan(), owner_id="worker-a", lease_seconds=5, now=10)
        self.assertEqual(self.tasks.recover_expired(now=16), 1)
        self.assertEqual(self.tasks.get("task-1").state, TaskState.RECOVERING)
        second = self.tasks.claim("task-1", self.plan(), owner_id="worker-b", lease_seconds=5, now=16)
        self.assertGreater(second.fence_token, first.fence_token)
        with self.assertRaises(StaleLeaseError):
            self.tasks.complete(first, success=True, outcome_hash="old", now=17)
        completed = self.tasks.complete(second, success=True, outcome_hash="new", now=17)
        self.assertEqual(completed.state, TaskState.COMPLETED)

    def test_expired_lease_is_rejected_without_waiting_for_sweeper(self):
        self.tasks.submit(self.request(), idempotency_key="expiry", now=10)
        self.tasks.prepare("task-1", now=10)
        lease = self.tasks.claim("task-1", self.plan(), owner_id="worker", lease_seconds=1, now=10)
        with self.assertRaises(StaleLeaseError):
            self.tasks.complete(lease, success=True, outcome_hash="late", now=12)
        with self.assertRaises(StaleLeaseError):
            self.tasks.renew(lease, lease_seconds=10, now=12)

    def test_claim_is_single_winner(self):
        self.tasks.submit(self.request(), idempotency_key="race")
        self.tasks.prepare("task-1")
        barrier = threading.Barrier(2)
        outcomes = []

        def claim(owner):
            barrier.wait()
            try:
                outcomes.append(self.tasks.claim("task-1", self.plan(), owner_id=owner))
            except InvalidTransition:
                outcomes.append(None)

        workers = [threading.Thread(target=claim, args=(name,)) for name in ("a", "b")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(sum(value is not None for value in outcomes), 1)

    def test_cancel_active_task_fences_completion(self):
        self.tasks.submit(self.request(), idempotency_key="cancel")
        self.tasks.prepare("task-1")
        lease = self.tasks.claim("task-1", self.plan(), owner_id="worker")
        cancelled = self.tasks.cancel("task-1")
        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        with self.assertRaises(StaleLeaseError):
            self.tasks.complete(lease, success=True, outcome_hash="late")

    def test_generic_transition_cannot_orphan_active_attempt(self):
        self.tasks.submit(self.request(), idempotency_key="active")
        self.tasks.prepare("task-1")
        lease = self.tasks.claim("task-1", self.plan(), owner_id="worker")
        with self.assertRaises(InvalidTransition):
            self.tasks.transition("task-1", TaskState.WAITING, reason_code="bypass")
        self.assertEqual(self.tasks.get("task-1").active_attempt_id, lease.attempt_id)

    def test_restart_reconstructs_unfinished_task_and_recovers_expired_lease(self):
        self.tasks.submit(self.request(), idempotency_key="restart", max_attempts=2, now=10)
        self.tasks.prepare("task-1", now=10)
        lease = self.tasks.claim("task-1", self.plan(), owner_id="dead-worker", lease_seconds=5, now=10)
        reopened = Store(self.path)
        tasks = TaskRepository(reopened, EventBus(reopened))
        self.assertEqual(tasks.get("task-1").active_attempt_id, lease.attempt_id)
        self.assertEqual(tasks.recover_expired(now=16), 1)
        self.assertEqual(tasks.get("task-1").state, TaskState.RECOVERING)


if __name__ == "__main__":
    unittest.main()
