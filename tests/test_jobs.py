"""Encrypted durable job queue and service support tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import Mock

from harness2.config import HarnessConfig
from harness2.crypto import CryptoError, decrypt, encrypt, load_or_create_key
from harness2.jobs import JobManager
from harness2.models import EngineResult, RoutingDecision, RunRequest
from harness2.service import rotate
from harness2.store import Store


class FakeOrchestrator:
    def __init__(self, success=True):
        self.success = success

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
        second = manager.work_once()
        self.assertEqual(second["status"], "dead")

    def test_cancel_removes_payload(self):
        manager = self.manager()
        job_id = manager.submit(RunRequest("secret"))
        with self.store.connect() as con:
            path = con.execute("SELECT payload_path FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertTrue(manager.cancel(job_id))
        self.assertFalse(os.path.exists(path))


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
