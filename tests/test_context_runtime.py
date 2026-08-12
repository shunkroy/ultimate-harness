"""Executable-context compiler, package, runtime and queue tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from harness2.config import HarnessConfig
from harness2.context import CompileError, ContextCompiler, ContextPackage, ContextRuntime, PackageError
from harness2.context.jobs import ContextJobManager
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
        self.manager = ContextJobManager(self.config, self.store)
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


if __name__ == "__main__":
    unittest.main()
