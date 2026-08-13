"""Fail-closed sandbox capability and authenticated provenance tests."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from dataclasses import replace
import hashlib

from harness2.config import HarnessConfig
from harness2.kernel.event_bus import EventBus
from harness2.kernel.execution_state import ExecutionStateRepository
from harness2.kernel.task_types import default_task_types
from harness2.kernel.tasks import TaskRepository
from harness2.sandbox import (
    DisabledSandboxBackend, IsolationLevel, SandboxCapabilities,
    SandboxError, SandboxPolicy, SandboxResult, SandboxUnavailable,
)
from harness2.skills.provenance import (
    LocalHMACProducerVerifier, ProvenanceRepository, SignedStatement,
    VerificationStatus,
)
from harness2.storage import LocalAuthenticatedStorage
from harness2.store import Store
from harness2.skills import SkillEvidence


class SandboxPolicyTests(unittest.TestCase):
    def setUp(self):
        self.descriptor = default_task_types().require("skill.execute/v1")

    def test_disabled_and_process_only_backends_fail_production_closed(self):
        disabled = DisabledSandboxBackend()
        decision = SandboxPolicy.authorize(self.descriptor, disabled.probe(), production=True)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "sandbox_os_isolation_required")
        process = SandboxCapabilities(
            "test", IsolationLevel.TEST_PROCESS, True, True, True, True, False, True,
        )
        self.assertFalse(SandboxPolicy.authorize(
            self.descriptor, process, production=True,
        ).allowed)
        with self.assertRaises(SandboxUnavailable):
            disabled.execute(None)  # type: ignore[arg-type]

    def test_os_backend_missing_network_denial_is_rejected(self):
        capabilities = SandboxCapabilities(
            "partial", IsolationLevel.OS_SANDBOX,
            filesystem_containment=True, network_denied=False,
            credential_isolation=True, process_containment=True,
            syscall_restriction=True, resource_limits_enforced=True,
        )
        decision = SandboxPolicy.authorize(self.descriptor, capabilities, production=True)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "sandbox_capability_insufficient")

    def test_result_hash_and_fenced_identity_are_enforced(self):
        output = b"answer"
        value = SandboxResult(
            "execution", "task", "attempt", 1, "a" * 64, "backend",
            "succeeded", 0, output, hashlib.sha256(output).hexdigest(),
            1.0, 2.0, IsolationLevel.OS_SANDBOX,
        )
        self.assertEqual(value.task_id, "task")
        with self.assertRaises(SandboxError):
            replace(value, output_sha256="0" * 64)


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.config = HarnessConfig(state_root=os.path.join(self.tmp.name, "state"))
        self.config.ensure()
        self.store = Store(self.config.database_path)
        events = EventBus(self.store)
        objects = LocalAuthenticatedStorage(
            self.config.object_store_root, self.config.object_store_key,
            openssl_bin=self.config.openssl_bin,
        )
        state = ExecutionStateRepository(
            self.store, events, TaskRepository(self.store, events), objects,
            default_task_types(),
        )
        self.verifier = LocalHMACProducerVerifier(
            "sandbox:local", "key-1", b"k" * 32,
        )
        self.state, self.events = state, events
        self.repository = ProvenanceRepository(
            self.store, events, state, (self.verifier,),
        )

    @staticmethod
    def statement(**overrides):
        values = {
            "subject_kind": "skill_manifest", "subject_id": "example@1",
            "subject_sha256": "a" * 64, "evidence_kind": "sandbox",
            "artifact_sha256": "b" * 64, "issued_at": 100.0,
            "expires_at": 1000.0, "nonce": "nonce-1",
            "producer_id": "sandbox:local", "key_id": "key-1",
        }
        values.update(overrides)
        return SignedStatement(**values)

    def test_signed_observation_survives_reopen_and_binds_exact_subject(self):
        signed = self.verifier.sign(self.statement())
        verification = self.verifier.verify(signed, now=200)
        self.assertEqual(verification.status, VerificationStatus.VERIFIED)
        recorded = self.repository.observe(signed, now=200)
        loaded = self.repository.latest_verified(
            subject_kind="skill_manifest", subject_id="example@1",
            subject_sha256="a" * 64, evidence_kind="sandbox", at=200,
        )
        self.assertEqual(loaded.observation_id, recorded.observation_id)
        self.assertIsNone(self.repository.latest_verified(
            subject_kind="skill_manifest", subject_id="other@1",
            subject_sha256="a" * 64, evidence_kind="sandbox", at=200,
        ))
        evidence = SkillEvidence(
            "sandbox-1", "sandbox", "b" * 64, "now",
            subject_hash="a" * 64, producer_id="sandbox:local",
        )
        verified = self.repository.verified_skill_evidence(
            evidence, skill_id="example", version="1", at=200,
        )
        self.assertTrue(self.repository.validate_skill_evidence(
            verified, skill_id="example", version="1", at=200,
        ))

    def test_tamper_wrong_key_and_expiry_fail(self):
        signed = self.verifier.sign(self.statement())
        tampered = replace(signed, artifact_sha256="c" * 64)
        self.assertEqual(
            self.verifier.verify(tampered, now=200).status,
            VerificationStatus.INVALID,
        )
        wrong = replace(signed, key_id="other")
        self.assertEqual(
            self.verifier.verify(wrong, now=200).status,
            VerificationStatus.UNKNOWN_PRODUCER,
        )
        self.assertEqual(
            self.verifier.verify(signed, now=1001).status,
            VerificationStatus.EXPIRED,
        )

    def test_test_only_provenance_is_not_production_verified(self):
        verifier = LocalHMACProducerVerifier(
            "sandbox:local", "key-1", b"k" * 32, test_only=True,
        )
        repository = ProvenanceRepository(
            self.store, self.events, self.state, (verifier,),
        )
        verification = verifier.verify(verifier.sign(self.statement()), now=200)
        self.assertEqual(verification.status, VerificationStatus.TEST_ONLY)
        repository.observe(verification.statement, now=200)
        self.assertIsNone(repository.latest_verified(
            subject_kind="skill_manifest", subject_id="example@1",
            subject_sha256="a" * 64, evidence_kind="sandbox", at=200,
            production=True,
        ))
        self.assertIsNotNone(repository.latest_verified(
            subject_kind="skill_manifest", subject_id="example@1",
            subject_sha256="a" * 64, evidence_kind="sandbox", at=200,
            production=False,
        ))

    def test_database_verified_flag_cannot_create_trust(self):
        unknown = self.statement(producer_id="attacker", key_id="attacker-key")
        attacker = LocalHMACProducerVerifier(
            "attacker", "attacker-key", b"a" * 32,
        )
        signed = attacker.sign(unknown)
        repository = ProvenanceRepository(
            self.store, self.events, self.state, (attacker,),
        )
        observation = repository.observe(signed, now=200)
        with self.store.connect() as con:
            con.execute(
                "UPDATE kernel_provenance_observations SET producer_identity=?,"
                "verification_status='verified' WHERE observation_id=?",
                ("sandbox:local", observation.observation_id),
            )
        with self.assertRaises(Exception):
            repository.latest_verified(
                subject_kind="skill_manifest", subject_id="example@1",
                subject_sha256="a" * 64, evidence_kind="sandbox", at=200,
            )


if __name__ == "__main__":
    unittest.main()
