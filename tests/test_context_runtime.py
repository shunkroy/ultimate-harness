"""Executable-context compiler, package, runtime and queue tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from harness2.config import HarnessConfig
from harness2.context import CompileError, ContextCompiler, ContextPackage, ContextRuntime, PackageError
from harness2.context.jobs import ContextJobManager
from harness2.kernel.event_bus import EventBus
from harness2.kernel.execution_state import ExecutionStateRepository
from harness2.kernel.task_types import default_task_types
from harness2.kernel.tasks import TaskRepository
from harness2.storage import LocalAuthenticatedStorage
from harness2.store import Store


SOURCE = """# Concepts
Context Program: Structured knowledge with operations and provenance.
Provenance: A trace from an output to its source.

# Rules
Generated claims require supporting evidence.

# Procedures
Compile: parse -> structure -> validate -> package

# Operations
query(topic: string) -> EvidenceSet
transform(text: string, mode: string) -> Text
generate(topic: string) -> EvidenceBrief
"""


class ContextCompilerTests(unittest.TestCase):
    def test_compile_extracts_structure_and_authorized_operations(self):
        compiled = ContextCompiler().compile_text(SOURCE, name="Executable Context")
        self.assertTrue(compiled.ir.context_id.startswith("ctx-executable-context-"))
        self.assertEqual([item.name for item in compiled.ir.operations], ["generate", "query", "transform"])
        self.assertEqual(compiled.ir.permissions, ())
        self.assertEqual(compiled.ir.rules[0].line_start, 6)

    def test_source_cannot_grant_an_arbitrary_operation(self):
        malicious = "# Operations\ndelete_files(path: string) -> Text\n"
        with self.assertRaises(CompileError):
            ContextCompiler().compile_text(malicious, name="Malicious")

    def test_operation_contract_cannot_be_weakened(self):
        wrong = "# Operations\ngenerate(prompt: string) -> Text\n"
        with self.assertRaises(CompileError):
            ContextCompiler().compile_text(wrong, name="Wrong")

    def test_compile_bytes_preserves_exact_bytes(self):
        raw = (SOURCE + "\n").encode("utf-8")
        compiled = ContextCompiler().compile_bytes(raw, name="Bytes")
        self.assertEqual(compiled.source_bytes, raw)
        with self.assertRaises(CompileError):
            ContextCompiler().compile_bytes(b"\xff", name="Bad")


class ContextPackageRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        compiled = ContextCompiler().compile_text(SOURCE, name="Executable Context")
        self.package = ContextPackage.write(compiled, os.path.join(self.tmp.name, "package"))
        self.runtime = ContextRuntime()
        self.runtime.load(self.package.root)

    def test_query_returns_source_hash_and_lines(self):
        result = self.runtime.execute(self.package.ir.context_id, "query", {"topic": "provenance"})
        self.assertTrue(result.success)
        self.assertTrue(result.validated)
        self.assertEqual(result.evidence[0]["source_sha256"], self.package.ir.source.sha256)
        self.assertGreater(result.evidence[0]["line_start"], 0)

    def test_transform_is_allowlisted_and_deterministic(self):
        good = self.runtime.execute(
            self.package.ir.context_id, "transform",
            {"text": " a   b ", "mode": "compress_whitespace"},
        )
        self.assertEqual(good.output, "a b")
        bad = self.runtime.execute(
            self.package.ir.context_id, "transform",
            {"text": "x", "mode": "run_shell"},
        )
        self.assertFalse(bad.success)
        self.assertEqual(bad.error_code, "unsupported_transform")

    def test_generate_refuses_unsupported_topic(self):
        result = self.runtime.execute(self.package.ir.context_id, "generate", {"topic": "quantum banana"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "insufficient_evidence")
        self.assertIsNone(result.output)

    def test_generate_creates_evidence_brief_not_raw_prompt_wrapper(self):
        result = self.runtime.execute(self.package.ir.context_id, "generate", {"topic": "context program"})
        self.assertTrue(result.success)
        self.assertTrue(result.output["provenance_required"])
        self.assertTrue(result.evidence)
        self.assertEqual(result.backend, "deterministic.evidence_brief")

    def test_invalid_input_schema_fails_closed(self):
        result = self.runtime.execute(self.package.ir.context_id, "query", {"topic": 3})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "invalid_input")

    def test_package_tamper_is_detected(self):
        with open(os.path.join(self.package.root, "ir.json"), "ab") as fh:
            fh.write(b" ")
        with self.assertRaises(PackageError):
            ContextPackage.load(self.package.root)


class ContextJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.config = HarnessConfig(state_root=os.path.join(self.tmp.name, "state"))
        self.config.ensure()
        self.store = Store(self.config.database_path)
        events = EventBus(self.store)
        tasks = TaskRepository(self.store, events)
        objects = LocalAuthenticatedStorage(
            self.config.object_store_root, self.config.object_store_key,
            openssl_bin=self.config.openssl_bin,
        )
        self.execution_state = ExecutionStateRepository(
            self.store, events, tasks, objects, default_task_types(),
        )
        self.manager = ContextJobManager(
            self.config, self.store, self.execution_state,
        )
        self.source = os.path.join(self.tmp.name, "source.txt")
        with open(self.source, "w", encoding="utf-8") as fh:
            fh.write(SOURCE)

    def test_explicit_job_compiles_one_package(self):
        job_id = self.manager.submit(self.source, name="Queue Context")
        self.assertEqual(self.manager.show(job_id)["status"], "queued")
        result = self.manager.work_once()
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(os.path.isdir(result["result"]["package"]))
        self.assertIsNone(self.manager.work_once())

    def test_job_uses_submission_snapshot_after_source_replacement(self):
        job_id = self.manager.submit(self.source, name="Snapshot Context")
        job = self.manager.show(job_id)
        self.assertEqual(job["schema"], "harness.context-job/v2")
        self.assertIn("snapshot_id", job)
        self.assertNotIn("source", job)
        with open(self.source, "w", encoding="utf-8") as stream:
            stream.write("# Concepts\nReplaced: source\n")
        restarted = ContextJobManager(
            self.config, Store(self.config.database_path), self.execution_state,
        )
        result = restarted.work_once()
        self.assertEqual(result["status"], "succeeded")
        package = ContextPackage.load(result["result"]["package"])
        self.assertEqual(package.source_text, SOURCE)

    def test_legacy_queued_job_fails_without_reopening_path(self):
        job_id = "a" * 32
        value = {
            "schema": "harness.context-job/v1", "id": job_id,
            "status": "queued", "created_at": 1, "updated_at": 1,
            "source": self.source, "name": "Legacy", "version": "0.1.0",
            "attempt": 0, "result": None, "error_code": None,
        }
        from harness2.security import atomic_write_json
        atomic_write_json(self.manager._path(job_id), value)
        os.unlink(self.source)
        result = self.manager.work_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "legacy_source_snapshot_missing")

    def test_snapshot_substitution_and_object_tamper_publish_no_package(self):
        first = self.manager.submit(self.source, name="First")
        second = self.manager.submit(self.source, name="Second")
        first_job = self.manager.show(first)
        second_path = self.manager._path(second)
        with open(second_path, "r", encoding="utf-8") as stream:
            second_job = json.load(stream)
        second_job["snapshot_id"] = first_job["snapshot_id"]
        from harness2.security import atomic_write_json
        atomic_write_json(second_path, second_job)
        self.manager.work_once()  # first is valid and ordered first
        result = self.manager.work_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "context_snapshot_binding_mismatch")
        self.assertIsNone(result["result"])

        third = self.manager.submit(self.source, name="Tampered")
        third_job = self.manager.show(third)
        snapshot = self.execution_state.source_snapshot(third_job["snapshot_id"])
        path = self.execution_state.objects._path(snapshot.payload.object_key)
        with open(path, "r+b") as stream:
            stream.seek(-1, os.SEEK_END)
            byte = stream.read(1)
            stream.seek(-1, os.SEEK_END)
            stream.write(bytes((byte[0] ^ 1,)))
        result = self.manager.work_once()
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["result"])


if __name__ == "__main__":
    unittest.main()
