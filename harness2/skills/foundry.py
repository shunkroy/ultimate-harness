"""Skill lifecycle policy foundation; execution sandbox is intentionally absent."""

from __future__ import annotations

from typing import Mapping

from .contracts import SkillDescriptor, SkillPromotionDecision, SkillState


class SkillNotFound(KeyError):
    pass


class SkillPromotionDenied(RuntimeError):
    pass


_TRANSITIONS: Mapping[SkillState, frozenset[SkillState]] = {
    SkillState.DISCOVERED: frozenset({SkillState.DRAFT, SkillState.ARCHIVED}),
    SkillState.DRAFT: frozenset({SkillState.SANDBOXED, SkillState.ARCHIVED}),
    SkillState.SANDBOXED: frozenset({SkillState.TESTED, SkillState.DRAFT, SkillState.ARCHIVED}),
    SkillState.TESTED: frozenset({SkillState.BENCHMARKED, SkillState.APPROVED, SkillState.DRAFT, SkillState.ARCHIVED}),
    SkillState.BENCHMARKED: frozenset({SkillState.APPROVED, SkillState.DRAFT, SkillState.ARCHIVED}),
    SkillState.APPROVED: frozenset({SkillState.ACTIVE, SkillState.DEPRECATED}),
    SkillState.ACTIVE: frozenset({SkillState.DEGRADED, SkillState.DEPRECATED}),
    SkillState.DEGRADED: frozenset({SkillState.ACTIVE, SkillState.DEPRECATED}),
    SkillState.DEPRECATED: frozenset({SkillState.ARCHIVED}),
    SkillState.ARCHIVED: frozenset(),
}


class SkillFoundry:
    """Evaluate evidence gates without importing or executing candidate code."""

    @staticmethod
    def evaluate(descriptor: SkillDescriptor, target: SkillState) -> SkillPromotionDecision:
        reasons: list[str] = []
        if target not in _TRANSITIONS[descriptor.state]:
            reasons.append(f"transition {descriptor.state.value}->{target.value} is not allowed")
        bound_evidence = tuple(
            item for item in descriptor.evidence
            if item.subject_hash == descriptor.manifest.manifest_hash
        )
        if len(bound_evidence) != len(descriptor.evidence):
            reasons.append("all evidence must bind to the exact manifest hash")
        passed = sum(1 for test in descriptor.tests if test.passed)
        failed = sum(1 for test in descriptor.tests if not test.passed)
        test_evidence = tuple(item.evidence_id for item in bound_evidence if item.kind == "test")
        benchmark_evidence = tuple(item.evidence_id for item in bound_evidence if item.kind == "benchmark")
        approval_evidence = tuple(
            item.evidence_id for item in bound_evidence
            if item.kind == "approval" and item.producer_id != descriptor.manifest.created_by
        )
        sandbox_evidence = tuple(item.evidence_id for item in bound_evidence if item.kind == "sandbox")
        if target in {SkillState.SANDBOXED, SkillState.TESTED, SkillState.BENCHMARKED, SkillState.APPROVED, SkillState.ACTIVE} and not sandbox_evidence:
            reasons.append("sandbox evidence is required")
        if target in {SkillState.TESTED, SkillState.BENCHMARKED, SkillState.APPROVED, SkillState.ACTIVE}:
            if passed < 1 or failed:
                reasons.append("passing tests with zero failures are required")
            if not test_evidence:
                reasons.append("test evidence artifact is required")
        if target == SkillState.BENCHMARKED:
            if descriptor.benchmark_score is None or not benchmark_evidence:
                reasons.append("benchmark score and evidence are required")
        if target in {SkillState.APPROVED, SkillState.ACTIVE} and not approval_evidence:
            reasons.append("independent approval evidence is required")
        if target == SkillState.ACTIVE and descriptor.state != SkillState.APPROVED:
            reasons.append("only approved skills can activate")
        evidence_ids = tuple(item.evidence_id for item in descriptor.evidence)
        return SkillPromotionDecision(
            not reasons, descriptor.state, target, tuple(reasons), evidence_ids,
        )

    @classmethod
    def promote(cls, descriptor: SkillDescriptor, target: SkillState) -> SkillDescriptor:
        decision = cls.evaluate(descriptor, target)
        if not decision.allowed:
            raise SkillPromotionDenied("; ".join(decision.reasons))
        return SkillDescriptor(
            descriptor.manifest, target, descriptor.evidence, descriptor.tests,
            descriptor.benchmark_score, target == SkillState.ACTIVE,
        )
