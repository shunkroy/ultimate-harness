"""Encrypted durable job queue and service support tests."""

from __future__ import annotations

import os
import tempfile
import unittest
import time
from unittest.mock import Mock

from harness2.config import HarnessConfig
from harness2.crypto import CryptoError, decrypt, encrypt, load_or_create_key
from harness2.jobs import JobManager
from harness2.kernel.tasks import StaleLeaseError, TaskState
from harness2.models import EngineResult, RoutingDecision, RunRequest
from harness2.policy import canonicalize_request
from harness2.service import rotate
from harness2.store import Store


class FakeOrchestrator:
    def __init__(self, success=True):
        self.success = success

    def prepare(self, request):
        prepared = canonicalize_request(request)
        engine = prepared.engine if prepared.engine != "auto" else "opencode"
        return prepared, RoutingDecision(engine, "kiteretsu", "free", "test")

    def run(self, request):
        result = EngineResult(
            request.engine if request.engine != "auto" else "opencode", self.success,
            text="answer" if self.success else "", error=None if self.success else "failed",
            error_code=None if self.success else "process_error", exit_code=0 if self.success else 1,
        )
        return RoutingDecision(result.engine, "kiteretsu", "free", "test"), result, "run1"


class CryptoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def test_roundtrip_and_tamper(self):
        key = b"x" * 32
        data = encrypt(key, "secret 🎉".encode())
        self.assertNotIn(b"secret", data)
        self.assertEqual(decrypt(key, data), "secret 🎉".encode())
        bad = bytearray(data)
        bad[-1] ^= 1
        with self.assertRaises(CryptoError):
            decrypt(key, bytes(bad))

    def test_key_private_and_stable(self):
        path = os.path.join(self.tmp.name, "state", "job.key")
        one = load_or_create_key(path)
        two = load_or_create_key(path)
        self.assertEqual(one, two)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.config = HarnessConfig(state_root=os.path.join(self.tmp.name, "state"))
        self.config.ensure()
        self.store = Store(self.config.database_path)

    def manager(self, success=True):
        return JobManager(self.config, self.store, FakeOrchestrator(success))

    def test_submit_encrypted_and_list_no_prompt(self):
        manager = self.manager()
        prompt = "ultra private prompt"
        job_id = manager.submit(RunRequest(prompt, engine="opencode"))
        with self.store.connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        with open(row["payload_path"], "rb") as fh:
            envelope = fh.read()
        self.assertNotIn(prompt.encode(), envelope)
        self.assertNotIn(prompt, repr(manager.list()))
        self.assertNotIn(prompt, repr(manager.show(job_id)))

    def test_submit_persists_canonical_cwd_and_identity(self):
        manager = self.manager()
        workspace = os.path.join(self.tmp.name, "workspace")
        os.mkdir(workspace)
        job_id = manager.submit(RunRequest("secret", engine="opencode", cwd=workspace + "/../workspace"))
        claimed = manager.claim()
        loaded = manager._load_request(claimed)
        expected = os.path.realpath(workspace)
        self.assertEqual(loaded.cwd, expected)
        self.assertEqual(loaded.cwd_identity, (os.stat(expected).st_dev, os.stat(expected).st_ino))
        with self.store.connect() as con:
            row = con.execute("SELECT cwd FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row[0], expected)

    def test_default_cwd_is_materialized_before_persistence(self):
        manager = self.manager()
        job_id = manager.submit(RunRequest("secret", engine="opencode"))
        shown = manager.show(job_id)
        self.assertEqual(shown["cwd"], os.path.realpath(os.getcwd()))

    def test_cwd_projection_mismatch_is_rejected(self):
        manager = self.manager()
        job_id = manager.submit(RunRequest("secret", engine="opencode"))
        with self.store.connect() as con:
            con.execute("UPDATE jobs SET cwd=? WHERE id=?", (self.tmp.name, job_id))
        claimed = manager.claim()
        with self.assertRaisesRegex(RuntimeError, "authority projections"):
            manager._load_request(claimed)

    def test_work_success_deletes_payload(self):
        manager = self.manager(True)
        job_id = manager.submit(RunRequest("secret", engine="opencode"))
        with self.store.connect() as con:
            path = con.execute("SELECT payload_path FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        result = manager.work_once()
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(os.path.exists(path))

    def test_failure_retries_then_dead(self):
        manager = self.manager(False)
        job_id = manager.submit(RunRequest("secret", engine="opencode"), max_attempts=2)
        first = manager.work_once()
        self.assertEqual(first["status"], "retry")
        with self.store.connect() as con:
            con.execute("UPDATE jobs SET next_run_at=0 WHERE id=?", (job_id,))
            con.execute(
                "UPDATE kernel_tasks SET next_run_at=0 WHERE task_id=("
                "SELECT task_id FROM kernel_legacy_job_tasks WHERE job_id=?)", (job_id,),
            )
        second = manager.work_once()
        self.assertEqual(second["status"], "dead")

    def test_cancel_removes_payload(self):
        manager = self.manager()
        job_id = manager.submit(RunRequest("secret"))
        with self.store.connect() as con:
            path = con.execute("SELECT payload_path FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertTrue(manager.cancel(job_id))
        self.assertFalse(os.path.exists(path))

    def test_payload_paths_relocate_only_within_jobs_namespace(self):
        manager = self.manager()
        expected = os.path.join(manager.jobs_dir, "a" * 32 + ".bin")
        self.assertEqual(
            manager._payload_file("C:\\old-phone\\state\\jobs\\" + "a" * 32 + ".bin", "a" * 32),
            os.path.realpath(expected),
        )
        with self.assertRaises(RuntimeError):
            manager._payload_file(os.path.join(self.tmp.name, "outside.bin"), "a" * 32)

    def test_job_cannot_delete_another_jobs_payload(self):
        manager = self.manager()
        first = manager.submit(RunRequest("first"))
        second = manager.submit(RunRequest("second"))
        second_path = os.path.join(manager.jobs_dir, second + ".bin")
        with self.store.connect() as con:
            con.execute("UPDATE jobs SET payload_path=? WHERE id=?", (second_path, first))
        with self.assertRaises(RuntimeError):
            manager.cancel(first)
        self.assertTrue(os.path.isfile(second_path))

    def test_tampered_payload_path_cannot_delete_outside_jobs(self):
        manager = self.manager()
        job_id = manager.submit(RunRequest("secret"))
        sentinel = os.path.join(self.tmp.name, "sentinel.bin")
        with open(sentinel, "wb") as stream:
            stream.write(b"keep")
        with self.store.connect() as con:
            con.execute("UPDATE jobs SET payload_path=? WHERE id=?", (sentinel, job_id))
        with self.assertRaises(RuntimeError):
            manager.cancel(job_id)
        self.assertTrue(os.path.isfile(sentinel))
        self.assertEqual(manager.show(job_id)["status"], "queued")

    def test_submit_and_completion_share_typed_fenced_state(self):
        manager = self.manager(True)
        job_id = manager.submit(RunRequest("secret", retries=4, engine="opencode"))
        with self.store.connect() as con:
            mapping = con.execute(
                "SELECT * FROM kernel_legacy_job_tasks WHERE job_id=?", (job_id,),
            ).fetchone()
            self.assertIsNotNone(mapping)
        claimed = manager.claim()
        self.assertEqual(manager._load_request(claimed).retries, 4)
        lease = claimed["typed_lease"]
        with self.store.connect() as con:
            con.execute(
                "UPDATE kernel_task_attempts SET lease_until=? WHERE attempt_id=?",
                (time.time() - 1, lease.attempt_id),
            )
            con.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job_id))
        self.assertEqual(manager._recover_stale(), 1)
        recovered = manager.tasks.get(lease.task_id)
        self.assertEqual(recovered.state, TaskState.FAILED)
        self.assertEqual(manager.show(job_id)["status"], "dead")
        with self.assertRaises(StaleLeaseError):
            manager.tasks.complete(
                lease, success=True, outcome_hash="a" * 64, now=time.time(),
            )

    def test_purge_mapped_job_preserves_typed_history(self):
        manager = self.manager()
        job_id = manager.submit(RunRequest("secret"))
        with self.store.connect() as con:
            task_id = con.execute(
                "SELECT task_id FROM kernel_legacy_job_tasks WHERE job_id=?", (job_id,),
            ).fetchone()[0]
        self.assertTrue(manager.purge(job_id))
        self.assertIsNotNone(manager.tasks.get(task_id))
        with self.store.connect() as con:
            self.assertIsNone(con.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone())

    def test_unmapped_preupgrade_job_is_quarantined_not_executed(self):
        manager = self.manager()
        job_id = "f" * 32
        path = os.path.join(manager.jobs_dir, job_id + ".bin")
        with open(path, "wb") as stream:
            stream.write(b"untrusted legacy envelope")
        now = time.time()
        with self.store.connect() as con:
            con.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, now, now, "queued", "a" * 64, path, "opencode",
                    None, None, None, 240, None, 0, 0, 0, 1, 0, 3, now,
                    None, None, None, None, None,
                ),
            )
        result = manager.work_once()
        self.assertEqual(result["status"], "dead")
        self.assertEqual(result["error_code"], "legacy_payload_not_migrated")
        self.assertEqual(manager.show(job_id)["attempt"], 0)


class RotateTests(unittest.TestCase):
    def test_rotate(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "service.log")
            with open(path, "wb") as fh:
                fh.write(b"x" * 20)
            self.assertTrue(rotate(path, max_bytes=10, keep=2))
            self.assertTrue(os.path.exists(path + ".1"))


if __name__ == "__main__":
    unittest.main()
