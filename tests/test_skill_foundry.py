"""Skill contract and evidence-gated promotion architecture tests."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from harness2.skills import (
    SkillCapability,
    SkillDescriptor,
    SkillEvidence,
    SkillFoundry,
    SkillManifest,
    SkillPromotionDenied,
    SkillState,
    SkillTest,
    VerifiedSkillEvidence,
)
from harness2.skills.contracts import SkillContractError


def manifest(**overrides):
    values = {
        "skill_id": "python-debug",
        "version": "1",
        "created_by": "provider:example",
        "source_hash": "a" * 64,
        "capabilities": (SkillCapability("code.debug.python"),),
    }
    values.update(overrides)
    return SkillManifest(**values)


def verified(items):
    return tuple(
        VerifiedSkillEvidence(
            item, "verified", "hmac-sha256-local/v1", item.producer_id,
            "local", (str(index + 1) * 64)[:64], item.artifact_hash,
            item.kind, None, False,
        )
        for index, item in enumerate(items)
    )


class TrustedProvenanceStub:
    def __init__(self, values):
        self.values = set(item.statement_hash for item in values)

    def validate_skill_evidence(self, item, **_):
        return item.statement_hash in self.values


class SkillContractTests(unittest.TestCase):
    def test_candidate_cannot_self_grant_permissions_or_escape_path(self):
        with self.assertRaises(SkillContractError):
            manifest(permissions=("network",))
        with self.assertRaises(SkillContractError):
            manifest(entrypoint="../core.py")

    def test_manifest_is_deterministic_and_content_addressed(self):
        one = manifest()
        two = manifest()
        self.assertEqual(one.canonical_json(), two.canonical_json())
        self.assertEqual(one.manifest_hash, two.manifest_hash)

    def test_generated_skill_cannot_promote_itself_without_evidence(self):
        descriptor = SkillDescriptor(manifest(), SkillState.DRAFT)
        with self.assertRaises(SkillPromotionDenied):
            SkillFoundry.promote(descriptor, SkillState.ACTIVE)

    def test_activation_requires_sandbox_tests_and_independent_approval(self):
        value = manifest()
        subject = value.manifest_hash
        evidence = (
            SkillEvidence("sandbox-1", "sandbox", "b" * 64, "2026-08-13T00:00:00Z", subject_hash=subject, producer_id="sandbox:local"),
            SkillEvidence("tests-1", "test", "c" * 64, "2026-08-13T00:00:00Z", subject_hash=subject, producer_id="test:local"),
            SkillEvidence("approval-1", "approval", "d" * 64, "2026-08-13T00:00:00Z", subject_hash=subject, producer_id="governor:user"),
        )
        tested = (SkillTest("unit", True, "e" * 64),)
        descriptor = SkillDescriptor(value, SkillState.APPROVED, evidence, tested, activated=False)
        values = verified(evidence)
        active = SkillFoundry.promote(
            descriptor, SkillState.ACTIVE, verified_evidence=values,
            provenance=TrustedProvenanceStub(values),
        )
        self.assertEqual(active.state, SkillState.ACTIVE)
        self.assertTrue(active.activated)

    def test_windows_absolute_entrypoint_is_rejected(self):
        with self.assertRaises(SkillContractError):
            manifest(entrypoint=r"C:\outside.py")

    def test_forged_or_self_approved_evidence_cannot_activate(self):
        value = manifest()
        evidence = (
            SkillEvidence("sandbox", "sandbox", "b" * 64, "now", subject_hash=value.manifest_hash, producer_id="sandbox"),
            SkillEvidence("test", "test", "c" * 64, "now", subject_hash=value.manifest_hash, producer_id="test"),
            SkillEvidence("approval", "approval", "d" * 64, "now", subject_hash=value.manifest_hash, producer_id=value.created_by),
        )
        descriptor = SkillDescriptor(
            value, SkillState.APPROVED, evidence,
            (SkillTest("unit", True, "e" * 64),), activated=False,
        )
        with self.assertRaises(SkillPromotionDenied):
            SkillFoundry.promote(descriptor, SkillState.ACTIVE)

    def test_descriptive_producer_labels_are_not_authenticated(self):
        value = manifest()
        evidence = (
            SkillEvidence("sandbox", "sandbox", "b" * 64, "now", subject_hash=value.manifest_hash, producer_id="sandbox"),
            SkillEvidence("test", "test", "c" * 64, "now", subject_hash=value.manifest_hash, producer_id="test"),
            SkillEvidence("approval", "approval", "d" * 64, "now", subject_hash=value.manifest_hash, producer_id="governor"),
        )
        descriptor = SkillDescriptor(
            value, SkillState.APPROVED, evidence,
            (SkillTest("unit", True, "e" * 64),), activated=False,
        )
        with self.assertRaises(SkillPromotionDenied):
            SkillFoundry.promote(descriptor, SkillState.ACTIVE)

    def test_skill_foundry_does_not_import_candidate_modules(self):
        root = Path(__file__).resolve().parents[1] / "harness2" / "skills"
        forbidden = []
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"exec", "eval", "__import__"}:
                    forbidden.append(f"{path.name}:{node.lineno}")
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
