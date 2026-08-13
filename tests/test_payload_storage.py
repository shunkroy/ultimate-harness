"""Immutable task payload, authenticated object and checkpoint tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace

from harness2.bootstrap import ApplicationRuntime
from harness2.config import HarnessConfig
from harness2.kernel.contracts import ExecutionPlan
from harness2.kernel.event_bus import EventBus
from harness2.kernel.execution_state import ExecutionStateRepository
from harness2.kernel.payloads import PayloadError, PayloadIntegrityError, TaskPayload
from harness2.kernel.task_types import default_task_types
from harness2.kernel.tasks import StaleLeaseError, TaskRepository
from harness2.storage import LocalAuthenticatedStorage
from harness2.store import Store


class ExecutionStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.config = HarnessConfig(state_root=os.path.join(self.temp.name, "state"))
        self.config.ensure()
        self.store = Store(self.config.database_path)
        self.events = EventBus(self.store)
        self.tasks = TaskRepository(self.store, self.events)
        self.objects = LocalAuthenticatedStorage(
            self.config.object_store_root, self.config.object_store_key,
            openssl_bin=self.config.openssl_bin,
        )
        self.state = ExecutionStateRepository(
            self.store, self.events, self.tasks, self.objects, default_task_types(),
        )

    @staticmethod
    def payload(inputs=None):
        return TaskPayload(
            "execution.generic/v1", "immutable objective",
            inputs or {"items": [1, 2]},
            {"required_capabilities": []}, {"timeout": 30},
        )

    @staticmethod
    def plan(task_id):
        return ExecutionPlan(
            "plan", task_id, "harness", (), ("execute",), ("verify",), "test",
        )

    def test_original_mutation_cannot_change_bound_payload(self):
        source = {"items": [1, 2]}
        payload = self.payload(source)
        task, reference = self.state.create_task(
            payload, task_id="immutable", idempotency_key="immutable",
        )
        source["items"].append(999)
        with self.assertRaises(AttributeError):
            payload.inputs["items"].append(888)
        loaded = self.state.load_task_payload(task.task_id)
        self.assertEqual(loaded.as_dict()["inputs"], {"items": [1, 2]})
        self.assertEqual(reference.content_sha256, self.state.payload_reference(task.task_id).content_sha256)
        with open(self.store.path, "rb") as stream:
            self.assertNotIn(b"immutable objective", stream.read())

    def test_key_order_is_canonical_but_schema_and_purpose_are_domain_separated(self):
        one = TaskPayload("execution.generic/v1", "x", {"a": 1, "b": 2})
        two = TaskPayload("execution.generic/v1", "x", {"b": 2, "a": 1})
        self.assertEqual(one.canonical, two.canonical)
        self.assertEqual(one.payload_id, two.payload_id)
        ref_a = self.objects.put(one.canonical, schema_id=one.schema_id, purpose="a", binding={"x": 1})
        ref_b = self.objects.put(one.canonical, schema_id=one.schema_id, purpose="b", binding={"x": 1})
        self.assertNotEqual(ref_a.content_sha256, ref_b.content_sha256)

    def test_authenticated_put_is_idempotent(self):
        first = self.objects.put(
            b"same", schema_id="test.object/1", purpose="test.input",
            binding={"task_id": "one"},
        )
        second = self.objects.put(
            b"same", schema_id="test.object/1", purpose="test.input",
            binding={"task_id": "one"},
        )
        self.assertEqual(first, second)

    def test_arbitrary_python_objects_and_unknown_task_types_fail_closed(self):
        with self.assertRaises(PayloadError):
            TaskPayload("execution.generic/v1", "x", {"bad": object()})
        with self.assertRaises(PayloadError):
            self.state.create_task(TaskPayload("forged/v1", "x"), task_id="forged")

    def test_contextual_substitution_and_ciphertext_tampering_fail(self):
        payload = self.payload()
        _, reference = self.state.create_task(payload, task_id="one")
        with self.assertRaises(PayloadIntegrityError):
            self.objects.get(reference, binding={
                "task_id": "two", "role": "input", "task_type": payload.task_type,
            })
        path = self.objects._path(reference.object_key)
        with open(path, "r+b") as stream:
            stream.seek(-1, os.SEEK_END)
            byte = stream.read(1)
            stream.seek(-1, os.SEEK_END)
            stream.write(bytes((byte[0] ^ 1,)))
        with self.assertRaises(PayloadIntegrityError):
            self.state.load_task_payload("one")

    def test_checkpoint_is_fenced_persistent_and_tamper_detected(self):
        task, _ = self.state.create_task(self.payload(), task_id="checkpoint", max_attempts=2)
        started = task.created_at + 1
        self.tasks.prepare(task.task_id, now=started)
        lease_a = self.tasks.claim(
            task.task_id, self.plan(task.task_id), owner_id="a",
            lease_seconds=1, now=started,
        )
        first = self.state.checkpoint(
            lease_a, {"offset": 3}, workflow_step="read", now=started + 0.5,
        )
        self.assertEqual(first.sequence, 1)
        reopened = ExecutionStateRepository(
            Store(self.store.path), EventBus(Store(self.store.path)),
            TaskRepository(Store(self.store.path), EventBus(Store(self.store.path))),
            self.objects, default_task_types(),
        )
        self.assertEqual(reopened.latest_checkpoint(task.task_id).checkpoint_id, first.checkpoint_id)
        self.assertEqual(self.tasks.recover_expired(now=started + 2), 1)
        lease_b = self.tasks.claim(
            task.task_id, self.plan(task.task_id), owner_id="b", now=started + 2,
        )
        second = self.state.checkpoint(
            lease_b, {"offset": 4}, workflow_step="read", now=started + 2,
        )
        self.tasks.complete(
            lease_b, success=True, outcome_hash="b" * 64, now=started + 2,
        )
        with self.assertRaises(StaleLeaseError):
            self.state.checkpoint(
                lease_a, {"offset": 5}, workflow_step="late", now=started + 2,
            )
        self.assertEqual(self.state.latest_checkpoint(task.task_id).checkpoint_id, second.checkpoint_id)
        with self.store.connect() as con:
            con.execute(
                "UPDATE kernel_task_checkpoints SET integrity_hash=? WHERE checkpoint_id=?",
                ("0" * 64, second.checkpoint_id),
            )
        with self.assertRaises(PayloadIntegrityError):
            self.state.latest_checkpoint(task.task_id)

    def test_checkpoint_ancestor_deletion_breaks_chain(self):
        task, _ = self.state.create_task(self.payload(), task_id="checkpoint-chain")
        started = task.created_at + 1
        self.tasks.prepare(task.task_id, now=started)
        lease = self.tasks.claim(
            task.task_id, self.plan(task.task_id), owner_id="worker", now=started,
        )
        first = self.state.checkpoint(
            lease, {"offset": 1}, workflow_step="one", now=started,
        )
        self.state.checkpoint(
            lease, {"offset": 2}, workflow_step="two", now=started,
        )
        with self.store.connect() as con:
            con.execute(
                "DELETE FROM kernel_task_checkpoints WHERE checkpoint_id=?", (first.checkpoint_id,),
            )
        with self.assertRaises(PayloadIntegrityError):
            self.state.latest_checkpoint(task.task_id)

    def test_descriptor_drift_is_rejected_at_load(self):
        task, _ = self.state.create_task(self.payload(), task_id="descriptor-drift")
        with self.store.connect() as con:
            con.execute(
                "UPDATE kernel_task_type_snapshots SET descriptor_sha256=? WHERE task_id=?",
                ("0" * 64, task.task_id),
            )
        with self.assertRaises(PayloadIntegrityError):
            self.state.load_task_payload(task.task_id)


if __name__ == "__main__":
    unittest.main()
