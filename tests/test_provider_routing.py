"""Provider-fluid routing: candidates, fallback, taxonomy, circuits, envelope.

Covers the phase requirements: generic AUTO candidates, quota fallback,
model/provider-granular cooldown scope, disabled/unconfigured skipping,
explicit intent, --no-fallback, failure normalization, cooldown expiry,
immutable task envelope, dry-run zero execution, run-record observability.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
import unittest.mock as mock
from dataclasses import replace

from harness2.adapters.base import EngineAdapter
from harness2.circuit import CircuitBreaker
from harness2.events import (
    FAILURE_AUTHENTICATION,
    FAILURE_CONTEXT_TOO_LARGE,
    FAILURE_ENGINE,
    FAILURE_ENGINE_UNAVAILABLE,
    FAILURE_NETWORK,
    FAILURE_POLICY_DENIED,
    FAILURE_QUOTA_EXHAUSTED,
    FAILURE_RATE_LIMITED,
    FAILURE_UNKNOWN,
    normalize_failure,
    parse_text,
)
from harness2.models import CapabilityStatus, EngineResult, EngineStatus, RunRequest
from harness2.orchestrator import Orchestrator
from harness2.policy import PolicyRefusal, PolicyRouter
from harness2.router import assemble_candidates, fallback_order, rank_fallbacks, score_candidate
from harness2.store import Store


class FakeEngine(EngineAdapter):
    def __init__(self, name, *, available=True, healthy=True, enabled=True,
                 capabilities=("reason.general",), detail="", cost_class="unknown",
                 result=None, capture=None):
        self.name = name
        self._available = available
        self._healthy = healthy
        self._enabled = enabled
        self._capabilities = capabilities
        self._detail = detail
        self._cost_class = cost_class
        self._result = result
        self.capture = [] if capture is None else capture
        self.calls = 0

    def status(self) -> EngineStatus:
        return EngineStatus(
            self.name, self._available, self._healthy, self._enabled,
            CapabilityStatus.ACTIVE if self._healthy else CapabilityStatus.IMPLEMENTED,
            self._detail, self._capabilities, "private-file", self._cost_class,
        )

    def run(self, request: RunRequest) -> EngineResult:
        self.calls += 1
        self.capture.append((request.prompt, request.cwd, request.engine, request.model))
        if self._result is not None:
            return self._result
        return EngineResult(self.name, True, text=f"{self.name}-ok")


def statuses(
    *, local=True, hermes=True, opencode=True, prime=True, zen=True,
) -> dict:
    def one(name, available, healthy, enabled, capabilities, detail="", cost="unknown"):
        return EngineStatus(
            name, available, healthy, enabled,
            CapabilityStatus.ACTIVE if healthy else CapabilityStatus.IMPLEMENTED,
            detail, capabilities, "private-file", cost,
        )
    return {
        "local": one("local", local, local, local, ("reason.private",), "loopback", "private"),
        "hermes": one("hermes", hermes, hermes, hermes, ("reason.general", "messaging"), "gateway", "mixed"),
        "opencode": one("opencode", opencode, opencode, opencode, ("reason.general", "coding"), "ready", "mixed"),
        "prime": one("prime", prime, prime, prime, ("reason.general", "durable-sessions"), "daemon", "mixed"),
        "zen": one("zen", zen, zen, zen, ("reason.general", "curated-models"), "key", "free"),
    }


class NormalizationTests(unittest.TestCase):
    def test_quota_patterns(self):
        for code, message in [
            ("APIError", "The usage limit has been reached"),
            ("error", "You exceeded your current quota, please check your plan and billing details"),
            (None, "Insufficient Balance"),
            ("InsufficientQuotaError", "quota exceeded for org"),
        ]:
            self.assertEqual(normalize_failure(code, message), FAILURE_QUOTA_EXHAUSTED, (code, message))

    def test_rate_limit_patterns(self):
        for code, message in [
            ("429", "rate limit exceeded"),
            ("TooManyRequests", "Too many requests"),
            ("TPMError", "tokens per minute (TPM): Limit 12000"),
        ]:
            self.assertEqual(normalize_failure(code, message), FAILURE_RATE_LIMITED, (code, message))

    def test_auth_patterns(self):
        for code, message in [
            ("401", "invalid API key provided"),
            ("AuthenticationError", "Incorrect API key"),
            ("missing_credential", "OpenCode Zen key is not configured"),
        ]:
            self.assertEqual(normalize_failure(code, message), FAILURE_AUTHENTICATION, (code, message))

    def test_context_too_large_is_not_quota(self):
        for message in [
            "This model's maximum context length is 128000 tokens",
            "reduce your message size and try again",
        ]:
            self.assertEqual(normalize_failure(None, message), FAILURE_CONTEXT_TOO_LARGE, message)

    def test_policy_and_engine_codes(self):
        self.assertEqual(normalize_failure("policy_denied", ""), FAILURE_POLICY_DENIED)
        self.assertEqual(normalize_failure("disabled", ""), FAILURE_POLICY_DENIED)
        self.assertEqual(normalize_failure("spawn_error", "boom"), FAILURE_ENGINE)
        self.assertEqual(normalize_failure("unavailable", ""), FAILURE_ENGINE_UNAVAILABLE)
        self.assertEqual(normalize_failure(None, ""), FAILURE_UNKNOWN)

    def test_multi_step_stream_is_terminal_only_at_stop(self):
        stream = (
            '{"type":"step_start","sessionID":"s1","part":{"id":"p1"}}\n'
            '{"type":"step_finish","sessionID":"s1","part":{"type":"step-finish","reason":"tool-calls"}}\n'
            '{"type":"step_start","sessionID":"s1","part":{"id":"p2"}}\n'
            '{"type":"text","sessionID":"s1","part":{"type":"text","text":"ANSWER"}}\n'
            '{"type":"step_finish","sessionID":"s1","part":{"type":"step-finish","reason":"stop"}}\n'
        )
        result = parse_text("opencode", stream, strict=True)
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.text, "ANSWER")
        self.assertEqual(result.trailing_count, 0)
        self.assertEqual(result.error_code, None)


class CandidateTests(unittest.TestCase):
    def test_auto_returns_multiple_eligible_candidates(self):
        decision = PolicyRouter(statuses()).decide(RunRequest("ordinary task"))
        self.assertEqual(decision.engine, "opencode")
        self.assertIn("prime", decision.candidates)
        self.assertIn("prime", decision.fallbacks)
        self.assertEqual(decision.candidates[0], "opencode")

    def test_disabled_engines_are_skipped(self):
        decision = PolicyRouter(statuses(zen=False, hermes=False)).decide(RunRequest("ordinary task"))
        skipped = dict(decision.skipped)
        self.assertIn("zen", skipped)
        self.assertIn("hermes", skipped)
        self.assertNotIn("zen", decision.candidates)
        self.assertNotIn("hermes", decision.candidates)

    def test_unconfigured_authentication_is_skipped(self):
        route = assemble_candidates("opencode", statuses(zen=False))
        self.assertNotIn("zen", route.candidates)
        self.assertTrue(any(name == "zen" and "disabled" in reason for name, reason in route.skipped))

    def test_unknown_engine_skipped(self):
        with mock.patch.dict(os.environ, {"HARNESS_FALLBACK_ORDER": "opencode,missing"}, clear=False):
            route = assemble_candidates("opencode", statuses())
        self.assertTrue(any(name == "missing" and "unknown" in reason for name, reason in route.skipped))
        self.assertEqual(route.candidates, ("opencode",))

    def test_explicit_engine_preserves_intent(self):
        decision = PolicyRouter(statuses()).decide(RunRequest("code", engine="prime"))
        self.assertEqual(decision.engine, "prime")
        self.assertEqual(decision.fallbacks, ())
        self.assertEqual(decision.candidates, ("prime",))

    def test_fallback_order_env_override(self):
        with mock.patch.dict(os.environ, {"HARNESS_FALLBACK_ORDER": "zen,prime"}, clear=False):
            self.assertEqual(fallback_order(), ("zen", "prime"))

    def test_scoring_prefers_free_and_healthy(self):
        status = statuses()["zen"]
        self.assertGreater(
            score_candidate("zen", status, cost_class="free"),
            score_candidate("prime", status, cost_class="paid-api"),
        )
        self.assertLess(
            score_candidate("prime", status, circuit_allowed=False),
            score_candidate("prime", status, circuit_allowed=True),
        )

    def test_rank_fallbacks_is_stable_and_scored(self):
        ranked = rank_fallbacks(
            ("prime", "zen"), statuses(),
            capability_fit={"zen": "reason.general"},
        )
        self.assertEqual(ranked[0], "zen")  # free + capability fit beats mixed


class PolicyFastPathTests(unittest.TestCase):
    """Q&A fast path: short general prompts route to the direct engine
    first (raw REST, sub-second); long/complex prompts keep the opencode
    control plane."""

    def _pool_with_direct(self, *, healthy=True):
        pool = statuses()
        pool["direct"] = EngineStatus(
            "direct", healthy, healthy, healthy,
            CapabilityStatus.ACTIVE if healthy else CapabilityStatus.IMPLEMENTED,
            "" if healthy else "no direct provider key configured",
            ("reason.general",), "external", "free",
        )
        return pool

    def test_short_general_prompt_fast_paths_to_direct(self):
        decision = PolicyRouter(self._pool_with_direct()).decide(RunRequest("hello"))
        self.assertEqual(decision.engine, "direct")
        self.assertEqual(decision.task_class, "fast")
        self.assertEqual(decision.candidates[0], "direct")
        self.assertIn("opencode", decision.fallbacks)
        self.assertIn("opencode", decision.candidates)

    def test_long_prompt_keeps_control_plane(self):
        decision = PolicyRouter(self._pool_with_direct()).decide(RunRequest("word " * 60))
        self.assertEqual(decision.engine, "opencode")
        self.assertEqual(decision.task_class, "control")
        self.assertEqual(decision.candidates[0], "opencode")
        # direct remains a later fallback in the agent chain.
        self.assertIn("direct", decision.candidates[1:])

    def test_fast_path_disabled_when_direct_unhealthy(self):
        decision = PolicyRouter(self._pool_with_direct(healthy=False)).decide(RunRequest("hello"))
        self.assertEqual(decision.engine, "opencode")
        self.assertEqual(decision.task_class, "control")

    def test_fast_path_respects_explicit_engine(self):
        decision = PolicyRouter(self._pool_with_direct()).decide(
            RunRequest("hello", engine="prime"),
        )
        self.assertEqual(decision.engine, "prime")
        self.assertEqual(decision.candidates, ("prime",))


class OrchestratorFallbackTests(unittest.TestCase):
    def make(self, engines, *, threshold=3, cooldown=30.0):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        store = Store(os.path.join(tmp.name, "state", "harness.db"))
        breaker = CircuitBreaker(store, threshold=threshold, base_cooldown=cooldown)
        orchestrator = Orchestrator({e.name: e for e in engines}, store)
        orchestrator.breakers = breaker
        return orchestrator

    def test_quota_failure_triggers_fallback(self):
        failed = FakeEngine(
            "opencode",
            result=EngineResult("opencode", False, error="The usage limit has been reached", error_code="APIError", exit_code=1),
        )
        ok = FakeEngine("prime")
        orchestrator = self.make([failed, ok])
        decision, result, run_id = orchestrator.run(RunRequest("hello", engine="auto"))
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.engine, "prime")
        self.assertTrue(run_id)
        tried = result.metadata["tried_routes"]
        self.assertEqual([t["engine"] for t in tried], ["opencode", "prime"])
        self.assertEqual(tried[0]["normalized_failure"], FAILURE_QUOTA_EXHAUSTED)
        self.assertTrue(result.metadata["fell_back"])

    def test_first_model_failure_does_not_blacklist_other_keys(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "state", "harness.db"))
        breaker = CircuitBreaker(store, threshold=1, base_cooldown=600)
        breaker.failure("opencode:openai:gpt-5.6-sol", "quota")
        self.assertFalse(breaker.before("opencode:openai:gpt-5.6-sol").allowed)
        # Unrelated model under the same engine remains eligible.
        self.assertTrue(breaker.before("opencode:google:gemini-3.5-flash-lite").allowed)
        self.assertTrue(breaker.before("opencode:zen:deepseek-v4-flash-free").allowed)

    def test_all_candidates_fail_returns_structured_failure(self):
        first = FakeEngine("opencode", result=EngineResult("opencode", False, error="usage limit", error_code="APIError", exit_code=1))
        second = FakeEngine("prime", result=EngineResult("prime", False, error="connection refused", error_code="network_error", exit_code=1))
        orchestrator = self.make([first, second])
        decision, result, run_id = orchestrator.run(RunRequest("hello", engine="auto"))
        self.assertFalse(result.success)
        self.assertEqual(len(result.metadata["tried_routes"]), 2)
        self.assertEqual(result.metadata["tried_routes"][0]["normalized_failure"], FAILURE_QUOTA_EXHAUSTED)
        self.assertEqual(result.metadata["tried_routes"][1]["normalized_failure"], FAILURE_NETWORK)
        self.assertEqual(result.metadata["final_route"]["engine"], "prime")
        self.assertTrue(run_id)

    def test_explicit_engine_has_no_unintended_fallback(self):
        failed = FakeEngine("opencode", result=EngineResult("opencode", False, error="usage limit", error_code="APIError", exit_code=1))
        ok = FakeEngine("prime")
        orchestrator = self.make([failed, ok])
        decision, result, run_id = orchestrator.run(RunRequest("hello", engine="opencode"))
        self.assertFalse(result.success)
        self.assertEqual(ok.calls, 0)
        self.assertEqual([t["engine"] for t in result.metadata["tried_routes"]], ["opencode"])

    def test_no_fallback_means_exactly_one_attempt(self):
        failed = FakeEngine("opencode", result=EngineResult("opencode", False, error="usage limit", error_code="APIError", exit_code=1))
        ok = FakeEngine("prime")
        orchestrator = self.make([failed, ok])
        decision, result, run_id = orchestrator.run(RunRequest("hello", engine="auto", no_fallback=True))
        self.assertFalse(result.success)
        self.assertEqual(ok.calls, 0)
        self.assertEqual(len(result.metadata["tried_routes"]), 1)

    def test_cooldown_expiry_restores_eligibility(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "state", "harness.db"))
        breaker = CircuitBreaker(store, threshold=1, base_cooldown=1.0)
        store.save_circuit({"key": "opencode:-:-", "state": "closed", "failures": 0,
                            "opened_at": None, "cooldown": 1.0, "last_error": None,
                            "updated_at": 0.0})
        breaker.failure("opencode:-:-", "boom")
        self.assertFalse(breaker.before("opencode:-:-").allowed)
        time.sleep(2.05)  # first failure doubles cooldown: 2 * base
        self.assertTrue(breaker.before("opencode:-:-").allowed)

    def test_envelope_is_immutable_across_fallback(self):
        failed = FakeEngine("opencode", result=EngineResult("opencode", False, error="usage limit", error_code="APIError", exit_code=1))
        ok = FakeEngine("prime", capture=[])
        orchestrator = self.make([failed, ok])
        cwd = tempfile.mkdtemp()
        request = RunRequest("exact prompt", engine="auto", cwd=cwd)
        decision, result, run_id = orchestrator.run(request)
        self.assertTrue(result.success)
        self.assertEqual(len(ok.capture), 1)
        captured_prompt, captured_cwd, captured_engine, captured_model = ok.capture[0]
        self.assertEqual(captured_prompt, "exact prompt")
        self.assertEqual(captured_cwd, cwd)

    def test_dry_run_performs_zero_provider_execution(self):
        def boom(request):
            raise AssertionError("dry-run must not invoke providers")
        first = FakeEngine("opencode")
        first.run = boom  # type: ignore[method-assign]
        second = FakeEngine("prime")
        second.run = boom  # type: ignore[method-assign]
        orchestrator = self.make([first, second])
        decision, result, run_id = orchestrator.run(RunRequest("hello", engine="auto", dry_run=True))
        self.assertTrue(result.success)
        self.assertEqual(run_id, "")
        self.assertIn("candidate_routes", result.metadata)
        self.assertIn("opencode", result.metadata["candidate_routes"])

    def test_run_record_contains_route_observability(self):
        failed = FakeEngine("opencode", result=EngineResult("opencode", False, error="usage limit", error_code="APIError", exit_code=1))
        ok = FakeEngine("prime")
        orchestrator = self.make([failed, ok])
        decision, result, run_id = orchestrator.run(RunRequest("hello", engine="auto"))
        with orchestrator.store.connect() as con:
            row = con.execute("SELECT status, engine FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["engine"], "prime")
        verified, count, _ = orchestrator.store.verify_audit()
        self.assertTrue(verified)
        with orchestrator.store.connect() as con:
            events = [r["event"] for r in con.execute(
                "SELECT event FROM audit WHERE event='route.failed'",
            )]
        self.assertIn("route.failed", events)


if __name__ == "__main__":
    unittest.main()
