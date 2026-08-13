"""Empirical scoring and resource-governor foundation tests."""

from __future__ import annotations

import os
import tempfile
import unittest

from harness2.kernel.provider_intelligence import ProviderIntelligence, ProviderObservation
from harness2.kernel.resources import (
    ResourceAction,
    ResourceGovernor,
    ResourceLimits,
    ResourceObservation,
)
from harness2.store import Store


class IntelligenceResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(os.path.join(self.temp.name, "state", "harness.db"))

    def test_scores_are_computed_only_from_recorded_observations(self):
        intelligence = ProviderIntelligence(self.store)
        self.assertEqual(intelligence.scores(), ())
        intelligence.record(ProviderObservation(
            "provider-a", "runtime-a", "code.debug.python", True, 100,
            "a" * 64, correctness_score=0.8,
        ))
        intelligence.record(ProviderObservation(
            "provider-a", "runtime-a", "code.debug.python", False, 300,
            "b" * 64, correctness_score=0.2, failure_class="wrong_answer",
        ))
        score = intelligence.scores(capability_id="code.debug.python")[0]
        self.assertEqual(score.observations, 2)
        self.assertEqual(score.success_rate, 0.5)
        self.assertEqual(score.mean_correctness, 0.5)
        self.assertEqual(score.mean_latency_ms, 200)

    def test_governor_checkpoints_running_work_instead_of_silent_kill(self):
        governor = ResourceGovernor(self.store, ResourceLimits(min_disk_free_bytes=1000))
        observation = ResourceObservation("node-a", disk_free_bytes=10)
        decision = governor.evaluate(observation, task_running=True)
        self.assertEqual(decision.action, ResourceAction.CHECKPOINT_AND_PAUSE)
        self.assertTrue(decision.checkpoint_required)
        governor.record(observation)
        with self.store.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM kernel_resource_observations").fetchone()[0], 1)

    def test_low_battery_refuses_noncritical_new_work_but_not_critical_work(self):
        governor = ResourceGovernor(self.store)
        observation = ResourceObservation(
            "phone", disk_free_bytes=10 * 1024**3,
            battery_percent=10, charging=False,
        )
        self.assertEqual(governor.evaluate(observation).action, ResourceAction.REFUSE)
        self.assertEqual(governor.evaluate(observation, critical=True).action, ResourceAction.ALLOW)

    def test_invalid_resource_and_provider_claims_are_rejected(self):
        with self.assertRaises(ValueError):
            ResourceObservation("phone", disk_free_bytes=1, battery_percent=101)
        with self.assertRaises(ValueError):
            ProviderObservation(
                "provider", "runtime", "capability", True, 1,
                "z" * 64,
            )


if __name__ == "__main__":
    unittest.main()
